"""Trace ONE sqlglot test in a fresh interpreter and write what it touched.

Usage (cwd must be the sqlglot checkout):
    python trace_one.py <pytest node id> <output .json.gz>

Why one process per test: everything the process executes is credited to the
test -- interpreter-wide imports, collection of the test file, module and class
bodies, lazily imported dialects, caches filled on first use, setUpClass, and
the test itself. There is no "the first test of the file pays for the import"
bias and no ordering dependence.

Recorded (all paths repo-relative):
  lines         every (file, line) executed under sqlglot/ or tests/ (sys.monitoring LINE)
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
_TOOL = _mon.COVERAGE_ID
_lines: dict[str, set[int]] = {}


def _on_line(code, line):
    # Each location fires once: DISABLE turns the event off for that (code, line).
    fn = code.co_filename
    if fn.startswith(_TRACKED):
        rel = fn[len(ROOT):]
        s = _lines.get(rel)
        if s is None:
            s = _lines[rel] = set()
        s.add(line)
    return _mon.DISABLE


_mon.use_tool_id(_TOOL, "mapbuild")
_mon.register_callback(_TOOL, _mon.events.LINE, _on_line)
_mon.set_events(_TOOL, _mon.events.LINE)

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
        "lines": {f: sorted(v) for f, v in sorted(_lines.items())},
        "classes": _classes(),
        "reads": sorted(_reads),
        "test_modules": _test_modules(),
    }
    with gzip.open(out_path, "wt", compresslevel=1) as fh:
        json.dump(result, fh, separators=(",", ":"))


if __name__ == "__main__":
    main()
