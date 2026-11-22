"""Why are rule checkpoints unusable? Compare boundary digests across seeds and from-checkpoint runs."""
import os, pickle, subprocess, sys, zipfile
from pathlib import Path
sys.path.insert(0, "/w/repo")
from selector import subtest as S
root = Path("/shared/sqlglot")
runs = {}
for seed in ("0", "1", "2"):
    out = f"/tmp/sd{seed}.pkl"
    subprocess.run([sys.executable, "-m", "selector.subtest", "seeddig", str(root), out], cwd="/w/repo",
                   env=dict(os.environ, PYTHONHASHSEED=seed), check=True)
    runs[seed] = pickle.load(open(out, "rb"))
keys = [k for k in runs["0"] if k[0] == "tpcds"]
import collections
first_diff = collections.Counter()
for key in keys:
    a = runs["0"][key]
    if a is None:
        first_diff["none"] += 1
        continue
    ks = [k for k in range(1, 15) if any(runs[s][key] is None or runs[s][key][0].get(k) != a[0][k] for s in "12")]
    fin = any(runs[s][key] is None or runs[s][key][1] != a[1] for s in "12")
    first_diff[(ks[0] if ks else None, fin)] += 1
print("tpcds (first seed-varying boundary, final varies):", dict(first_diff))
# from-checkpoint usability under seed 0 for a few queries
sys.path.insert(0, str(root))
sys.setrecursionlimit(1000)
from tests.helpers import TPCDS_SCHEMA
from sqlglot import parse_one
import sqlglot.executor as E
meta = pickle.loads(zipfile.ZipFile("/w/repo/index/subtest.zip").read("meta.pkl"))
qs = meta["suites"]["tpcds"]
res = collections.Counter()
for i, q in enumerate(qs[:40]):
    if not q.get("execute") or not q["outs"].get("d_opt"):
        continue
    e = pickle.loads(q["outs"]["pexpr"])
    sch = E.ensure_schema(TPCDS_SCHEMA, dialect=None)
    _, cks, digs, dfin, _ = S.rule_steps(e, sch, keep=True)
    row = []
    for k in range(1, 15):
        try:
            oe = S.from_checkpoint(cks[k], TPCDS_SCHEMA, k)
            row.append("ok" if S.strict_digest(oe) == dfin else "diff")
            if S.strict_digest(pickle.loads(cks[k])) != digs[k]:
                row[-1] += "/rt"
        except Exception as ex:
            row.append(type(ex).__name__)
    print(i, dfin == q["outs"]["d_opt"], row)
