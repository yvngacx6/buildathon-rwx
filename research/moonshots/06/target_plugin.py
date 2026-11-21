"""pytest plugin: which tests execute any of the target lines (M06_TARGETS json [[relpath, line],...]).
Writes {nodeid: [[relpath, line], ...]} to M06_OUT. Pool work is NOT inlined (spawned workers
load this plugin? no) -> we patch ProcessPoolExecutor to a sync pool as in frontier_plugin."""
import json, os, sys
from concurrent.futures import Future

M = sys.monitoring
TOOL = 4
ROOT = os.path.realpath(os.getcwd()) + os.sep
targets = {}
for p, ln in json.load(open(os.environ["M06_TARGETS"])):
    targets.setdefault(ROOT + p, set()).add(ln)
cur = [None]
hits = {}


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


def line(code, ln):
    t = targets.get(code.co_filename)
    if t is None or ln not in t:
        return M.DISABLE
    if cur[0] is not None:
        hits.setdefault(cur[0], set()).add((code.co_filename[len(ROOT):], ln))


def pytest_configure(config):
    M.use_tool_id(TOOL, "m06t")
    M.register_callback(TOOL, M.events.LINE, line)
    M.set_events(TOOL, M.events.LINE)


def pytest_collection_modifyitems(session, config, items):
    for mod in list(sys.modules.values()):
        if getattr(mod, "__name__", "").startswith("tests.") and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = SyncPool


def pytest_runtest_setup(item):
    cur[0] = item.nodeid


def pytest_runtest_teardown(item, nextitem):
    pass


def pytest_unconfigure(config):
    M.set_events(TOOL, 0)
    json.dump({k: sorted(v) for k, v in hits.items()}, open(os.environ["M06_OUT"], "w"))
