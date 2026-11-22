"""Why does the generate replay reject a patch? dbg25.py SQLGLOT STORE PATCH: swaps the patched
code in (as the run-time child) and reports, per eligible query, output / state mismatches."""
import pickle, sys, zipfile
sys.path.insert(0, "/w/repo")
from selector import diff, subtest as st

root, store, patch = sys.argv[1:4]
jobs = []
for fp in diff.parse(open(patch).read()):
    old = open(f"{root}/{fp.path}").read()
    jobs.append((fp.path, diff.apply(old, fp), None))
st._warm(root)
print("swap:", st._swap(root, jobs))
import sqlglot.executor as E
meta = pickle.loads(zipfile.ZipFile(store).read("meta.pkl"))
n_out = n_state = n_ok = 0
for i, q in enumerate(meta["suites"]["tpcds"]):
    g = q["outs"].get("gen")
    if not g:
        continue
    calls, state = pickle.loads(g)
    py = E.PythonExecutor().generator
    bad = 0
    for kind, fz, d_in, depth, out in calls:
        e = pickle.loads(fz)
        got = py.generate(e) if kind == "py" else e.sql()
        bad += got != out
    s_bad = st.gen_state(py) != state
    n_out += bool(bad)
    n_state += s_bad and not bad
    n_ok += not bad and not s_bad
print(f"queries: outputs differ {n_out}, only state differs {n_state}, identical {n_ok}")
