#!/usr/bin/env python3
"""A/B the selector's cost at two revisions on UNLABELED sample-style patches.

No labels: this measures only how much each revision selects (simulated seconds, as the
scorer prices a commit: max worker sum + 2 s per worker), never recall. Use it to find
rules that broaden selections on shapes the labeled sets lack, then check recall on the
labeled sets (eval/evaluate.py).

usage:
  python3 eval/ab.py gen  --out DIR [--n 600] [--seed 7331]
  python3 eval/ab.py run  --patches DIR --rev A=/path/to/checkoutA --rev B=/path/to/checkoutB [--shards 8]
  python3 eval/ab.py diff --patches DIR [--top 25]

Each checkout must have its index built (python3 -m selector.nameindex build ... index/names.pkl;
python3 -m selector.maps build maps index/maps). The current repo can be one of them
(e.g. --rev B=.). Results go to DIR/runs/<name>.json ({commit: [cost_s, reason]}).
Needs BR_SQLGLOT (default ~/rwx-research-robust/sqlglot) at the pinned commit.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SQLGLOT = Path(os.environ.get("BR_SQLGLOT", Path.home() / "rwx-research-robust" / "sqlglot"))
KINDS = {"code": 0.60, "comment": 0.13, "fixture": 0.17, "multi": 0.10}  # sample-style only
LINE = re.compile(r"^\[(\S+)\] (\d+) tests, (\d+) workers, ([\d.]+)s :: (.*)$")


def gen(out: Path, n: int, seed: int) -> None:
    sys.path.insert(0, str(REPO / "eval"))
    import mutants
    g = mutants.Gen(SQLGLOT, seed)
    tmp = Path(tempfile.mkdtemp())
    work = tmp / "sg"
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(SQLGLOT), str(work)], check=True)
    mutants.git(work, "checkout", "-q", mutants.PINNED)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "sample" / "tests.json", out / "tests.json")
    meta, seen = {}, set()
    for kind, frac in KINDS.items():
        made, want = 0, round(n * frac)
        while made < want:
            res = g.make(kind)
            if not res:
                continue
            files, desc = res
            for rel, body in files.items():
                (work / rel).write_text(body)
            mutants.git(work, "add", "-A")
            diff = mutants.git(work, "diff", "--cached", "--abbrev=7")
            mutants.git(work, "reset", "-q", "--hard", mutants.PINNED)
            if not diff or diff in seen:
                continue
            seen.add(diff)
            pid = g.new_id()
            (out / "commits" / f"{pid}.patch").write_text(diff)
            meta[pid] = {"kind": kind, "desc": desc, "files": sorted(files)}
            made += 1
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    shutil.rmtree(tmp)
    print(f"{len(meta)} patches -> {out / 'commits'}")


def run_one(checkout: Path, patches: Path, ids: list[str], shard: Path) -> dict:
    (shard / "commits").mkdir(parents=True, exist_ok=True)
    shutil.copy(patches / "tests.json", shard / "tests.json")
    for i in ids:
        shutil.copy(patches / "commits" / f"{i}.patch", shard / "commits")
    env = dict(os.environ, BR_DATA=str(shard), BR_SQLGLOT=str(SQLGLOT))
    proc = subprocess.run([sys.executable, "-m", "selector"], cwd=checkout, env=env,
                          capture_output=True, text=True)
    res = {}
    for line in proc.stderr.splitlines():
        m = LINE.match(line)
        if m:
            res[m[1]] = [float(m[4]), m[5]]
    missing = set(ids) - set(res)
    if missing:
        sys.exit(f"{checkout}: no result for {sorted(missing)[:5]}\n{proc.stderr[-2000:]}")
    return res


def run(patches: Path, revs: list[str], shards: int) -> None:
    ids = sorted(p.stem for p in (patches / "commits").glob("*.patch"))
    tmp = Path(tempfile.mkdtemp())
    jobs = []
    with ThreadPoolExecutor(max_workers=shards * len(revs)) as ex:
        for spec in revs:
            name, path = spec.split("=", 1)
            for k in range(shards):
                jobs.append((name, ex.submit(run_one, Path(path).resolve(), patches, ids[k::shards],
                                             tmp / f"{name}{k}")))
        out: dict[str, dict] = {}
        for name, fut in jobs:
            out.setdefault(name, {}).update(fut.result())
    shutil.rmtree(tmp)
    (patches / "runs").mkdir(exist_ok=True)
    for name, res in out.items():
        (patches / "runs" / f"{name}.json").write_text(json.dumps(res, indent=0))
        print(f"{name}: {sum(c for c, _ in res.values()):.1f}s over {len(res)} patches")


def diff(patches: Path, a: str, b: str, top: int) -> None:
    A = json.loads((patches / "runs" / f"{a}.json").read_text())
    B = json.loads((patches / "runs" / f"{b}.json").read_text())
    meta = json.loads((patches / "meta.json").read_text())
    sa, sb = sum(v[0] for v in A.values()), sum(v[0] for v in B.values())
    print(f"{a}: {sa:.1f}s   {b}: {sb:.1f}s   delta {sb - sa:+.1f}s over {len(A)} patches")
    d = sorted(((B[i][0] - A[i][0], i) for i in A), reverse=True)
    up = [x for x in d if x[0] > 0.05]
    down = [x for x in d if x[0] < -0.05]
    print(f"  up: {len(up)} patches {sum(x for x, _ in up):+.1f}s   down: {len(down)} {sum(x for x, _ in down):+.1f}s")
    for delta, i in up[:top]:
        print(f"{delta:+7.1f}s {i} [{meta[i]['kind']}] {meta[i]['desc'][:110]}")
        print(f"         {a}: {A[i][0]:.1f}s {A[i][1][:150]}")
        print(f"         {b}: {B[i][0]:.1f}s {B[i][1][:150]}")
    for delta, i in down[:top // 3]:
        print(f"{delta:+7.1f}s {i} [{meta[i]['kind']}] {meta[i]['desc'][:110]}")
        print(f"         {a}: {A[i][0]:.1f}s {A[i][1][:150]}")
        print(f"         {b}: {B[i][0]:.1f}s {B[i][1][:150]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["gen", "run", "diff"])
    ap.add_argument("--out", "--patches", dest="dir", type=Path, required=True)
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--seed", type=int, default=7331)
    ap.add_argument("--rev", action="append", default=[])
    ap.add_argument("--shards", type=int, default=8)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--ab", nargs=2, default=["A", "B"])
    a = ap.parse_args()
    if a.cmd == "gen":
        gen(a.dir, a.n, a.seed)
    elif a.cmd == "run":
        run(a.dir, a.rev, a.shards)
    else:
        diff(a.dir, *a.ab, a.top)


if __name__ == "__main__":
    main()
