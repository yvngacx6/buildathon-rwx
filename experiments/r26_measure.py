"""r26: measure the real CPU of re-running TPC queries in one forked child (no swap).

usage (in the image): python -m experiments.r26_measure [suite] [opt]
"""
import contextlib
import io
import marshal
import os
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, "/app")
from selector import subtest as st  # noqa: E402

root = "/opt/sqlglot"
store = Path("/app/index/subtest.zip")
with zipfile.ZipFile(store) as zf:
    import pickle
    meta = pickle.loads(zf.read("meta.pkl"))
suite = sys.argv[1] if len(sys.argv) > 1 else "tpcds"
opt = len(sys.argv) > 2 and sys.argv[2] == "opt"
qs = meta["suites"][suite]
todo = [i for i, q in enumerate(qs) if (q["execute"] or opt)]
est = sum((q["cpu_opt"] if opt else q["cpu_exec"]) for i, q in enumerate(qs) if i in todo)
print(f"{suite} opt={opt}: {len(todo)} queries, build estimate {est:.2f}s", flush=True)
t0 = time.process_time()
st._warm(root)
print(f"warm (parent imports) {time.process_time() - t0:.2f}s", flush=True)

pid = os.fork()
if pid == 0:
    sys.setrecursionlimit(1000)
    from sqlglot.executor.table import Table
    from sqlglot import find_tables, parse_one
    c0 = time.process_time()
    tables = {}
    per = []
    with zipfile.ZipFile(store) as zf:
        for i in todo:
            q = qs[i]
            a = time.process_time()
            tabs = {}
            if q["execute"] and not opt:
                for t in find_tables(parse_one(q["sql"])):
                    key = (suite, t.name)
                    if key not in tables:
                        cols, rows = marshal.loads(zf.read(f"{suite}/{t.name}"))
                        tables[key] = Table(columns=cols, rows=rows)
                    tabs[t.name] = tables[key]
            b = time.process_time()
            with contextlib.redirect_stdout(io.StringIO()):
                got = st.run_query(q, meta["schemas"][suite], tabs, opt)
            c = time.process_time()
            want = (q["outs"].get("exec") if not opt else None,
                    (q["outs"]["duck"] if (q["execute"] or opt) else None),
                    q["outs"].get("opt") if opt else None)
            per.append((i, b - a, c - b, q["cpu_opt"] if opt else q["cpu_exec"], got == want))
    tot = time.process_time() - c0
    load = sum(p[1] for p in per)
    run = sum(p[2] for p in per)
    bad = [p[0] for p in per if not p[4]]
    print(f"child total {tot:.2f}s (load tables {load:.2f}, queries {run:.2f}); mismatches {bad}")
    for p in sorted(per, key=lambda p: -p[2])[:60]:
        print(f"  q{p[0]}: load {p[1]:.3f} run {p[2]:.3f} est {p[3]:.3f}")
    sys.stdout.flush()
    os._exit(0)
t1 = os.times()
os.waitpid(pid, 0)
t2 = os.times()
print(f"child os.times CPU {(t2.children_user - t1.children_user) + (t2.children_system - t1.children_system):.2f}s")
