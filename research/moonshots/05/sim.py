"""Per line run by test_execute_tpcds: CPU to verify a change on that line,
current (whole query re-run) vs incremental (only stages that run the line, from checkpoints)."""
import pickle, os, collections
D = os.path.dirname(os.path.abspath(__file__))
cpu = pickle.load(open(f"{D}/stageprof.pkl", "rb"))["tpcds"]
cov = pickle.load(open(f"{D}/stageprof_cov.pkl", "rb"))["tpcds"]
full = collections.Counter()
inc = collections.Counter()
nq = collections.Counter()
stage_of = collections.defaultdict(set)
for qc, qv in zip(cpu, cov):
    if not qc or not qv:
        continue
    st_cpu = [dt for _, dt, _ in qc[0]]
    tot = sum(st_cpu)
    lines_q = set()
    per_line_inc = collections.Counter()
    for (name, _, c), dt in zip(qv[0], st_cpu):
        ls = {(f, l) for f, ll in c.items() for l in ll if f.startswith("sqlglot/")}
        for L in ls:
            per_line_inc[L] += dt
            stage_of[L].add(name.split(":")[0] if not name.startswith("step") else "exec")
        lines_q |= ls
    for L in lines_q:
        full[L] += tot
        inc[L] += per_line_inc[L]
        nq[L] += 1
lines = list(full)
print("lines run by tpcds queries:", len(lines))
BUD = 6.0  # laptop CPU (12 s scorer at 2x)
for name, c in (("full", full), ("inc", inc)):
    ok = sum(1 for L in lines if c[L] <= BUD)
    print(f"{name}: lines verifiable within {BUD}s laptop CPU: {ok}/{len(lines)} ({ok/len(lines):.0%});"
          f" mean cpu {sum(c.values())/len(lines):.2f}s; median {sorted(c.values())[len(lines)//2]:.2f}")
# by stage class of the line
grp = collections.defaultdict(list)
for L in lines:
    s = stage_of[L]
    key = "exec-only" if s <= {"exec", "result"} else ("parse/opt/plan only" if not (s & {"exec"}) else "mixed")
    grp[key].append(L)
for k, ls in grp.items():
    okf = sum(1 for L in ls if full[L] <= BUD)
    oki = sum(1 for L in ls if inc[L] <= BUD)
    print(k, len(ls), "full ok", okf, "inc ok", oki)
# by file
byf = collections.defaultdict(lambda: [0, 0, 0])
for L in lines:
    b = byf[L[0]]
    b[0] += 1
    b[1] += full[L] <= BUD
    b[2] += inc[L] <= BUD
for f, b in sorted(byf.items(), key=lambda x: -x[1][0])[:25]:
    print(f"{f:55s} n={b[0]:5d} full_ok={b[1]:5d} inc_ok={b[2]:5d}")
pickle.dump({"full": full, "inc": inc, "nq": nq, "stage_of": dict(stage_of)}, open(f"{D}/sim.pkl", "wb"))
