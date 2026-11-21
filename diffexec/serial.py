"""Serialisers for differential execution (I12).

Two picklers:

* ``InPickler`` writes a call's inputs so that they can be rebuilt in another process.
  sqlglot ``Expr`` nodes are written with their full slot state (``parent``, ``_type``,
  ``_meta``, ``comments``, ``_hash`` ...), not through ``Expr.__reduce__`` (serde), which
  drops the parent link. A local function (closure / lambda) is written as its code key
  plus its cell contents and rebuilt from the module's source.
* ``CmpPickler`` writes a value only to compare it (blake2b digest). Functions and code
  objects are written as a digest of their bytecode (no line numbers), so that a function
  object whose code was patched compares unequal.

Both rely on pickle's memo, so object sharing (``x is y``) inside one dumped tuple is
part of the bytes: a call that returns its argument and a call that returns an equal copy
of it give different bytes.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import pickle
import sys
import types

# (filename, qualname, firstlineno) -> code object to use when rebuilding a local function.
# Replay sets entries to the patched code object.
CODE_OVERRIDE: dict[tuple[str, str, int], types.CodeType] = {}
_SOURCE_CODES: dict[str, dict[tuple[str, int], types.CodeType]] = {}
_EMPTY = ("<empty cell>",)


class Unrecordable(Exception):
    pass


# ── elided containers (inputs too big to snapshot) ─────────────────────────────────────────
# When a call's input is over the size cap, `dumps_in(..., elide=K, elide_tuple=KT)` writes every
# list / dict / set (exact type) with more than K items, and every tuple with more than KT
# items, as an `Elided` stand-in that only knows its length. (A stand-in tuple that was a dict
# key or set member is hashed while loading: that input fails to load and is not ok.)
# A stand-in answers len() and bool() like the real container did at the call. ANY other use
# (item access, iteration, comparison, hashing, attribute lookup incl. __class__, arithmetic,
# builtins called on it such as type()/id()/isinstance() (replay watches CALL events), pickling
# ...) sets TOUCHED and raises ElidedAccess (a BaseException: `except Exception` cannot swallow
# it, and TOUCHED is checked even if something does). The replay rejects a call whose old or
# new run TOUCHED a stand-in. So an accepted call observed the elided containers only through
# len / bool / identity, which the stand-in reproduces, and the recorded run (real containers)
# took the same path. The effect digest writes those containers as (kind, length, ordinal) on
# both sides (record side: by id, see `elided` in dumps_in / digest_cmp).
TOUCHED = [False]


class ElidedAccess(BaseException):
    pass


def _touch(*_a, **_k):
    TOUCHED[0] = True
    raise ElidedAccess()


class Elided:
    __slots__ = ("_kind", "_n", "_k")

    def __len__(self):
        return object.__getattribute__(self, "_n")

    def __bool__(self):
        return object.__getattribute__(self, "_n") > 0


for _name in ("__hash__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__",
              "__dir__", "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
              "__contains__", "__getitem__", "__setitem__", "__delitem__", "__iter__",
              "__reversed__", "__call__", "__index__", "__int__", "__float__", "__complex__",
              "__bytes__", "__str__", "__repr__", "__format__", "__enter__", "__exit__",
              "__await__", "__aiter__", "__anext__", "__next__", "__round__", "__trunc__",
              "__floor__", "__ceil__", "__sizeof__", "__reduce__", "__reduce_ex__",
              "__length_hint__", "__buffer__", "__release_buffer__", "__add__", "__radd__",
              "__iadd__", "__sub__", "__rsub__", "__isub__", "__mul__", "__rmul__", "__imul__",
              "__matmul__", "__rmatmul__", "__imatmul__", "__truediv__", "__rtruediv__",
              "__itruediv__", "__floordiv__", "__rfloordiv__", "__ifloordiv__", "__mod__",
              "__rmod__", "__imod__", "__divmod__", "__rdivmod__", "__pow__", "__rpow__",
              "__ipow__", "__lshift__", "__rlshift__", "__ilshift__", "__rshift__",
              "__rrshift__", "__irshift__", "__and__", "__rand__", "__iand__", "__xor__",
              "__rxor__", "__ixor__", "__or__", "__ror__", "__ior__", "__neg__", "__pos__",
              "__abs__", "__invert__", "__get__", "__set__", "__delete__", "__copy__",
              "__deepcopy__", "__getstate__", "__setstate__", "__getnewargs__",
              "__getnewargs_ex__", "__fspath__", "__missing__", "__class_getitem__",
              "__mro_entries__", "__instancecheck__", "__subclasscheck__"):
    setattr(Elided, _name, _touch)


MADE = [0]  # stand-ins created so far (replay: does this input contain any?)


def make_elided(kind, n, k):
    MADE[0] += 1
    e = object.__new__(Elided)
    object.__setattr__(e, "_kind", kind)
    object.__setattr__(e, "_n", n)
    object.__setattr__(e, "_k", k)
    return e


def elided_key(e) -> tuple:
    g = object.__getattribute__
    return (g(e, "_kind"), g(e, "_n"), g(e, "_k"))


_ELIDE_TYPES = {list: "list", dict: "dict", set: "set"}


def import_module(name):
    return importlib.import_module(name)


def _expr_base():
    mod = sys.modules.get("sqlglot.expressions.core")
    return getattr(mod, "Expr", None) if mod else None


def _generator_state(obj):
    """A sqlglot Generator whose `_dispatch` is the shared per-class table that __init__ put
    there (generator._DISPATCH_CACHE[cls]): written without that table (hundreds of entries)
    and rebuilt with it, the way __init__ does. Anything else: None (pickled as usual)."""
    mod = sys.modules.get("sqlglot.generator")
    gen = getattr(mod, "Generator", None) if mod else None
    if gen is None or not isinstance(obj, gen):
        return None
    cache = getattr(mod, "_DISPATCH_CACHE", None)
    try:
        dispatch = object.__getattribute__(obj, "_dispatch")
    except AttributeError:
        return None
    if cache is None or cache.get(type(obj)) is not dispatch:
        return None
    d, slots = _slot_state(obj)
    slots = {k: v for k, v in (slots or {}).items() if k != "_dispatch"}
    return (new_generator, (type(obj),), (d, slots or None))


def new_generator(cls):
    import sqlglot.generator as g
    obj = cls.__new__(cls)
    dispatch = g._DISPATCH_CACHE.get(cls)
    if dispatch is None:
        dispatch = g._DISPATCH_CACHE[cls] = g._build_dispatch(cls)
    obj._dispatch = dispatch
    return obj


def _slot_state(obj):
    d = getattr(obj, "__dict__", None)
    slots = {}
    for cls in type(obj).__mro__:
        for s in cls.__dict__.get("__slots__", ()):
            if s in ("__dict__", "__weakref__") or s in slots:
                continue
            try:
                slots[s] = getattr(obj, s)
            except AttributeError:
                pass
    return (dict(d) if d else None, slots or None)


def new_object(cls):
    return cls.__new__(cls)


def code_index(filename: str) -> dict[tuple[str, int], types.CodeType]:
    """All code objects compiled from the (pinned) source file, by (qualname, firstlineno)."""
    got = _SOURCE_CODES.get(filename)
    if got is None:
        with open(filename, "rb") as fh:
            top = compile(fh.read(), filename, "exec", dont_inherit=True)
        got = {}
        todo = [top]
        while todo:
            c = todo.pop()
            got[(c.co_qualname, c.co_firstlineno)] = c
            todo.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
        _SOURCE_CODES[filename] = got
    return got


def rebuild_function(module, filename, qualname, firstlineno, name, defaults, kwdefaults, cells):
    code = CODE_OVERRIDE.get((filename, qualname, firstlineno))
    if code is None:
        code = code_index(filename)[(qualname, firstlineno)]
    g = importlib.import_module(module).__dict__
    closure = tuple(types.CellType() if c == _EMPTY else types.CellType(c) for c in cells) or None
    fn = types.FunctionType(code, g, name, defaults, closure)
    fn.__qualname__ = qualname
    if kwdefaults:
        fn.__kwdefaults__ = kwdefaults
    return fn


def _cells(fn):
    out = []
    for c in fn.__closure__ or ():
        try:
            out.append(c.cell_contents)
        except ValueError:
            out.append(_EMPTY)
    return tuple(out)


def _by_reference(fn) -> bool:
    if "<locals>" in fn.__qualname__ or "<lambda>" in fn.__qualname__:
        return False
    try:
        obj = importlib.import_module(fn.__module__)
        for part in fn.__qualname__.split("."):
            obj = getattr(obj, part)
    except Exception:
        return False
    obj = getattr(obj, "__func__", obj)
    return obj is fn


_DIGESTS: dict[types.CodeType, bytes] = {}


def _const_repr(k) -> str:
    """repr of a code constant that does not depend on set iteration order."""
    if isinstance(k, frozenset):
        return "frozenset(" + repr(sorted(_const_repr(x) for x in k)) + ")"
    if isinstance(k, tuple):
        return "(" + ",".join(_const_repr(x) for x in k) + ",)"
    return repr((type(k).__name__, k))


def code_digest(code: types.CodeType, shallow: bool = False) -> bytes:
    """Bytecode digest without line numbers. shallow=True ignores nested code constants."""
    if not shallow and code in _DIGESTS:
        return _DIGESTS[code]
    h = hashlib.blake2b(digest_size=16)
    for part in (code.co_code, code.co_names, code.co_varnames, code.co_freevars,
                 code.co_cellvars, code.co_argcount, code.co_posonlyargcount,
                 code.co_kwonlyargcount, code.co_flags & ~0x0, code.co_exceptiontable):
        h.update(repr(part).encode())
    for k in code.co_consts:
        if isinstance(k, types.CodeType):
            h.update(b"<code>" if shallow else code_digest(k))
        else:
            h.update(_const_repr(k).encode())
    d = h.digest()
    if not shallow:
        _DIGESTS[code] = d
    return d


class InPickler(pickle.Pickler):
    elide = None  # None, or the item count above which list / dict / set are elided
    elided = None  # id -> (obj, (kind, n, ordinal)) of the elided containers

    def reducer_override(self, obj):
        t = type(obj)
        if t is Elided:
            raise Unrecordable("Elided")
        if t is types.ModuleType:
            if sys.modules.get(obj.__name__) is not obj:
                raise Unrecordable("module not in sys.modules")
            return (import_module, (obj.__name__,))
        base = _expr_base()
        if base is not None and isinstance(obj, base):
            return (new_object, (type(obj),), _slot_state(obj))
        gen = _generator_state(obj)
        if gen is not None:
            return gen
        if type(obj) is types.FunctionType and not _by_reference(obj):
            c = obj.__code__
            return (rebuild_function, (obj.__module__, c.co_filename, c.co_qualname, c.co_firstlineno,
                                       obj.__name__, obj.__defaults__, obj.__kwdefaults__, _cells(obj)))
        if isinstance(obj, (types.GeneratorType, types.CoroutineType, types.FrameType,
                            types.TracebackType)):
            raise Unrecordable(type(obj).__name__)
        return NotImplemented


def _cmp_function(module, qualname, digest, cells, defaults):
    raise RuntimeError("comparison-only pickle")


_IMMUTABLE = (str, bytes, int, float, complex, tuple, frozenset, type(None), bool)


class _ElidingInPickler(InPickler):
    """InPickler that writes big list / dict / set as persistent ids (the C pickler does not
    offer exact lists / dicts / sets to reducer_override). The same container always gets the
    same id, and loads_in() returns one stand-in per id, so aliasing survives."""

    elide_tuple = None  # tuples with more items than this are elided too

    def persistent_id(self, obj):
        t = type(obj)
        if (t in _ELIDE_TYPES and len(obj) > self.elide) or (
                t is tuple and self.elide_tuple is not None and len(obj) > self.elide_tuple):
            hit = self.elided.get(id(obj))
            if hit is None:
                hit = self.elided[id(obj)] = (obj, (t.__name__, len(obj), len(self.elided)))
            return ("elided",) + hit[1]
        return None


class _InUnpickler(pickle.Unpickler):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._stubs = {}

    def persistent_load(self, pid):
        if pid[0] != "elided":
            raise pickle.UnpicklingError(f"unknown persistent id {pid!r}")
        got = self._stubs.get(pid)
        if got is None:
            got = self._stubs[pid] = make_elided(*pid[1:])
        return got


def loads_in(data: bytes):
    """Load a call input written by dumps_in (with or without elided containers)."""
    return _InUnpickler(io.BytesIO(data)).load()


class CmpPickler(pickle._Pickler):
    """Pure-Python pickler for digests. Immutable values are never memoized, so the bytes do
    not depend on whether two equal strings / tuples happen to be one object (interning);
    mutable objects and Expr nodes are memoized, so their sharing is part of the bytes.

    `elided` (record side): id -> (obj, key) of the containers the input pickle elided; they
    are written as their stand-in key, exactly like a replayed `Elided` stand-in."""

    elide = None
    elided = None

    def memoize(self, obj):
        if type(obj) in _IMMUTABLE:
            return
        super().memoize(obj)

    def reducer_override(self, obj):
        if type(obj) in _IMMUTABLE:
            return NotImplemented
        if type(obj) is Elided:
            return (make_elided, elided_key(obj))
        if self.elided:
            hit = self.elided.get(id(obj))
            if hit is not None and hit[0] is obj:
                return (make_elided, hit[1])
        if type(obj) is types.FunctionType and obj.__module__ != __name__:
            return (_cmp_function, (obj.__module__, obj.__qualname__, code_digest(obj.__code__),
                                    _cells(obj), obj.__defaults__))
        if type(obj) is types.CodeType:
            return (_cmp_function, ("<code>", obj.co_qualname, code_digest(obj), (), None))
        return InPickler.reducer_override(self, obj)


class FastCmpPickler(pickle.Pickler):
    """C-pickler version of CmpPickler (what digest_cmp uses; ~5x faster). The C pickler
    memoizes str / tuple / bytes / frozenset by identity, so those are routed through
    persistent ids that carry their value (a fresh object per visit): the bytes then do not
    depend on interning, as in CmpPickler. Mutable objects and Expr nodes are memoized, so
    their sharing is part of the bytes. Elided containers (replay: `Elided` stand-ins; record:
    the ids in `elided`) are written as their stand-in key on both sides."""

    elided = None

    def persistent_id(self, obj):
        t = type(obj)
        if t is str:
            return obj.encode("utf-8", "surrogatepass")
        if self.elided and t is not Elided:
            hit = self.elided.get(id(obj))
            if hit is not None and hit[0] is obj:
                return ("e",) + hit[1]
        if t is tuple:
            return ("t",) + obj
        if t is frozenset:
            return ("f",) + tuple(obj)
        if t is bytes:
            return ("b", bytearray(obj))
        if t is Elided:
            return ("e",) + elided_key(obj)
        return None

    def reducer_override(self, obj):
        if type(obj) is types.FunctionType and obj.__module__ != __name__:
            return (_cmp_function, (obj.__module__, obj.__qualname__, code_digest(obj.__code__),
                                    _cells(obj), obj.__defaults__))
        if type(obj) is types.CodeType:
            return (_cmp_function, ("<code>", obj.co_qualname, code_digest(obj), (), None))
        return InPickler.reducer_override(self, obj)


class TooBig(Exception):
    pass


class _CappedBuffer(io.BytesIO):
    """Stops a pickle early once it is over `cap` bytes (the pickler writes in ~64 kB frames)."""

    def __init__(self, cap: float):
        super().__init__()
        self.cap = cap

    def write(self, b):
        n = super().write(b)
        if self.tell() > self.cap:
            raise TooBig()
        return n


def dumps_in(value, cap: float = float("inf"), elide: int | None = None,
             elided: dict | None = None, elide_tuple: int | None = None) -> bytes:
    """Pickle a call's input. Raises TooBig as soon as it is known to be over `cap` bytes.
    With `elide`, list / dict / set with more than `elide` items are written as stand-ins and
    listed in `elided` (id -> (obj, key)); the caller keeps that dict for the effect digest."""
    buf = _CappedBuffer(cap)
    if elide is None:
        p = InPickler(buf, protocol=5)
    else:
        p = _ElidingInPickler(buf, protocol=5)
        p.elide, p.elided = elide, (elided if elided is not None else {})
        p.elide_tuple = elide_tuple
    p.dump(value)
    data = buf.getvalue()
    if len(data) > cap:
        raise TooBig()
    return data


def digest_cmp(value, elided: dict | None = None) -> bytes:
    buf = io.BytesIO()
    p = FastCmpPickler(buf, protocol=5)
    p.elided = elided
    p.dump(value)
    return hashlib.blake2b(buf.getvalue(), digest_size=16).digest()
