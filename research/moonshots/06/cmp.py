"""cmp.py dir selA.json selB.json : cost + misses of each (answers optional)."""
import json, sys
from pathlib import Path
sys.path.insert(0, "/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a98424c1132b02e15")
from selector.pack import cost_ms

d = Path(sys.argv[1])
tests = json.load(open(d / "tests.json"))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
name = {t["number"]: t["id"].split("::")[-1] for t in tests}
ans = json.load(open(d / "answers.json"))["commits"] if (d / "answers.json").exists() else None
sels = [json.load(open(f)) for f in sys.argv[2:]]
for f, s in zip(sys.argv[2:], sels):
    tot = sum(cost_ms(r["workers"], ms) for r in s.values()) / 1000
    miss = []
    if ans:
        for cid, r in s.items():
            got = {n for w in r["workers"] for n in w}
            lost = set(ans[cid]["failing"]) - got
            if lost:
                miss.append((cid, [name[n] for n in lost]))
    print(f"{Path(f).name}: {len(s)} commits, {tot:.1f}s ({tot / len(s) * 40:.1f}/40), misses {len(miss)} {miss[:5]}")
if len(sels) == 2:
    a, b = sels
    diffs = []
    for cid in a:
        ca, cb = cost_ms(a[cid]["workers"], ms) / 1000, cost_ms(b[cid]["workers"], ms) / 1000
        if abs(ca - cb) > 0.05:
            ga = {n for w in a[cid]["workers"] for n in w}
            gb = {n for w in b[cid]["workers"] for n in w}
            diffs.append((cb - ca, cid, sorted(name[n] for n in ga - gb if ms[n] >= 300)))
    diffs.sort()
    print(f"changed commits {len(diffs)}; delta {sum(x[0] for x in diffs):+.1f}s")
    for x in diffs[:12]:
        print("  ", round(x[0], 2), x[1], x[2][:6])
