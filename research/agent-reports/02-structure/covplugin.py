"""pytest plugin: run ProcessPoolExecutor work inline so per-test coverage contexts see it.

test_optimizer.check_file and test_executor._mp_execute submit work to spawned
processes; without this, coverage attributes none of that code to the test.
"""
import concurrent.futures as cf
import pickle


def _rt(x):
    # emulate the process boundary: Expr pickling goes through sqlglot.serde
    return pickle.loads(pickle.dumps(x))


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer:
            initializer(*_rt(initargs))

    def submit(self, fn, *a, **k):
        f = cf.Future()
        try:
            a, k = _rt((a, k))
            f.set_result(_rt(fn(*a, **k)))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *its, **k):
        return map(fn, *its)

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()


cf.ProcessPoolExecutor = InlineExecutor


# --- record which non-.py files under tests/ each test opens (fixture map) ---
import os, sys, json  # noqa: E402

_CUR = [""]
_OPENS = {}
_LOG = os.environ.get("FIXLOG")


def _audit(event, args):
    if event == "open" and _CUR[0] and isinstance(args[0], str):
        p = args[0]
        if "/tests/" in p and not p.endswith((".py", ".pyc")):
            _OPENS.setdefault(_CUR[0], set()).add(p.split("/tests/", 1)[1])


if _LOG:
    sys.addaudithook(_audit)


def pytest_runtest_setup(item):
    _CUR[0] = item.nodeid


def pytest_unconfigure(config):
    if _LOG:
        wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
        with open(os.path.join(_LOG, f"opens-{wid}.json"), "w") as fh:
            json.dump({k: sorted(v) for k, v in _OPENS.items()}, fh)
