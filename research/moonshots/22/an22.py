"""Compare rule checkpoints off (0 = moon-05) vs on (1): an22.py OUTDIR name:dir ...
Prints sim cost, misses, subtest drops / child CPU, and the commits newly dropped."""
import json, os, re, sys
O = sys.argv[1]
DROP = re.compile(r"^\[(\w+)\].*subtest: (\[[\d, ]*\]) dropped \((\d+) queries re-run, ([\d.]+)s\)", re.M)
KEEP = re.compile(r"^\[(\w+)\].*subtest: keep \((?:verification of \d+ queries failed, ([\d.]+)s|est)", re.M)
for arg in sys.argv[2:]:
    n, d = arg.split(":", 1)
    if not os.path.exists(f"{O}/{n}.1.out"):
        continue
    T = {t["number"]: t["seconds"] for t in json.load(open(d + "/tests.json"))["tests"]}
    ap = d + "/answers.json"
    A = json.load(open(ap))["commits"] if os.path.exists(ap) else {}
    res = {}
    for m in "01":
        tot, miss, per = 0.0, [], {}
        for l in open(f"{O}/{n}.{m}.out"):
            r = json.loads(l)
            ws = [w for w in r["workers"] if w]
            c = (max(sum(T[x] for x in w) for w in ws) + 2 * len(ws)) if ws else 0
            sel = {x for w in ws for x in w}
            if r["commit"] in A and set(A[r["commit"]]["failing"]) - sel:
                miss.append(r["commit"])
                c += 300
            per[r["commit"]] = c
            tot += c
        err = open(f"{O}/{n}.{m}.err").read()
        drops = {c: (t, float(s)) for c, t, _, s in DROP.findall(err)}
        keeps = {c: float(s or 0) for c, s in KEEP.findall(err)}
        child = sum(s for _, s in drops.values()) + sum(keeps.values())
        res[m] = (tot, miss, child, drops, per, keeps)
    a, b = res["0"], res["1"]
    new = sorted(set(b[3]) - set(a[3]))
    lost = sorted(set(a[3]) - set(b[3]))
    both = set(a[3]) & set(b[3])
    print(f"{n}: commits {len(a[4])}; sim {a[0]:.1f} -> {b[0]:.1f} (d {b[0]-a[0]:+.1f}); misses {a[1]} -> {b[1]}; "
          f"subtest child cpu {a[2]:.1f} -> {b[2]:.1f}; drops {len(a[3])} -> {len(b[3])} (+{len(new)} -{len(lost)})")
    if both:
        ca = sum(a[3][c][1] for c in both); cb = sum(b[3][c][1] for c in both)
        print(f"  child cpu on the {len(both)} commits dropped in both: {ca:.2f} -> {cb:.2f}s")
    kb = set(a[5]) & set(b[5])
    if kb:
        print(f"  child cpu on the {len(kb)} commits kept (verify failed / est) in both: "
              f"{sum(a[5][c] for c in kb):.2f} -> {sum(b[5][c] for c in kb):.2f}s")
    print("  new drops:", " ".join(f"{c}:{b[3][c][0]}:{b[3][c][1]}" for c in new))
    if lost:
        print("  LOST drops:", lost)
    worse = [(k, a[4][k], b[4][k]) for k in a[4] if b[4].get(k, 0) > a[4][k] + 0.01]
    if worse:
        print("  worse:", worse[:10])
