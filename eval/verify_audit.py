#!/usr/bin/env python3
"""Fidelity and calibration report for selector/verify.py, from container runs.

usage: verify_audit.py <set-dir> <static.jsonl> <audit.jsonl> <audit.err> [--fit]

<static.jsonl> is a run with BR_VERIFY=0, <audit.*> a run with BR_VERIFY=1 BR_VERIFY_AUDIT=1
(every runnable commit verified, no cost filter, no guards). Prints, per verified commit:
static vs verified cost, measured CPU, and any labeled failure the verified output dropped
(must be none), plus tests reported failing that the labels say pass (wasted seconds only).
With --fit: least-squares CPU model (overhead + per file + per recorded second) and the net
gain of the shipped cost policy at its current constants.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selector import diff, pack  # noqa: E402

POOL_FILES = {"tests/test_optimizer.py", "tests/test_executor.py"}  # verify.POOL_MARKERS at the pin
LINE = re.compile(r"\[verify\] (\w+): (.*); est ([\d.]+) s, cpu ([\d.]+) s, wall ([\d.]+) s, "
                  r"expected saving ([\d.]+) s")


def load(path: Path) -> dict[str, set[int]]:
    return {r["commit"]: {n for w in r["workers"] for n in w}
            for r in map(json.loads, path.read_text().splitlines())}


def main() -> None:
    sdir, static_p, audit_p, err_p = map(Path, sys.argv[1:5])
    tests = json.loads((sdir / "tests.json").read_text())["tests"]
    ms = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
    sec = {t["number"]: t["seconds"] for t in tests}
    file_of = {t["number"]: t["file"] for t in tests}
    labels = {c: set(a["failing"]) for c, a in
              json.loads((sdir / "answers.json").read_text())["commits"].items()}
    static, audit = load(static_p), load(audit_p)
    cost = lambda s: pack.cost_ms(pack.pack(s, ms), ms) / 1000  # noqa: E731
    rows = []
    for line in err_p.read_text().splitlines():
        m = LINE.match(line)
        if not m:
            continue
        c, note, est, cpu, wall, exp = m.groups()
        S, V, L = static[c], audit[c], labels.get(c, set())
        miss = sorted((L & S) - V)
        R = {n for n in S if sec[n] < 5 and file_of[n] not in POOL_FILES}
        rows.append(dict(c=c, note=note, est=float(est), cpu=float(cpu), wall=float(wall),
                         exp=float(exp), cs=cost(S), cv=cost(V), miss=miss,
                         nr=len(R), rsec=sum(sec[n] for n in R), nf=len({file_of[n] for n in R}),
                         cs_keep=cost(S - R), anomaly=note.startswith("static"),
                         data=any(not fp.path.endswith(".py") for fp in
                                  diff.parse((sdir / "commits" / f"{c}.patch").read_text()))))
    n_miss = sum(bool(r["miss"]) for r in rows)
    print(f"{sdir.name}: {len(rows)} verified, {sum(r['anomaly'] for r in rows)} anomalies, "
          f"{n_miss} commits drop a labeled failure")
    for r in rows:
        flag = " MISS " + str(r["miss"][:5]) if r["miss"] else ""
        print(f"  {r['c']:24s} static {r['cs']:6.2f} -> {r['cv']:6.2f}  cpu {r['cpu']:5.2f} "
              f"est {r['est']:5.2f} R {r['nr']:4d}/{r['rsec']:6.2f}s/{r['nf']:2d}f  {r['note'][:60]}{flag}")
    tot_save = sum(r["cs"] - r["cv"] for r in rows)
    tot_cpu = sum(r["cpu"] for r in rows)
    print(f"  all verified: saving {tot_save:.1f} s, cpu {tot_cpu:.1f} s, net {tot_save - tot_cpu:+.1f} s")
    chosen = [r for r in rows if r["exp"] > r["est"] + 0.3]
    s2, c2 = sum(r["cs"] - r["cv"] for r in chosen), sum(r["cpu"] for r in chosen)
    print(f"  policy (expected > est + 0.3): {len(chosen)} commits, saving {s2:.1f} s, "
          f"cpu {c2:.1f} s, net {s2 - c2:+.1f} s (server start not included)")
    json.dump(rows, open(str(audit_p) + ".rows.json", "w"))


if __name__ == "__main__":
    main()
