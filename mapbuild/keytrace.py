"""Key-lookup map (moonshot 21): which tests look up which keys in sqlglot's import-time
dict tables, built once from the pinned commit.

A pytest plugin (`-p mapbuild.keytrace`, with the repo root on PYTHONPATH). At configure time,
before collection, it imports every sqlglot module (as selector/state.py does), enumerates
the state entries exactly as `state.Snapshot` names them, and replaces every exact-`dict`
entry value -- and every exact dict nested in one (tries) -- by a recording copy
(`RecDict`, a dict subclass). Every reference to an original is swapped for its copy
(class attributes, module globals, dict values, list slots, closure cells, `__slots__`); an
original some other object still holds (a tuple, a default, a C cache) makes its entry
"leaky": the selector never narrows a leaky entry.

Recorded per table and per context (test / module / everything):
  * key reads: `d[k]`, `d.get(k)`, `k in d` -- hits AND misses (a newly added key turns a miss
    into a hit, so a recorded miss observes it);
  * whole reads: anything else that can see more than one key (iteration, len / bool, keys /
    values / items, copy, ==, |, repr, pickling, **-unpacking, which goes through keys());
  * writes: any mutation at run time -> the table is "mutated" (never narrowed).

Context: the test's call phase -> that test; its setup / teardown (setUpClass, class / module
fixtures) -> every test of the same file; collection and anything outside a test -> all tests.
A test that starts a subprocess or forks (audit events) reads every table: its child's lookups
are not seen.

Keys are stored hash-free (`kid`): str (any str subclass, so str-valued enums) by value,
int / bool / integral float by int value, Enum members by class + name, classes by qualified
name, tuples / frozensets element-wise. A key outside those -> a whole read.

Output (gzipped JSON, `--keytrace-out`): {"tests": [ids], "leaky": [entry], "mutated": [entry],
"entries": {entry: {path: {"W": [ti], "K": {kid: [ti]}}}}} where path is the "\x1f"-joined kids
of the nested keys ("" = the entry's own dict) and ti -1 means every test.
"""

from __future__ import annotations

import enum
import gc
import gzip
import json
import os
import sys
import types

_CTX = -1  # current context index (-1: everything)
_CONTEXTS: list[tuple[str, str]] = []  # ("test", nodeid) | ("file", path)
_CTX_INDEX: dict[tuple[str, str], int] = {}
LOOK: dict[tuple[int, int], set] = {}  # (table, ctx) -> raw keys looked up
WHOLE: dict[int, set[int]] = {}  # table -> contexts that read it whole
MUTATED: set[int] = set()
WRITERS: set[int] = set()  # contexts that wrote some import-time dict (filled a cache?)
ALLTABLES: set[int] = set()  # contexts that spawned a process: read everything


def _ctx(kind: str, name: str) -> int:
    key = (kind, name)
    i = _CTX_INDEX.get(key)
    if i is None:
        i = _CTX_INDEX[key] = len(_CONTEXTS)
        _CONTEXTS.append(key)
    return i


def _whole(t: int) -> None:
    s = WHOLE.get(t)
    if s is None:
        s = WHOLE[t] = set()
    s.add(_CTX)


def _key(t: int, k: object) -> None:
    try:
        s = LOOK.get((t, _CTX))
        if s is None:
            s = LOOK[(t, _CTX)] = set()
        s.add(k)
    except TypeError:  # unhashable: dict itself raises; count it as whole to be safe
        _whole(t)


