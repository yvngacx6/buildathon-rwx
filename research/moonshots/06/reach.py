"""Reachable fraction of each selected mid-tier test, from frontier traces.
usage: reach.py front.pkl patchdir sel.json [answers?]"""
import json, pickle, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, "/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a98424c1132b02e15")
from selector import diff
from selector.pack import pack, cost_ms

front = pickle.load(open(sys.argv[1], "rb"))
pdir = Path(sys.argv[2])
sel = json.load(open(sys.argv[3]))
tests = json.load(open(pdir / "tests.json"))["tests"]
ansf = pdir / "answers.json"
ans = json.load(open(ansf))["commits"] if ansf.exists() else None
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
num = {t["id"]: t["number"] for t in tests}
T = {}
for nodeid, v in front.items():
    if nodeid in num:
        fr = v["frontiers"]
        tot = sum(f[3] for f in fr) or 1e-9
        T[num[nodeid]] = (nodeid.split("::")[-1], fr, tot, v["outside"], v["total_cpu"])


def c(s):
    return cost_ms(pack(set(s), ms), ms) / 1000 if s else 0.0


def changed(cid):
    out = set()
    for fp in diff.parse((pdir / "commits" / f"{cid}.patch").read_text()):
        p = fp.path
        if not (p and p.startswith("sqlglot/") and p.endswith(".py")):
            continue
        rel = p[len("sqlglot/"):]
        try:
            diff.apply(open("/home/aditya/rwx-research-robust/sqlglot/" + p).read(), fp)
        except Exception:
            out.add(("__unapplied__", 0))
            continue
        old, new = diff.changed_lines(fp)
        # a pure insertion: its statement is the old line before it
        new = [x - 1 for x in new] + new
        for ln in old:
            out.add((rel, ln))
        for h in fp.hunks:
            pass
        # pure insertions: count the old lines around each hunk's change
        for ln in new:
            out.add((rel, ln))  # approximation (new numbering ~ old near the edit)
    return out


rows = []
agg = defaultdict(lambda: [0, 0.0, 0.0, 0])  # n, sum frac, solo saving, nonpy
for cid, r in sel.items():
    s = {n for w in r["workers"] for n in w}
    if len(s) > 1500:
        continue
    hit = s & set(T)
    if not hit:
        continue
    ch = changed(cid)
    eligible = "diffexec" in r.get("reason", "")
    now = c(s)
    for n in hit:
        name, fr, tot, outside, tcpu = T[n]
        if ans and n in ans[cid]["failing"]:
            continue  # must stay
        reached = [f for f in fr if f[4] & ch]
        frac = sum(f[3] for f in reached) / tot
        if outside & ch:
            frac = 1.0
        saving = now - c(s - {n})
        a = agg[name]
        a[0] += 1
        a[1] += frac
        a[2] += saving
        a[3] += eligible
        rows.append((cid, name, round(frac, 3), len(reached), len(fr), round(saving, 2), eligible))
print(f"{pdir.name}: per test (passing selections only if labeled): n, mean reached CPU frac, solo saving s, diffexec-eligible")
for name, (n, fs, sv, el) in sorted(agg.items(), key=lambda x: -x[1][2]):
    print(f"   {name:40s} n={n:4d} reach={fs/n:5.2f} saving={sv:7.1f}s eligible={el}")
# saving weighted by (1 - frac) as a rough "cheap to verify" share
cheap = sum(r[5] for r in rows if r[2] <= 0.25)
el_cheap = sum(r[5] for r in rows if r[2] <= 0.25 and r[6])
print(f"   saving where reach<=25%: {cheap:.1f}s (diffexec-eligible part {el_cheap:.1f}s); total {sum(r[5] for r in rows):.1f}s; n zero-reach {sum(1 for r in rows if r[3]==0)}")
json.dump(rows, open(f"/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m06/reach_{pdir.name}.json", "w"))
