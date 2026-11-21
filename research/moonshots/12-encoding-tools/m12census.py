import sys, os, pickle, collections
ROOT = os.path.realpath(os.environ["DIFFEXEC_ROOT"]) + "/sqlglot/"
mon = sys.monitoring; TOOL = 3
cnt = collections.Counter(); cur = ["IMPORT"]
def _start(code, off):
    if not code.co_filename.startswith(ROOT):
        return mon.DISABLE
    cnt[(code.co_filename[len(ROOT)-8:], code.co_qualname, code.co_firstlineno, cur[0])] += 1
mon.use_tool_id(TOOL, "m12"); mon.register_callback(TOOL, mon.events.PY_START, _start); mon.set_events(TOOL, mon.events.PY_START)
import pytest
from concurrent.futures import Future
import concurrent.futures, concurrent.futures.process
class SyncPool:
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer: initializer(*initargs)
    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs))); f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e: f.set_exception(e)
        return f
    def shutdown(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
concurrent.futures.ProcessPoolExecutor = SyncPool; concurrent.futures.process.ProcessPoolExecutor = SyncPool
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    cur[0] = item.nodeid; yield; cur[0] = "OTHER"
def pytest_sessionfinish(session):
    mon.set_events(TOOL, 0)
    pickle.dump(dict(cnt), open(os.environ["M12_OUT"], "wb"))
