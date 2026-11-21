"""On a labeled set: seconds saved by an oracle that drops every selected heavy test that passes.
usage: labeled.py dir sel.json"""
import json, sys
from collections import Counter, defaultdict
sys.path.insert(0, "/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a98424c1132b02e15")
from selector.pack import pack, cost_ms

d = sys.argv[1]
tests = json.load(open(d + "/tests.json"))["tests"]
ans = json.load(open(d + "/answers.json"))["commits"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
name = {t["number"]: t["id"].split("::")[-1] for t in tests}
HANDLED = {n for n in ms if name[n] in ("test_max_nodes", "test_execute_tpcds", "test_generator_memory",
                                         "test_execute_tpch", "test_optimized_tpch")}
MID = {n for n, m in ms.items() if m >= 1000} - HANDLED
SEMI = {n for n, m in ms.items() if 500 <= m < 1000}


def c(sel):
    return cost_ms(pack(set(sel), ms), ms) / 1000 if sel else 0.0


sel = json.load(open(sys.argv[2]))
tot = 0
sv = Counter()
sel_n, fail_n = Counter(), Counter()
for cid, r in sel.items():
    s = {n for w in r["workers"] for n in w}
    f = set(ans[cid]["failing"])
    now = c(s)
    tot += now
    if len(s) > 1500:
        continue
    for grp, G in (("mid", MID), ("mid+semi", MID | SEMI), ("handled", HANDLED), ("all>=0.5", MID | SEMI | HANDLED)):
        sv[grp] += now - c(s - (G - f))
    for n in s & (MID | HANDLED):
        sel_n[n] += 1
        fail_n[n] += n in f
print(f"{d}: {len(sel)} commits, total {tot:.1f}s; oracle-drop of passing tests saves:",
      {k: round(v, 1) for k, v in sv.items()})
for n in sorted(sel_n, key=lambda n: -sel_n[n]):
    print(f"   {name[n]:40s} selected {sel_n[n]:3d} failing {fail_n[n]:3d}")
