"""r26: where genmem's all-dialect child spends its CPU (pinned source, from .pyc)."""
import gc
import sys
import time
import tracemalloc

sys.path.insert(0, "/opt/sqlglot")
t0 = time.process_time()
from sqlglot.dialects.dialect import Dialect, Dialects  # noqa: E402
t1 = time.process_time()
imp = loops_w = loops_m = 0.0
for name in Dialects:
    a = time.process_time()
    d = Dialect.get_or_raise(name)
    b = time.process_time()
    for _ in range(100):
        d.generator()
    gc.collect()
    c = time.process_time()
    tracemalloc.start()
    for _ in range(1000):
        d.generator()
    gc.collect()
    tracemalloc.stop()
    e = time.process_time()
    imp += b - a
    loops_w += c - b
    loops_m += e - c
print(f"base import {t1 - t0:.2f}  dialect loads {imp:.2f}  warmup {loops_w:.2f}  measured {loops_m:.2f}  n={len(Dialects)}")
