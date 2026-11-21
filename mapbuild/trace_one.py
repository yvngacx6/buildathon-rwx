"""Trace ONE sqlglot test in a fresh interpreter and write what it touched.

Usage (cwd must be the sqlglot checkout):
    python trace_one.py <pytest node id> <output .json.gz>

Why one process per test: nothing the test needs was done by an earlier test --
caches filled on first use, setUpClass, lazily imported dialects all happen in
this test's own process. There is no "the first test of the file pays for it"
bias and no ordering dependence.

Recorded (all paths repo-relative):
  lines_full    every (file, line) under sqlglot/ or tests/ the process executed (sys.monitoring)
  lines_phase   the subset executed after the test item's setup began (setUpClass, setUp,
                test body, teardown, pool jobs)
  lines         the subset of lines_phase executed outside any module body execution, i.e.
                not merely because the test imported a module for the first time; import-time
                effects are covered separately by import_time.json.gz and classes
  classes       module.Qualname of every class in the MRO of each instantiated
                Expr / Tokenizer / Parser / Generator / Dialect, plus dialect
                classes looked up by name (Dialect["x"], Dialect.get, get_or_raise)
  reads         files under the repo opened for reading (audit hook), except
                sqlglot/ and tests/ Python sources (lines/test_imports cover those)
  test_modules  modules under tests/ present in sys.modules at the end
  outcome       "passed" / "failed" / "skipped" / "missing" (aggregated over reports)

Process-pool work (tests/test_optimizer.py, tests/test_executor.py) runs inline
through InlinePool, which pickles arguments, initargs and results exactly like
the real spawn pool, so sqlglot.serde / __reduce__ code stays on the path.
"""

import sys

# ---- 1. line tracing: start before anything else is imported -------------------------------
import os

ROOT = os.path.realpath(os.getcwd()) + os.sep
_TRACKED = (ROOT + "sqlglot" + os.sep, ROOT + "tests" + os.sep)
_mon = sys.monitoring
_E = _mon.events
_TOOL = _mon.COVERAGE_ID

# Three line sets, file -> {line}:
#   full     everything the process ran (interpreter start, collection, the test)
#   phase    ran after the test item's setup began (setUpClass, setUp, test, teardown)
#   in_test  ran in that phase *outside* any module execution, i.e. not merely because a
#            module was being imported (a lazy `import sqlglot.dialects.x` inside the test)
_full: dict[str, set[int]] = {}
_phase: dict[str, set[int]] = {}
_in_test: dict[str, set[int]] = {}
_state = {"phase": False, "import_depth": 0}


def _add(store, rel, line):
    s = store.get(rel)
    if s is None:
        s = store[rel] = set()
    s.add(line)


def _on_line(code, line):
    fn = code.co_filename
    if line <= 0 or not fn.startswith(_TRACKED):  # line 0: RESUME of an empty module
        return _mon.DISABLE
    rel = fn[len(ROOT):]
    _add(_full, rel, line)
    if not _state["phase"]:
        return _mon.DISABLE  # re-enabled by restart_events() when the phase starts
    _add(_phase, rel, line)
    if _state["import_depth"]:
        # Keep the event live: if the test later runs this line outside an import we must see it.
        return None
    _add(_in_test, rel, line)
    return _mon.DISABLE


# Import depth = number of module bodies currently executing. A module body is a "<module>"
# code object compiled from a .py file; eval/exec of generated code is also named "<module>"
# (the executor evaluates compiled SQL expressions that way) and must NOT count as an import.
def _is_module_body(code):
    return code.co_name == "<module>" and code.co_filename.endswith(".py")


def _on_start(code, offset):
    if not _is_module_body(code):
        return _mon.DISABLE
    _state["import_depth"] += 1
    return None


def _on_return(code, offset, retval):
    if not _is_module_body(code):
        return _mon.DISABLE
    _state["import_depth"] -= 1
    return None


def _on_unwind(code, offset, exc):  # module body raised (failed import); cannot be disabled
    if _is_module_body(code):
        _state["import_depth"] -= 1


def _begin_test_phase():
    _state["phase"] = True
    _mon.restart_events()  # lines run before (e.g. at import) must fire again in the phase


_mon.use_tool_id(_TOOL, "mapbuild")
_mon.register_callback(_TOOL, _E.LINE, _on_line)
_mon.register_callback(_TOOL, _E.PY_START, _on_start)
_mon.register_callback(_TOOL, _E.PY_RETURN, _on_return)
_mon.register_callback(_TOOL, _E.PY_UNWIND, _on_unwind)
_mon.set_events(_TOOL, _E.LINE | _E.PY_START | _E.PY_RETURN | _E.PY_UNWIND)

# ---- 2. file reads (audit hook) -----------------------------------------------------------------
_reads: set[str] = set()
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _audit(event, args):
    if event != "open":
        return
    path, mode, flags = args
    if isinstance(path, int):
        return
    if isinstance(mode, str):
        if any(c in mode for c in "wax+"):
            return
    elif isinstance(flags, int) and flags & _WRITE_FLAGS:
        return
    try:
        p = os.fsdecode(path)
    except TypeError:
        return
    p = os.path.realpath(p)  # relative paths resolve against the *current* cwd (tests may chdir)
    if not p.startswith(ROOT):
        return
    rel = p[len(ROOT):]
    if rel.endswith(".pyc") or "__pycache__" in rel:
        return
    if rel.endswith(".py") and rel.startswith(("sqlglot/", "tests/")):
        return
    if not os.path.isfile(p):
        # directories, and dir_fd-relative opens (e.g. shutil.rmtree of a temp dir: the
        # event carries only "build", which does not exist relative to our cwd)
        return
    _reads.add(rel)


sys.addaudithook(_audit)

