import time, sys, os
os.environ["SKIP_INTEGRATION"] = "0"
t = time.process_time()
import tests.test_executor as te
from sqlglot import parse_one
t1 = time.process_time(); te.TestExecutor.setUpClass(); t2 = time.process_time()
print(f"import {t1-t:.2f} setUpClass {t2-t1:.2f}", file=sys.stderr)
te.initializer(te.TPCDS_SCHEMA, te.TestExecutor.tpcds_tables)
ts = []
for i, (meta, sql, _) in enumerate(te.TestExecutor.tpcds_sqls):
    a = time.process_time(); r = te.mp_execute(parse_one(sql), meta); ts.append((time.process_time() - a, i, r is not None))
ts.sort(reverse=True)
print("n", len(ts), "executed", sum(x[2] for x in ts), "total", round(sum(x[0] for x in ts), 2), "top", [(round(a, 2), i) for a, i, _ in ts[:8]], file=sys.stderr)