class RecDict(dict):
    __slots__ = ("_t",)

    # key reads
    def __getitem__(self, k):
        _key(self._t, k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        _key(self._t, k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        _key(self._t, k)
        return dict.__contains__(self, k)

    # whole reads
    def __iter__(self):
        _whole(self._t)
        return dict.__iter__(self)

    def __len__(self):
        _whole(self._t)
        return dict.__len__(self)

    def __reversed__(self):
        _whole(self._t)
        return dict.__reversed__(self)

    def keys(self):
        _whole(self._t)
        return dict.keys(self)

    def values(self):
        _whole(self._t)
        return dict.values(self)

    def items(self):
        _whole(self._t)
        return dict.items(self)

    def copy(self):
        _whole(self._t)
        return dict.copy(self)

    def __copy__(self):
        _whole(self._t)
        return dict.copy(self)

    def __deepcopy__(self, memo):
        import copy
        _whole(self._t)
        return copy.deepcopy(dict(dict.items(self)), memo)

    def __reduce_ex__(self, proto):
        _whole(self._t)
        return (dict, (dict(dict.items(self)),))

    def __reduce__(self):
        _whole(self._t)
        return (dict, (dict(dict.items(self)),))

    def __eq__(self, other):
        _whole(self._t)
        return dict.__eq__(self, other)

    def __ne__(self, other):
        _whole(self._t)
        return dict.__ne__(self, other)

    __hash__ = None  # type: ignore[assignment]

    def __or__(self, other):
        _whole(self._t)
        return dict.__or__(dict(dict.items(self)), other)

    def __ror__(self, other):
        _whole(self._t)
        if not isinstance(other, dict):
            return NotImplemented
        out = dict(other)
        out.update(dict.items(self))
        return out

    def __repr__(self):
        _whole(self._t)
        return dict.__repr__(self)

    def __sizeof__(self):
        _whole(self._t)
        return dict.__sizeof__(self)

    def __getstate__(self):
        _whole(self._t)
        return None

    # writes
    def _mut(self):
        MUTATED.add(self._t)
        WRITERS.add(_CTX)
        _whole(self._t)

    def __setitem__(self, k, v):
        self._mut()
        dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        self._mut()
        dict.__delitem__(self, k)

    def setdefault(self, k, default=None):
        self._mut()
        return dict.setdefault(self, k, default)

    def pop(self, *a):
        self._mut()
        return dict.pop(self, *a)

    def popitem(self):
        self._mut()
        return dict.popitem(self)

    def update(self, *a, **kw):
        self._mut()
        return dict.update(self, *a, **kw)

    def clear(self):
        self._mut()
        return dict.clear(self)

    def __ior__(self, other):
        self._mut()
        return dict.__ior__(self, other)


def kid(k: object) -> str | None:
    """`selector.state.kid` (bound in `install`): one definition for both sides."""
    return _STATE.kid(k)


# ── installation ─────────────────────────────────────────────────────────────────────────

TABLES: list[RecDict] = []  # table index -> wrapper
PATHS: list[list[tuple[str, str]]] = []  # table index -> [(entry, path)]
ORIGINALS: list[dict] = []
LEAKY: set[int] = set()  # table indices with an unswapped reference
ROOT_OF: list[set[int]] = []  # table index -> root table indices it lies under


def _wrap(d: dict, entry: str, path: str, memo: dict[int, int], root: int | None,
          depth: int) -> int:
    t = memo.get(id(d))
    if t is not None:
        PATHS[t].append((entry, path))
        if root is not None:
            ROOT_OF[t].add(root)
        return t
    t = len(TABLES)
    memo[id(d)] = t
    rd = RecDict()
    rd._t = t
    TABLES.append(rd)
    PATHS.append([(entry, path)])
    ORIGINALS.append(d)
    ROOT_OF.append({t if root is None else root})
    root = t if root is None else root
    for k, v in d.items():
        if type(v) is dict and depth < 24:
            kk = kid(k)
            if kk is None:
                LEAKY.add(t)  # cannot name the nested path
                dict.__setitem__(rd, k, v)
                continue
            sub = _wrap(v, entry, path + "\x1f" + kk if path else kk, memo, root, depth + 1)
            dict.__setitem__(rd, k, TABLES[sub])
        else:
            dict.__setitem__(rd, k, v)
    return t


def install(root: str) -> None:
    import importlib.util
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "_keytrace_state", os.path.join(here, "selector", "state.py"))
    state = importlib.util.module_from_spec(spec)
    sys.modules["_keytrace_state"] = state  # dataclass / pickle lookups need it registered
    spec.loader.exec_module(state)

    global _STATE, _BEFORE
    _STATE = state
    snap = state.take(root, {})
    _STATE, _BEFORE = state, dict(snap.entries)
    entries = sorted(snap.entries)
    memo: dict[int, int] = {}
    for e in entries:
        try:
            v = snap.value(e)
        except (KeyError, ValueError):
            continue
        if type(v) is dict:
            _wrap(v, e, "", memo, None, 0)
    v = None  # noqa: F841
    snap = None  # noqa: F841 - drop its references to the originals
    gc.collect()
    _swap()


def _swap() -> None:
    watched = {id(d): i for i, d in enumerate(ORIGINALS)}
    mine = {id(TABLES), id(ORIGINALS), id(PATHS)} | {id(x) for x in TABLES} | set(watched)
    owner: dict[int, type] = {}
    objs = gc.get_objects()
    for o in objs:
        if isinstance(o, type):
            for r in gc.get_referents(o.__dict__):
                if type(r) is dict:
                    owner[id(r)] = o
    frame = sys._getframe()
    for o in objs:
        if id(o) in mine or o is objs or o is watched or o is owner:
            continue
        hit = [r for r in gc.get_referents(o) if id(r) in watched]
        if not hit:
            continue
        if isinstance(o, types.FrameType):
            continue
        try:
            done = _replace(o, watched, owner)
        except Exception:  # noqa: BLE001
            done = False
        if not done:
            for r in hit:
                LEAKY.add(watched[id(r)])
    objs = None  # noqa: F841
    # verify: nothing but our bookkeeping holds an original
    gc.collect()
    for o in gc.get_objects():
        if id(o) in mine or o is watched:
            continue
        for r in gc.get_referents(o):
            i = watched.get(id(r))
            if i is not None and not isinstance(o, types.FrameType):
                LEAKY.add(i)


def _replace(o: object, watched: dict[int, int], owner: dict[int, type]) -> bool:
    if type(o) is dict:
        cls = owner.get(id(o))
        for k, v in list(o.items()):
            i = watched.get(id(v))
            if i is None:
                continue
            if cls is not None:
                type.__setattr__(cls, k, TABLES[i])
            else:
                o[k] = TABLES[i]
        return all(id(v) not in watched for v in o.values())
    if type(o) is list:
        for j, v in enumerate(o):
            i = watched.get(id(v))
            if i is not None:
                o[j] = TABLES[i]
        return True
    if isinstance(o, types.CellType):
        i = watched.get(id(o.cell_contents))
        o.cell_contents = TABLES[i]
        return True
    slots = [s for k in type(o).__mro__ for s in (k.__dict__.get("__slots__") or ())
             if isinstance(k.__dict__.get("__slots__"), (tuple, list))]
    ok = False
    for s in slots:
        try:
            v = getattr(o, s)
        except AttributeError:
            continue
        i = watched.get(id(v))
        if i is not None:
            setattr(o, s, TABLES[i])
            ok = True
    d = getattr(o, "__dict__", None)
    return ok and not (type(d) is dict and any(id(v) in watched for v in d.values()))


# ── pytest plugin ────────────────────────────────────────────────────────────────────────

_OUT = None


def pytest_addoption(parser):
    parser.addoption("--keytrace-out", default="keytrace.json.gz")


def _audit(event, args):
    if event in ("subprocess.Popen", "os.fork", "os.posix_spawn", "os.exec", "os.system",
                 "os.spawn", "os.forkpty"):
        ALLTABLES.add(_CTX)


def pytest_configure(config):
    global _OUT, _CTX
    _OUT = config.getoption("--keytrace-out")
    _CTX = -1
    install(os.environ.get("BR_SQLGLOT", os.getcwd()))
    _selfcheck()
    _inline_pools()
    sys.addaudithook(_audit)


def _inline_pools() -> None:
    """Run process-pool work inline, in the submitting test's context (pickle round trip
    as a worker would see it). A pool's workers outlive the test that started them
    (test_optimizer's class-wide spawn pool), so marking only the spawning test would miss
    every later test's lookups in the children."""
    import concurrent.futures as cf
    import pickle

    init = cf.ProcessPoolExecutor.__init__

    def __init__(self, *a, **kw):
        init(self, *a, **kw)
        self._kt_init = (kw.get("initializer", a[2] if len(a) > 2 else None),
                         kw.get("initargs", a[3] if len(a) > 3 else ()))

    def submit(self, fn, /, *args, **kwargs):
        fut = cf.Future()
        initializer, initargs = getattr(self, "_kt_init", (None, ()))
        if initializer is not None:  # once per executor, as each worker would
            self._kt_init = (None, ())
            initializer(*pickle.loads(pickle.dumps(initargs)))
        try:
            fn2, args2, kwargs2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            fut.set_result(pickle.loads(pickle.dumps(fn2(*args2, **kwargs2))))
        except BaseException as exc:  # noqa: BLE001 - delivered through the future
            fut.set_exception(exc)
        return fut

    cf.ProcessPoolExecutor.__init__ = __init__
    cf.ProcessPoolExecutor.submit = submit


def _selfcheck() -> None:
    """The ways a dict can be read whole must reach a recorded method."""
    global _CTX
    import copy
    import pickle
    saved = _CTX
    _CTX = -99
    rd = RecDict({"a": 1})
    rd._t = -5
    checks = [lambda: {**rd}, lambda: dict(rd), lambda: {}.update(rd) or 0,
              lambda: (lambda **k: k)(**rd), lambda: set(rd), lambda: list(rd),
              lambda: {} == rd, lambda: {} | rd, lambda: copy.copy(rd), lambda: copy.deepcopy(rd),
              lambda: pickle.dumps(rd), lambda: str(rd), lambda: bool(rd), lambda: sorted(rd),
              lambda: tuple(rd), lambda: frozenset(rd), lambda: ",".join(rd),
              lambda: json.dumps(rd), lambda: rd.items(), lambda: [*rd],
              lambda: dict.fromkeys(rd), lambda: max(rd), lambda: rd == {}]
    for i, c in enumerate(checks):
        WHOLE.pop(-5, None)
        c()
        if _CTX not in WHOLE.get(-5, ()):
            raise SystemExit(f"keytrace selfcheck: whole read #{i} not recorded")
    WHOLE.pop(-5, None)
    rd.get("zz")
    if "zz" not in LOOK.pop((-5, -99), ()):
        raise SystemExit("keytrace selfcheck: missing-key get not recorded")
    _CTX = saved


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    global _CTX
    _CTX = _ctx("file", item.nodeid.split("::")[0])
    yield
    _CTX = -1


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    global _CTX
    _CTX = _ctx("test", item.nodeid)
    yield
    _CTX = -1


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    global _CTX
    _CTX = _ctx("file", item.nodeid.split("::")[0])
    yield
    _CTX = -1


_STATE = None
_BEFORE: dict[str, str] = {}


def pytest_sessionfinish(session):
    global LOOK, WHOLE
    nodeids = [it.nodeid for it in getattr(session, "items", [])]
    _write(_OUT, nodeids)
    # Which import-time state did the run itself change (run-time caches, registries)?
    LOOK, WHOLE = {}, {}
    RecDict.__name__ = "dict"
    snap = _STATE.Snapshot("sqlglot")
    snap.take()
    changed = sorted(k for k in snap.entries.keys() | _BEFORE.keys()
                     if snap.entries.get(k) != _BEFORE.get(k))
    with open(_OUT + ".runstate.txt", "w") as f:
        f.write("\n".join(changed) + "\n")
    # an entry whose value the run itself changed (patched, cached, filled lazily) is never
    # narrowed: add them to the map's "mutated" list
    with gzip.open(_OUT, "rt") as f:
        data = json.load(f)
    data["mutated"] = sorted(set(data["mutated"]) | set(changed))
    with gzip.open(_OUT, "wt") as f:
        json.dump(data, f, separators=(",", ":"))


def _write(out: str, nodeids: list[str]) -> None:
    tests = sorted(set(nodeids))
    index = {t: i for i, t in enumerate(tests)}
    by_file: dict[str, list[int]] = {}
    for t in tests:
        by_file.setdefault(t.split("::")[0], []).append(index[t])

    def expand(ctxs) -> set[int]:
        got: set[int] = set()
        for c in ctxs:
            if c < 0:
                return {-1}
            kind, name = _CONTEXTS[c]
            if kind == "test":
                if name in index:
                    got.add(index[name])
            else:
                got.update(by_file.get(name, ()))
        return got

    spawn = expand(ALLTABLES)
    entries: dict[str, dict] = {}
    keyed: dict[int, dict[str, set[int]]] = {}
    for (t, c), keys in LOOK.items():
        slot = keyed.setdefault(t, {})
        for k in keys:
            kk = kid(k)
            if kk is None:
                WHOLE.setdefault(t, set()).add(c)
                continue
            slot.setdefault(kk, set()).add(c)
    leaky_entries = sorted({e for t in LEAKY for r in ROOT_OF[t] for e, _ in PATHS[r]}
                           | {e for t in LEAKY for e, _ in PATHS[t]})
    # A table read whole in a context that also wrote an import-time dict may have been
    # copied into a run-time cache (generator._DISPATCH_CACHE <- dict(TRANSFORMS)): later
    # tests read the cache, not the table, so the table's recorded readers are incomplete.
    fed = {t for t, ctxs in WHOLE.items() if t >= 0 and ctxs & WRITERS}
    mutated = sorted({e for t in MUTATED | fed for r in ROOT_OF[t] for e, _ in PATHS[r]})
    for t, paths in enumerate(PATHS):
        whole = expand(WHOLE.get(t, ())) | spawn
        ks = {k: sorted(expand(cs)) for k, cs in keyed.get(t, {}).items()}
        rec = {"W": sorted(whole), "K": ks}
        for e, p in paths:
            entries.setdefault(e, {})[p] = rec
    with gzip.open(out, "wt") as f:
        json.dump({"tests": tests, "leaky": leaky_entries, "mutated": mutated,
                   "entries": entries}, f, separators=(",", ":"))
