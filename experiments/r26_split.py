"""r26: CPU split of the TPC executor queries: parse / optimize (up to Plan) / plan+run / duckdb."""
import marshal
import os
import pickle
import sys
import time
import zipfile

sys.path.insert(0, "/app")
sys.path.insert(0, "/opt/sqlglot")
sys.setrecursionlimit(1000)
import sqlglot.executor as ex  # noqa: E402
from sqlglot import find_tables, parse_one, transpile  # noqa: E402
from sqlglot.executor.table import Table  # noqa: E402

suite = sys.argv[1] if len(sys.argv) > 1 else "tpcds"
with zipfile.ZipFile("/app/index/subtest.zip") as zf:
    meta = pickle.loads(zf.read("meta.pkl"))
    raw = {n.split("/", 1)[1]: marshal.loads(zf.read(n)) for n in zf.namelist() if n.startswith(suite + "/")}
tables = {k: Table(columns=c, rows=r) for k, (c, r) in raw.items()}
schema = meta["schemas"][suite]
RealPlan = ex.Plan
mark = {}


def hook(e):
    mark["o"] = time.process_time()
    return RealPlan(e)


ex.Plan = hook
tot = {"parse": 0.0, "opt": 0.0, "run": 0.0, "duck": 0.0}
for q in meta["suites"][suite]:
    if not q["execute"]:
        continue
    t0 = time.process_time()
    e2 = pickle.loads(pickle.dumps(parse_one(q["sql"])))
    t1 = time.process_time()
    used = {t.name: tables[t.name] for t in find_tables(e2)}
    res = ex.execute(e2, schema=schema, tables=used)
    pickle.loads(pickle.dumps(res))
    t2 = time.process_time()
    transpile(q["sql"], write="duckdb")
    t3 = time.process_time()
    tot["parse"] += t1 - t0
    tot["opt"] += mark["o"] - t1
    tot["run"] += t2 - mark["o"]
    tot["duck"] += t3 - t2
print(suite, {k: round(v, 2) for k, v in tot.items()})
