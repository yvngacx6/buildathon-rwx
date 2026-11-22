"""Probe check: failures (full-suite labels, lab/<name>.txt) must be inside each selection.

usage: python3 checkprobes.py <probes dir> <lab dir> <base.out> <new.out>
"""
import json
import sys
from pathlib import Path

probes, lab, base, new = map(Path, sys.argv[1:5])
ids = {t["number"]: t["id"] for t in json.loads((probes / "tests.json").read_text())["tests"]}
secs = {t["id"]: t.get("seconds", 0) for t in json.loads((probes / "tests.json").read_text())["tests"]}


def load(p):
    out = {}
    for line in p.read_text().splitlines():
        d = json.loads(line)
        out[d["commit"]] = {ids[n] for w in d["workers"] for n in w}
    return out


b, w = load(base), load(new)
print(f"{'probe':6} {'fail':>4} {'base#':>6} {'new#':>6} {'base s':>7} {'new s':>7}  miss")
for f in sorted(lab.glob("*.txt")):
    import re
    lines = f.read_text().splitlines()
    log = f.with_suffix(".log").read_text().splitlines()
    fail = {m.group(0) for x in log if re.match(r"(FAILED|ERROR|SUBFAILED)", x)
            for m in [re.search(r"tests/\S+::[^\s\[]+(\[[^\]]*\])?", x)] if m}
    fail = {re.sub(r"\[.*\]$", "", t) if t.split("::")[-1].split("[")[0] in t else t
            for t in fail}
    c = f.stem
    if c not in w:
        continue
    miss_new = sorted(fail - w[c])
    miss_base = sorted(fail - b.get(c, set()))
    print(f"{c:6} {len(fail):4} {len(b.get(c, ())):6} {len(w[c]):6} "
          f"{sum(secs[t] for t in b.get(c, ())):7.1f} {sum(secs[t] for t in w[c]):7.1f}  "
          f"{len(miss_new)} {miss_new[:2]} (base {len(miss_base)}) | {lines[-1][:60]}")
