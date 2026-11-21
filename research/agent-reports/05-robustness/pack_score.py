"""Pack proto_select selections (LPT, best worker count <=8) into output jsonl for score.py. usage: pack_score.py tests.json patchdir out.jsonl"""
import json, sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).parent))
from proto_select import select
tests = json.load(open(sys.argv[1]))["tests"]; num = {t["id"]: t["number"] for t in tests}; ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
def pack(nums):
    best = None
    for k in range(1, 9):
        W = [[] for _ in range(k)]; L = [0] * k
        for n in sorted(nums, key=lambda n: -ms[n]): i = L.index(min(L)); W[i].append(n); L[i] += ms[n]
        c = max(L) + 2000 * k
        if best is None or c < best[0]: best = (c, W)
    return best[1] if nums else []
with open(sys.argv[3], "w") as o:
    for p in sorted(pathlib.Path(sys.argv[2]).glob("*.patch")):
        sel, _ = select(p.read_text(), set(num))
        o.write(json.dumps({"commit": p.stem, "workers": pack([num[t] for t in sel])}) + "\n")
