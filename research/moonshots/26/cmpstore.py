"""Compare two subtest.zip stores: every member byte-equal except meta.pkl, whose queries must
match outside the timing fields (cpu_*, calib); timing fields are summarised as ratios.

usage: python3 cmpstore.py A.zip B.zip
"""
import pickle
import statistics
import sys
import zipfile

TIMING = {"cpu_exec", "cpu_opt", "calib", "cpu_stage", "cpu_from", "gen_cpu"}


def strip(o, path, ratios, other):
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if k in TIMING:
                ov = other.get(k) if isinstance(other, dict) else None
                if isinstance(v, (int, float)) and isinstance(ov, (int, float)) and v > 0.01:
                    ratios.setdefault(k, []).append(ov / v)
                elif isinstance(v, dict) and isinstance(ov, dict):
                    for kk in v:
                        if v[kk] > 0.01 and kk in ov:
                            ratios.setdefault(k, []).append(ov[kk] / v[kk])
                continue
            out[k] = strip(v, path + [k], ratios, other.get(k) if isinstance(other, dict) else None)
        return out
    if isinstance(o, list):
        return [strip(v, path + [i], ratios, other[i] if isinstance(other, list) and i < len(other) else None)
                for i, v in enumerate(o)]
    return o


a, b = zipfile.ZipFile(sys.argv[1]), zipfile.ZipFile(sys.argv[2])
na, nb = set(a.namelist()), set(b.namelist())
print(f"members: {len(na)} vs {len(nb)}; only A {len(na - nb)}, only B {len(nb - na)}")
diff = [n for n in sorted(na & nb) if n != "meta.pkl" and a.read(n) != b.read(n)]
print(f"non-meta members differing: {len(diff)} {diff[:5]}")
ma, mb = pickle.loads(a.read("meta.pkl")), pickle.loads(b.read("meta.pkl"))
ratios = {}
sa = strip(ma, [], ratios, mb)
sb = strip(mb, [], {}, ma)
print("meta (timing stripped) equal:", sa == sb)
if sa != sb:
    for s in sa["suites"]:
        for i, (x, y) in enumerate(zip(sa["suites"][s], sb["suites"][s])):
            if x != y:
                keys = [k for k in set(x) | set(y) if x.get(k) != y.get(k)]
                sub = [k for k in set(x.get("outs", {})) | set(y.get("outs", {}))
                       if x.get("outs", {}).get(k) != y.get("outs", {}).get(k)]
                print(f"  {s} {i}: {keys} outs:{sub}")
    for k in sa:
        if k != "suites" and sa[k] != sb[k]:
            print("  top-level differs:", k)
for k, r in ratios.items():
    print(f"timing {k}: n={len(r)} median B/A {statistics.median(r):.3f}  p10 {sorted(r)[len(r) // 10]:.3f}"
          f"  p90 {sorted(r)[9 * len(r) // 10]:.3f}")
