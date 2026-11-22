"""Compare stage off (0) vs on (1) runs fetched from EC2 into ec2out/."""
import json, os, re, sys
D = os.path.dirname(os.path.abspath(__file__))
O = f"{D}/ec2out"
SETS = {"R": f"{D}/R", "practice_like": f"{D}/pl120", "sample": f"{D}/sample", "private": f"{D}/private"}
for n, d in SETS.items():
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
        cpu = re.findall(r"^cpu ([\d.]+)s", err, re.M)
        child = sum(float(x) for x in re.findall(r"subtest: (?:\[[\d, ]*\] dropped \(\d+ queries re-run, |keep \(verification of \d+ queries failed, )([\d.]+)s", err))
        drops = len(re.findall(r"subtest: \[[\d, ]*\] dropped", err))
        res[m] = (tot, miss, cpu[-1] if cpu else "?", child, drops, per)
    a, b = res["0"], res["1"]
    print(f"{n}: sim {a[0]:.1f} -> {b[0]:.1f} (d {b[0]-a[0]:+.1f}); misses {a[1]} -> {b[1]}; selector cpu {a[2]} -> {b[2]}; "
          f"subtest child cpu {a[3]:.1f} -> {b[3]:.1f}; subtest drops {a[4]} -> {b[4]}")
    worse = [(k, a[5][k], b[5][k]) for k in a[5] if b[5].get(k, 0) > a[5][k] + 0.01]
    if worse:
        print("  worse:", worse[:10])
