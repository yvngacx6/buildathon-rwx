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


def _expr_base():
    mod = sys.modules.get("sqlglot.expressions.core")
    return getattr(mod, "Expr", None) if mod else None


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
    def reducer_override(self, obj):
        base = _expr_base()
        if base is not None and isinstance(obj, base):
            return (new_object, (type(obj),), _slot_state(obj))
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


class CmpPickler(pickle._Pickler):
    """Pure-Python pickler for digests. Immutable values are never memoized, so the bytes do
    not depend on whether two equal strings / tuples happen to be one object (interning);
    mutable objects and Expr nodes are memoized, so their sharing is part of the bytes."""

    def memoize(self, obj):
        if type(obj) in _IMMUTABLE:
            return
        super().memoize(obj)

    def reducer_override(self, obj):
        if type(obj) in _IMMUTABLE:
            return NotImplemented
        if type(obj) is types.FunctionType and obj.__module__ != __name__:
            return (_cmp_function, (obj.__module__, obj.__qualname__, code_digest(obj.__code__),
                                    _cells(obj), obj.__defaults__))
        if type(obj) is types.CodeType:
            return (_cmp_function, ("<code>", obj.co_qualname, code_digest(obj), (), None))
        return InPickler.reducer_override(self, obj)


def dumps_in(value) -> bytes:
    buf = io.BytesIO()
    InPickler(buf, protocol=5).dump(value)
    return buf.getvalue()


def digest_cmp(value) -> bytes:
    buf = io.BytesIO()
    CmpPickler(buf, protocol=5).dump(value)
    return hashlib.blake2b(buf.getvalue(), digest_size=16).digest()
