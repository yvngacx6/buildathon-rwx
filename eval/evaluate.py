#!/usr/bin/env python3
"""Score the selector on one or more labeled patch sets, without Docker.

Usage:
  python3 eval/evaluate.py                     # sample set only
  python3 eval/evaluate.py sample private      # several sets
  python3 eval/evaluate.py --json ...          # machine-readable summary

A set is a directory with commits/*.patch, tests.json and answers.json
(the format of sample/). Known sets:
  sample   → sample/
  private  → $BR_PRIVATE or eval/data/private/   (never committed)

Prints simulated seconds, misses (with the missed commit ids and kinds), and selector CPU.
Needs BR_SQLGLOT to point at a sqlglot checkout at the pinned commit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def set_dir(name: str) -> Path:
    if name == "sample":
        return REPO / "sample"
    if name == "private":
        return Path(os.environ.get("BR_PRIVATE", REPO / "eval" / "data" / "private"))
    return Path(name)


def run_selector(data: Path) -> tuple[list[dict], float, str]:
    env = dict(os.environ, BR_DATA=str(data))
    env.setdefault("BR_SQLGLOT", str(Path.home() / "rwx-research-robust" / "sqlglot"))
    t0 = os.times()
    proc = subprocess.run([sys.executable, "-m", "selector"], cwd=REPO, env=env,
                          capture_output=True, text=True)
    t1 = os.times()
    cpu = (t1.children_user - t0.children_user) + (t1.children_system - t0.children_system)
    if proc.returncode != 0:
        sys.exit(f"selector crashed:\n{proc.stderr[-3000:]}")
    rows = [json.loads(l) for l in proc.stdout.splitlines() if l]
    return rows, cpu, proc.stderr


def score(data: Path, rows: list[dict]) -> dict:
    tests = json.loads((data / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    answers = json.loads((data / "answers.json").read_text())["commits"]
    by_commit = {r["commit"]: r["workers"] for r in rows}
    total_ms, misses, per_kind = 0, [], {}
    for commit, ans in answers.items():
        workers = [w for w in by_commit.get(commit, []) if w]
        cost = (max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0
        chosen = {n for w in workers for n in w}
        missed = set(ans["failing"]) - chosen
        if commit not in by_commit or missed:
            misses.append((commit, ans.get("kind", "?"), sorted(missed)[:5]))
            cost += 300_000
        total_ms += cost
        k = per_kind.setdefault(ans.get("kind", "?"), [0, 0])
        k[0] += 1
        k[1] += cost
    return {"commits": len(answers), "simulated_s": total_ms / 1000, "misses": misses,
            "per_kind_s": {k: round(v[1] / 1000, 1) for k, v in per_kind.items()}}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    as_json = "--json" in sys.argv
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    results = {}
    for name in args or ["sample"]:
        data = set_dir(name)
        if not (data / "answers.json").exists():
            print(f"{name}: no labeled set at {data}", file=sys.stderr)
            continue
        rows, cpu, log = run_selector(data)
        r = score(data, rows)
        r["cpu_s"] = round(cpu, 2)
        results[name] = r
        if verbose:
            print(log, file=sys.stderr)
    if as_json:
        print(json.dumps(results))
        return
    for name, r in results.items():
        print(f"{name:8s} {r['simulated_s']:9.1f}s  misses={len(r['misses'])}/{r['commits']}  "
              f"cpu={r['cpu_s']}s  per-kind={r['per_kind_s']}")
        for commit, kind, missed in r["misses"]:
            print(f"         MISS {commit} ({kind}) e.g. tests {missed}")


if __name__ == "__main__":
    main()