# ---- 3. inline process pool with pickle round-trip ----------------------------------------------
import concurrent.futures  # noqa: E402
import concurrent.futures.process  # noqa: E402
import pickle  # noqa: E402


def _roundtrip(x):
    """Emulate the process boundary: args and results cross it pickled."""
    return pickle.loads(pickle.dumps(x))


class InlinePool:
    """Drop-in for ProcessPoolExecutor that runs jobs synchronously in this process."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, /, *args, **kwargs):
        fut = concurrent.futures.Future()
        try:
            a, k = _roundtrip((args, kwargs))
            fut.set_result(_roundtrip(fn(*a, **k)))
        except BaseException as e:  # noqa: BLE001 - mirror the pool: errors surface via the future
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


concurrent.futures.ProcessPoolExecutor = InlinePool
concurrent.futures.process.ProcessPoolExecutor = InlinePool

# ---- 4. class use: wrap base-class __init__ as soon as each base module is loaded ---------------
import importlib.abc  # noqa: E402
import importlib.machinery  # noqa: E402

_used_types: set[type] = set()

# module that defines the base -> base class names to wrap
_BASES = {
    "sqlglot.expressions.core": ("Expr",),
    "sqlglot.tokens": ("Tokenizer",),
    "sqlglot.parser": ("Parser",),
    "sqlglot.generator": ("Generator",),
    "sqlglot.dialects.dialect": ("Dialect",),
}


def _wrap_init(cls):
    """Record type(self) on every instantiation of cls or a subclass.

    If cls owns __init__, call it. If not, defer along the *instance* MRO with
    super(cls, self) -- calling the __init__ found on cls itself would skip
    overrides that sit between cls and its base in a subclass MRO (report 05).
    """
    own = cls.__dict__.get("__init__")

    def __init__(self, *args, **kwargs):
        _used_types.add(type(self))
        if own is not None:
            return own(self, *args, **kwargs)
        return super(cls, self).__init__(*args, **kwargs)

    __init__.__wrapped__ = own
    cls.__init__ = __init__


def _wrap_dialect_lookups(mod):
    """Dialects are often used by name without our seeing an instance; record lookups."""
    meta = type(mod.Dialect)

    def record_result(fn):
        def wrapper(*args, **kwargs):
            res = fn(*args, **kwargs)
            if isinstance(res, type):
                _used_types.add(res)
            elif res is not None:
                _used_types.add(type(res))
            return res

        return wrapper

    for name in ("__getitem__", "get"):
        attr = meta.__dict__.get(name)
        if isinstance(attr, classmethod):
            setattr(meta, name, classmethod(record_result(attr.__func__)))
    attr = mod.Dialect.__dict__.get("get_or_raise")
    if isinstance(attr, classmethod):
        mod.Dialect.get_or_raise = classmethod(record_result(attr.__func__))


class _PostImportLoader(importlib.abc.Loader):
    def __init__(self, inner):
        self.inner = inner

    def create_module(self, spec):
        return self.inner.create_module(spec)

    def exec_module(self, module):
        self.inner.exec_module(module)
        for cls_name in _BASES[module.__name__]:
            _wrap_init(getattr(module, cls_name))
        if module.__name__ == "sqlglot.dialects.dialect":
            _wrap_dialect_lookups(module)


class _BaseFinder(importlib.abc.MetaPathFinder):
    """Wraps the bases right after their modules execute, without importing sqlglot ourselves
    (an eager import would credit sqlglot's import lines to tests that never import it)."""

    def find_spec(self, fullname, path, target=None):
        if fullname not in _BASES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _PostImportLoader(spec.loader)
        return spec


sys.meta_path.insert(0, _BaseFinder())

# ---- 5. run the test -----------------------------------------------------------------------------
import gzip  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402


class _Outcome:
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        _begin_test_phase()  # before any fixture, so setUpClass is in the phase

    def __init__(self):
        self.seen = False
        self.failed = False
        self.skipped = False

    def pytest_runtest_logreport(self, report):
        # Subtest failures arrive as extra reports with failed=True; any failure fails the test.
        self.seen = True
        if report.failed:
            self.failed = True
        elif report.skipped and report.when in ("setup", "call"):
            self.skipped = True

    @property
    def value(self):
        if not self.seen:
            return "missing"
        return "failed" if self.failed else "skipped" if self.skipped else "passed"


def _classes():
    names = set()
    for t in _used_types:
        for c in t.__mro__:
            m = getattr(c, "__module__", "") or ""
            if m == "sqlglot" or m.startswith("sqlglot."):
                names.add(f"{m}.{c.__qualname__}")
    return sorted(names)


def _test_modules():
    out = set()
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f:
            f = os.path.realpath(f)
            if f.startswith(_TRACKED[1]):
                out.add(f[len(ROOT):])
    return sorted(out)


def _dump(store):
    return {f: sorted(v) for f, v in sorted(store.items())}


def main():
    nodeid, out_path = sys.argv[1], sys.argv[2]
    outcome = _Outcome()
    rc = pytest.main(
        [nodeid, "-q", "-p", "no:cacheprovider", "-p", "no:randomly", "-p", "no:xdist"],
        plugins=[outcome],
    )
    _mon.set_events(_TOOL, 0)
    result = {
        "id": nodeid,
        "outcome": outcome.value,
        "exit_code": int(rc),
        "lines_full": _dump(_full),
        "lines_phase": _dump(_phase),
        "lines": _dump(_in_test),
        "classes": _classes(),
        "reads": sorted(_reads),
        "test_modules": _test_modules(),
    }
    with gzip.open(out_path, "wt", compresslevel=1) as fh:
        json.dump(result, fh, separators=(",", ":"))


if __name__ == "__main__":
    main()
