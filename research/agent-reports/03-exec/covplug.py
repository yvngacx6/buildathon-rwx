# pytest plugin: per-test line coverage of /opt/sqlglot/{sqlglot,tests} via sys.monitoring + file opens via audit hook
import sys, os, json
ROOT = "/opt/sqlglot/"
mon = sys.monitoring
TOOL = 4
cur = {"key": "IMPORT", "lines": set(), "opens": set(), "types": set()}
data = {}  # nodeid -> (lines, opens)
setup_lines = {}  # nodeid -> lines seen during setup (class fixtures)

def _line(code, line):
    fn = code.co_filename
    if ROOT is None or fn is None: return
    if fn.startswith(ROOT):
        cur["lines"].add((fn[len(ROOT):], line))
    return mon.DISABLE

def _audit(ev, args):
    if ROOT is not None and ev == "open" and args and isinstance(args[0], str) and args[0].startswith(ROOT):
        cur["opens"].add(args[0][len(ROOT):])

mon.use_tool_id(TOOL, "covmap")
mon.register_callback(TOOL, mon.events.LINE, _line)
mon.set_events(TOOL, mon.events.LINE)
sys.addaudithook(_audit)
import pytest, pickle
from concurrent.futures import Future
class SyncPool:
    """In-process stand-in for tests' spawn ProcessPoolExecutor so lines executed by pool jobs
    (incl. pickling of args/results, which goes through sqlglot serde) are attributed to the test."""
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer: initializer(*initargs)
    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f
    def map(self, fn, *its): return [self.submit(fn, *x).result() for x in zip(*its)]
    def shutdown(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
import concurrent.futures, concurrent.futures.process
concurrent.futures.ProcessPoolExecutor = SyncPool
concurrent.futures.process.ProcessPoolExecutor = SyncPool

def _switch(key):
    store = data.setdefault(cur["key"], [set(), set(), set()])
    store[0] |= cur["lines"]; store[1] |= cur["opens"]
    store[2] |= cur["types"]
    cur["key"] = key; cur["lines"] = set(); cur["opens"] = set(); cur["types"] = set()
    mon.restart_events()

def _wrap_init(cls):
    orig = cls.__init__
    def __init__(self, *a, **k):
        cur["types"].add(type(self))
        return orig(self, *a, **k)
    cls.__init__ = __init__

def pytest_configure(config):
    import sqlglot
    from sqlglot import exp, tokens, parser, generator
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.optimizer.annotate_types import TypeAnnotator
    for c in (exp.Expr, tokens.Tokenizer, parser.Parser, generator.Generator, Dialect, TypeAnnotator):
        _wrap_init(c)

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _switch(("SETUP", item.nodeid)); yield; _switch(("CALL", item.nodeid))

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _switch(("CALL", item.nodeid)); yield; _switch("OTHER")

def pytest_sessionfinish(session):
    _switch("END")
    out = {}
    for k, v in data.items():
        l, o = v[0], v[1]; ty = v[2]
        key = k if isinstance(k, str) else k[0] + "|" + k[1]
        mro = sorted({c.__module__ + ":" + c.__qualname__ for t in ty for c in t.__mro__ if c.__module__.startswith("sqlglot")})
        out[key] = [sorted(l), sorted(o), mro]
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(f"/out/cov_{wid}.json", "w") as f:
        json.dump(out, f)
