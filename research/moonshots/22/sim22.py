"""Offline: per optb line of the TPC execute queries, CPU to verify a change on it.
moon-05 (optimize from the post-parse tree) vs moon-22 (from the last usable rule boundary).
usage: sim22.py STORE.zip"""
import collections, pickle, sys, zipfile
zf = zipfile.ZipFile(sys.argv[1])
meta = pickle.loads(zf.read("meta.pkl"))
print("meta.pkl bytes", len(zf.read("meta.pkl")), "ck members", sum(n.startswith("ck/") for n in zf.namelist()),
      "ck bytes", sum(i.file_size for i in zf.infolist() if i.filename.startswith("ck/")),
      "zip compressed", sum(i.compress_size for i in zf.infolist()))
BUD = 6.0
for suite, qs in meta["suites"].items():
    m05, m22 = collections.Counter(), collections.Counter()
    nok = nq = 0
    steps_cpu = collections.Counter()
    kdist = collections.Counter()
    for q in qs:
        o = q.get("outs", {})
        if not q.get("execute") or not o.get("d_opt"):
            continue
        nq += 1
        fs = o.get("fs")
        ok = o.get("ck_ok") or []
        nok += bool(fs and ok)
        for s, c in (o.get("cpu_steps") or {}).items():
            steps_cpu[s] += c
        for f, ls in q["phases"]["win"]["optb"].items():
            idx = dict(zip(*fs[f])) if fs and f in fs else {}
            for ln in ls:
                m05[(f, ln)] += o["cpu_stage"]
                first = idx.get(ln, 0) - 1 if idx else -1
                k = max((b for b in ok if b <= first), default=0) if first > 0 else 0
                kdist[k] += 1
                m22[(f, ln)] += o["cpu_from"][k] if k else o["cpu_stage"]
    L = list(m05)
    if not L:
        continue
    print(f"== {suite}: {nq} executed queries with d_opt, {nok} with rule checkpoints; optb lines {len(L)}")
    print("  per-step CPU (sum over queries):", {k: round(v, 2) for k, v in sorted(steps_cpu.items())})
    print("  stage CPU sum", round(sum(q['outs'].get('cpu_stage', 0) for q in qs if q.get('execute')), 2))
    print("  (line,query) start boundary distribution:", dict(sorted(kdist.items())))
    for name, c in (("moon-05", m05), ("moon-22", m22)):
        okn = sum(1 for x in L if c[x] <= BUD)
        print(f"  {name}: mean per-line verify CPU {sum(c.values())/len(L):.3f}s, median "
              f"{sorted(c.values())[len(L)//2]:.3f}s, <= {BUD}s: {okn}/{len(L)}")
    byf = collections.defaultdict(lambda: [0, 0.0, 0.0])
    for x in L:
        b = byf[x[0]]; b[0] += 1; b[1] += m05[x]; b[2] += m22[x]
    for f, b in sorted(byf.items(), key=lambda t: -t[1][1])[:30]:
        print(f"    {f:50s} n={b[0]:5d} m05={b[1]/b[0]:.3f} m22={b[2]/b[0]:.3f} x{b[1]/max(b[2],1e-9):.1f}")
