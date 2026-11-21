"""Compare gap.py / pq JSON outputs: cmp.py OLD.json NEW.json"""
import json, sys

def rows(p):
    d = json.load(open(p))
    if "rows" in d or any(isinstance(v, dict) and "rows" in v for v in d.values()):
        rs = d["rows"] if "rows" in d else [r for v in d.values() for r in v["rows"]]
        return {r["commit"]: (r["ours"], len(r["missed"]), r["reason"]) for r in rs}
    return {k: (v["cost"], 0, v["reason"]) for k, v in d.items()}

a, b = rows(sys.argv[1]), rows(sys.argv[2])
ta = sum(v[0] for v in a.values()); tb = sum(v[0] for v in b.values())
ma = sum(v[1] > 0 for v in a.values()); mb = sum(v[1] > 0 for v in b.values())
print(f"{sys.argv[2]}: {len(b)} commits  old {ta:.1f}s  new {tb:.1f}s  delta {tb - ta:+.1f}s  misses {ma}->{mb}")
for k in sorted(b, key=lambda k: b[k][0] - a.get(k, (0,))[0]):
    d = b[k][0] - a[k][0]
    if abs(d) > 0.05 or b[k][1] != a[k][1]:
        print(f"  {k:28s} {a[k][0]:7.1f} -> {b[k][0]:7.1f} ({d:+.1f}) miss {a[k][1]}->{b[k][1]}  {b[k][2][:110]}")
