import json
from pathlib import Path
S = Path("/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m06")
drops = json.load(open(S / "drops.json"))
hits = {s: {k: {tuple(x) for x in v} for k, v in json.load(open(S / f"hits_{s}.json")).items()} for s in (0, 1, 2)}
for s in (0, 1, 2):
    print("seed", s, "tests hitting any target:", len(hits[s]))
bad = []
for cid, v in drops.items():
    lines = {tuple(x) for x in v["lines"]}
    for t in v["dropped"]:
        for s in (0, 1, 2):
            if hits[s].get(t, set()) & lines:
                bad.append((cid, t, s))
print("dropped (commit,test) pairs that hit a changed line in some seed:", len(bad))
by = {}
for cid, t, s in bad:
    by.setdefault((cid, t), []).append(s)
for k, v in list(by.items())[:40]:
    print("  ", k, "seeds", v)
# seed disagreement overall on target lines
allt = set().union(*[set(h) for h in hits.values()])
dis = [t for t in allt if not (hits[0].get(t) == hits[1].get(t) == hits[2].get(t))]
print("tests whose target-line hits differ across seeds:", len(dis), dis[:10])
