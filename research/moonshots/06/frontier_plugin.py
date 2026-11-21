"""pytest plugin: per test, the frontier calls (test-code frame -> sqlglot function) with
their sqlglot line coverage and CPU. Pools are made synchronous so the work is in-process.

env: M06_OUT=path.pkl
"""
import os, pickle, sys, time
from concurrent.futures import Future

M = sys.monitoring
TOOL = 4
SQ = os.sep + "sqlglot" + os.sep + "sqlglot" + os.sep
TS = os.sep + "sqlglot" + os.sep + "tests" + os.sep


class SyncPool:
    def __init__(self, *a, **k):
        pass

    def submit(self, fn, *a, **k):
        f = Future()
        try:
            f.set_result(fn(*a, **k))
        except BaseException as e:
            f.set_exception(e)
        return f

    def map(self, fn, *its, **k):
        return list(map(fn, *its))

    def shutdown(self, *a, **k):
        pass


class Rec:
    def __init__(self):
        self.reset(None)
        self.out = {}

    def reset(self, nodeid):
        self.nodeid = nodeid
        self.frontiers = []  # [test_file, test_line, qualname, cpu, lines(set)]
        self.cur = None  # active frontier frame
        self.cur_lines = None
        self.cur_t0 = 0.0
        self.outside = set()  # sqlglot lines run outside any frontier while the test runs
        self.t0 = time.process_time()

    def py_start(self, code, off):
        if self.nodeid is None or self.cur is not None:
            return
        fn = code.co_filename
        if SQ not in fn:
            return
        f = sys._getframe(1)
        b = f.f_back
        if b is None or TS not in b.f_code.co_filename:
            return
        self.cur = f
        self.cur_lines = set()
        self.cur_t0 = time.process_time()
        self.frontiers.append([b.f_code.co_filename.split(TS)[-1], b.f_lineno,
                               fn.split(SQ)[-1] + ":" + code.co_qualname, 0.0, self.cur_lines])

    def py_end(self, code, off, *rest):
        if self.cur is None:
            return
        if sys._getframe(1) is self.cur:
            self.frontiers[-1][3] = time.process_time() - self.cur_t0
            self.cur = None
            self.cur_lines = None

    def line(self, code, ln):
        if self.nodeid is None:
            return
        fn = code.co_filename
        if SQ not in fn:
            return
        key = (fn.split(SQ)[-1], ln)
        if self.cur_lines is not None:
            self.cur_lines.add(key)
        else:
            self.outside.add(key)

    def finish(self):
        total = time.process_time() - self.t0
        self.out[self.nodeid] = {"total_cpu": total, "frontiers": self.frontiers,
                                 "outside": self.outside}
        self.nodeid = None


R = Rec()


def pytest_configure(config):
    M.use_tool_id(TOOL, "m06")
    M.register_callback(TOOL, M.events.PY_START, R.py_start)
    M.register_callback(TOOL, M.events.PY_RETURN, R.py_end)
    M.register_callback(TOOL, M.events.PY_UNWIND, R.py_end)
    M.register_callback(TOOL, M.events.LINE, R.line)
    M.set_events(TOOL, M.events.PY_START | M.events.PY_RETURN | M.events.PY_UNWIND | M.events.LINE)


def pytest_collection_modifyitems(session, config, items):
    for mod in list(sys.modules.values()):
        if getattr(mod, "__name__", "").startswith("tests.") and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = SyncPool


def pytest_runtest_setup(item):
    R.reset(item.nodeid)


def pytest_runtest_teardown(item, nextitem):
    R.finish()


def pytest_unconfigure(config):
    M.set_events(TOOL, 0)
    with open(os.environ["M06_OUT"], "wb") as f:
        pickle.dump(R.out, f)
