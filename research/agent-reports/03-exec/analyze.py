import json, pickle, sys
sys.path.insert(0, ".")
from select_cands import Selector
B = "../../../sample/"
tests = json.load(open(B + "tests.json"))["tests"]; secs = {t["number"]: t["seconds"] for t in tests}
ans = json.load(open(B + "answers.json"))["commits"]
sel = Selector(pickle.load(open(sys.argv[1], "rb")), tests, sys.argv[2])
tot = 0
for cid, a in sorted(ans.items()):
    c, notes = sel.candidates(open(B + f"commits/{cid}.patch").read())
    fail = set(a["failing"]); miss = fail - c
    T = sum(secs[i] for i in c); tot += T
    print(f"{cid} {a['kind']:8} fail={len(fail):2} cand={len(c):4} T={T:7.2f} miss={len(miss)} {' '.join(n for n in notes if "level" in n)[:120]}")
print("total cand secs", round(tot, 1))
