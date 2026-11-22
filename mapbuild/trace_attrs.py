"""moon-19: record which sqlglot class attributes ONE test looks up (fresh interpreter per test).

Usage (cwd = the sqlglot checkout):  python trace_attrs.py <pytest node id> <out .json.gz>

Every sqlglot class gets its plain state entries (`__dict__` values that are not dunders,
functions, descriptors, classes or enum members: flags, tables, strings, numbers, tuples …)
replaced, right after its module executes, by a non-data descriptor that returns the value
and, while the test runs (from its setup on, setUpClass included), records the lookup:
(class the lookup started from, name, class that defines the entry). Every `x.NAME`,
`cls.NAME`, getattr(x, "NAME"), hasattr, super().NAME read of such an entry goes through the
descriptor; only namespace reads (`cls.__dict__`, vars(cls), getattr_static) bypass it — the
selector keeps those (dynamic sites) unrestricted.

Recorded per test:
  reads      "module:Qual NAME" for every class K between the lookup class and the
             defining class (inclusive) in the lookup class's MRO: the test looked NAME up
             through K or a subclass of K, and K or an ancestor of it supplied the value.
             Classes without a real `module:Qual` name (made by type() at import) are
             credited to their nearest real ancestor.
  defs       "module:Qual" → [tracked names, other (untracked) non-dunder names] for every
             class the tracer walked (union over tests = what the selector can trust)
  replaced   true if some tracked entry was rebound / deleted while the test ran (its later
             reads would be invisible): the selector then treats the test as reading all
  outcome    passed / failed / skipped / missing (must equal the plain run)
"""

import enum
import gzip
import json
import os
import sys
import types

ROOT = os.path.realpath(os.getcwd()) + os.sep
_PHASE = [False]
_READS: set = set()  # (lookup class, name, defining class)
_TRACKED: dict = {}  # (id(cls), name) -> (cls, Tracked)
_WALKED: dict = {}  # id(cls) -> cls
_SKIP_TYPES = (types.FunctionType, types.BuiltinFunctionType, types.MethodType, staticmethod,
               classmethod, property, type, types.ModuleType)


class Tracked:
    __slots__ = ("owner", "name", "value")

    def __init__(self, owner, name, value):
        self.owner, self.name, self.value = owner, name, value

    def __get__(self, obj, objtype=None):
        if _PHASE[0]:
            _READS.add((objtype if objtype is not None else type(obj), self.name, self.owner))
        return self.value


def _ours(cls) -> bool:
    m = getattr(cls, "__module__", "") or ""
    return m == "sqlglot" or m.startswith("sqlglot.")


def _plain(name, value) -> bool:
    if name.startswith("__") and name.endswith("__"):
        return False
    if isinstance(value, (_SKIP_TYPES, Tracked, enum.Enum)):
        return False
    return not hasattr(type(value), "__get__")  # descriptors (cached_property, slots …)


def _walk(cls):
    if id(cls) in _WALKED or not _ours(cls):
        return
    _WALKED[id(cls)] = cls
    if isinstance(cls, enum.EnumMeta):
        return  # members live in the class dict; leave Enum machinery alone
    for name, value in list(vars(cls).items()):
        if isinstance(value, type):
            _walk(value)
            continue
        if not _plain(name, value):
            continue
        t = Tracked(cls, name, value)
        try:
            type.__setattr__(cls, name, t)
        except (TypeError, AttributeError):
            continue
        _TRACKED[(id(cls), name)] = (cls, t)


def _walk_module(mod):
    for value in list(vars(mod).values()):
        if isinstance(value, type) and _ours(value):
            _walk(value)


class _Loader:
    def __init__(self, inner):
        self.inner = inner

    def create_module(self, spec):
        return self.inner.create_module(spec)

    def exec_module(self, module):
        self.inner.exec_module(module)
        _walk_module(module)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _Finder:
    def find_spec(self, fullname, path, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        import importlib.machinery

        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None and spec.origin and spec.origin.endswith(".py"):
            spec.loader = _Loader(spec.loader)
        return spec


sys.meta_path.insert(0, _Finder())


def _real_key(cls):
    mod = sys.modules.get(getattr(cls, "__module__", None) or "")
    qual = getattr(cls, "__qualname__", "")
    if mod is None or "<" in qual:
        return None
    obj = mod
    for part in qual.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return f"{cls.__module__}:{qual}" if obj is cls else None


def _credit(cls):
    """Real key of cls, or of its nearest ancestor that has one."""
    for k in cls.__mro__:
        if _ours(k):
            key = _real_key(k)
            if key:
                return key
    return None


# Process-pool work (tests/test_optimizer.py, tests/test_executor.py use spawn pools) must run
# in THIS interpreter, or its lookups are invisible (found by the first re-score: 2 misses).
# Same InlinePool as trace_one.py: arguments, initargs and results cross a pickle round trip.
import concurrent.futures  # noqa: E402
import concurrent.futures.process  # noqa: E402
import pickle  # noqa: E402


def _roundtrip(x):
    return pickle.loads(pickle.dumps(x))


class InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, /, *args, **kwargs):
        fut = concurrent.futures.Future()
        try:
            a, k = _roundtrip((args, kwargs))
            fut.set_result(_roundtrip(fn(*a, **k)))
        except BaseException as e:  # noqa: BLE001
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

import pytest  # noqa: E402


class _Plugin:
    def __init__(self):
        self.seen = self.failed = self.skipped = False

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        for mod in list(sys.modules.values()):  # anything imported without our loader
            if getattr(mod, "__name__", "").split(".")[0] == "sqlglot":
                _walk_module(mod)
        _PHASE[0] = True

    def pytest_runtest_logreport(self, report):
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


def main():
    nodeid, out_path = sys.argv[1], sys.argv[2]
    plugin = _Plugin()
    rc = pytest.main([nodeid, "-q", "-p", "no:cacheprovider", "-p", "no:randomly", "-p", "no:xdist"],
                     plugins=[plugin])
    _PHASE[0] = False
    replaced = any(vars(cls).get(name) is not t for (cls, t) in _TRACKED.values()
                   for name in [t.name])
    reads = set()
    for look, name, owner in _READS:
        mro = look.__mro__
        stop = mro.index(owner) if owner in mro else len(mro) - 1
        for k in mro[: stop + 1]:
            if _ours(k):
                key = _credit(k)
                if key:
                    reads.add(f"{key} {name}")
    defs = {}
    for cls in _WALKED.values():
        key = _real_key(cls)
        if not key:
            continue
        tracked = sorted(n for (i, n) in _TRACKED if i == id(cls))
        other = sorted(n for n in vars(cls) if not (n.startswith("__") and n.endswith("__"))
                       and n not in tracked)
        defs[key] = [tracked, other]
    with gzip.open(out_path, "wt", compresslevel=1) as fh:
        json.dump({"id": nodeid, "outcome": plugin.value, "exit_code": int(rc), "replaced": replaced,
                   "reads": sorted(reads), "defs": defs}, fh, separators=(",", ":"))


if __name__ == "__main__":
    main()
