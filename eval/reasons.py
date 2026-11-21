#!/usr/bin/env python3
"""Rank WHY the selector spends seconds on unlabeled sample-style patches (eval/ab.py runs).

Buckets every patch with a non-zero cost by the mechanism its reason string names (ALL
fallbacks, diffexec gaps, blocked name narrowing, fixtures, …) and sums the simulated seconds
per (edit kind, bucket). No labels: a bucket's seconds are spend, not waste; use it to pick
what to attack, then measure recall on the labeled sets (eval/gap.py, eval/evaluate.py).

usage:
  python3 eval/reasons.py --run NAME DIR [DIR ...] [--top 40] [--examples 0]
  (DIR = an eval/ab.py patches dir holding meta.json and runs/NAME.json)
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path


def bucket(why: str) -> str:
    if why.startswith("ALL"):
        if "selection ~ALL cost" in why:
            return "ALL: selection ~ALL cost (function run by ~every test / runs at import)"
        if "runs during `import sqlglot`" in why:
            return "ALL: lines run during `import sqlglot`"
        if "base class / module body" in why:
            return "ALL: base-file class / module / def-time body"
        if "binding removed" in why:
            return "ALL: binding removed"
        if "definition-time change with no known users" in why:
            return "ALL: definition-time change, no known users"
        return "ALL: other"
    m = re.search(r"not recorded \(([^)]*)\)", why)
    if m:
        return f"diffexec: function not recorded ({m[1]})"
    m = re.search(r"no narrowing \((.*?)\)[;\]]", why)
    if m:
        what = re.sub(r"\d+", "N", m[1])
        what = re.sub(r"^\S+ read at import", "<name> read at import", what)
        return "no name narrowing: " + what[:80]
    if "fixture" in why:
        return "fixture → readers"
    if "diffexec: dropped" in why:
        return "diffexec: replayed, residual tests whose calls' effects differ"
    if "runs at import" in why:
        return "function runs at import (import-time users added)"
    if "definition-time" in why:
        return "unrun def → definition-time"
    if "same syntax tree" in why:
        return "same syntax tree (+ other part of a multi-file patch)"
    return "function map, other"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+", type=Path)
    ap.add_argument("--run", default="base")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--examples", type=int, default=0)
    a = ap.parse_args()
    secs: collections.Counter = collections.Counter()
    count: collections.Counter = collections.Counter()
    examples: dict = collections.defaultdict(list)
    total = n = 0
    for d in a.dirs:
        res = json.loads((d / "runs" / f"{a.run}.json").read_text())
        meta = json.loads((d / "meta.json").read_text())
        for pid, (cost, why) in res.items():
            n += 1
            total += cost
            if cost <= 0:
                continue
            key = (meta[pid]["kind"], bucket(why))
            secs[key] += cost
            count[key] += 1
            examples[key].append((cost, f"{d.name}/{pid}", why[:200]))
    print(f"{total:.1f}s over {n} patches ({a.run})")
    print(f"{'seconds':>9} {'share':>6} {'n':>5}  kind | bucket")
    for (kind, b), s in secs.most_common(a.top):
        print(f"{s:9.1f} {100 * s / total:5.1f}% {count[kind, b]:5d}  {kind} | {b}")
        for cost, pid, why in sorted(examples[kind, b], reverse=True)[:a.examples]:
            print(f"{'':24}{cost:5.1f}s {pid} {why}")


if __name__ == "__main__":
    main()
