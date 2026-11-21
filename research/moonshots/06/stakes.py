"""Seconds at stake from the 1-30 s tests in dumped selections.

usage: stakes.py tests.json sel.json [sel2.json ...]
For each commit: cost now; cost if each heavy test is dropped alone (repacked); cost if all
mid-tier (1 <= s < 3, not handled) heavy tests dropped; cost if all >= 1 s dropped.
"""
import json, sys
from collections import Counter, defaultdict
sys.path.insert(0, "/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a98424c1132b02e15")
from selector.pack import pack, cost_ms

tests = json.load(open(sys.argv[1]))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
name = {t["number"]: t["id"].split("::")[-1] for t in tests}
HEAVY = {n for n, m in ms.items() if m >= 1000}
HANDLED = {n for n in HEAVY if name[n] in ("test_max_nodes", "test_execute_tpcds", "test_generator_memory",
                                         "test_execute_tpch", "test_optimized_tpch")}
MID = HEAVY - HANDLED
SEMI = {n for n, m in ms.items() if 500 <= m < 1000}


def c(sel):
    return cost_ms(pack(set(sel), ms), ms) / 1000 if sel else 0.0


for f in sys.argv[2:]:
    d = json.load(open(f))
    tot = 0.0
    solo = defaultdict(float)
    cnt = Counter()
    drop_mid = drop_heavy = drop_handled = drop_semi = 0.0
    nall = 0
    commits_mid = 0
    for cid, r in d.items():
        sel = {n for w in r["workers"] for n in w}
        now = c(sel)
        tot += now
        if len(sel) > 1500:
            nall += 1
            continue  # ALL: decomposition irrelevant (ALL fallback)
        for n in sel & HEAVY:
            cnt[n] += 1
            solo[n] += now - c(sel - {n})
        if sel & MID:
            commits_mid += 1
        drop_mid += now - c(sel - MID)
        drop_heavy += now - c(sel - HEAVY)
        drop_handled += now - c(sel - HANDLED)
        drop_semi += now - c(sel - MID - SEMI)
    print(f"== {f}: {len(d)} commits, total {tot:.1f}s, ALL-ish {nall}")
    print(f"   drop all >=1s: -{drop_heavy:.1f}s | drop handled5: -{drop_handled:.1f}s | "
          f"drop mid(1-3s, not handled): -{drop_mid:.1f}s over {commits_mid} commits | drop mid+0.5-1s: -{drop_semi:.1f}s")
    for n in sorted(cnt, key=lambda n: -solo[n]):
        print(f"   {name[n]:45s} {ms[n]/1000:6.2f}s  selected {cnt[n]:4d}  solo saving {solo[n]:7.1f}s")
