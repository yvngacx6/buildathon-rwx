"""Narrow definition-time edits by the names they change (idea I08).

A class-attribute or module-constant edit runs once, at import, so the line map cannot say who
depends on it. The conservative rule (users of the class ∪ tests that ran the file) is safe
but wide. This module answers a narrower question, statically, on the pinned source:

    which lines can observe the changed value?

1. `body_change` diffs the old and new statements of one class body (or module body). It only
   accepts edits that are plain assignments to names. For each changed assignment it finds the
   changed sub-expressions:
     * inside a lambda body → that lambda's lines (it only runs when called: line map applies);
     * anywhere else        → the assigned name's value changed. The new sub-expression must be
       *inert* (literals, containers, lambdas, names the old file already used), so evaluating
       it at import cannot raise or call anything.
   Anything else (a call, a new method, a bare expression statement, …) → `Fallback`.

2. `Index` finds every *site* in sqlglot/ and tests/ that can read a name:
     * `x.NAME` (attribute load, or augmented assignment);
     * the string constant "NAME" (getattr / __dict__.get / mock.patch.object …);
     * bare `NAME` where it can bind to the changed name (the same class body; for module
       names: the same module or a file that imports the name);
     * dynamic access (getattr / hasattr with a computed name, vars(), dir(), `__dict__`),
       unless a constant fragment of the computed name rules NAME out.
   Each site is classified as
     * `run`: inside a def or lambda that only runs when called → the line map is reliable;
     * `def`: runs at import / class creation, or its result is memoized across tests
       (module bodies, class bodies, decorators, default arguments, metaclass methods,
       `__init_subclass__`, what those call, functions whose result goes into a module-level
       cache, `@cache`d functions). The line map cannot see who depends on such a read.
       A `def` read that merely copies the value into another class/module attribute
       (`FUNCTIONS = {**Parser.FUNCTIONS, ...}`) is followed to the new name; any other
       `def` read → `Fallback`.

3. For a class attribute, `Index.check_class` also requires that every ancestor is a repo class
   (or object / ABC / Generic) with no decorator other than identity ones (`return cls`; the
   mypy_extensions `trait` / `mypyc_attr` stand-ins), and `Index.handles` that no
   definition-time call is handed the class or its registry name (`"mysql"`), unless the
   callee only captures that argument in a closure.

`Index` does the scanning and `Index.export()` turns the result into plain tables; that runs
at image build (`selector/nameindex.py`), whose `NameIndex` answers the queries at run time.

The caller (select.py) unions the line map over the `run` sites and intersects with the
conservative set. Nothing here depends on labels; everything is computed from the pinned source.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from collections import Counter
from dataclasses import dataclass, field

from .classify import DEFINITION_TIME_METHODS, _is_metaclass, lambda_call_lines as _runtime_lines

# Bases whose metaclass / class machinery is known not to consume ordinary class attributes.
HARMLESS_BASES = {"object", "ABC", "Generic", "Protocol"}
METACLASS_CREATION_METHODS = {"__new__", "__init__", "__prepare__"}  # run when a class is made
CACHE_DECORATORS = {"cache", "lru_cache", "cached_property", "cache_info"}
DYNAMIC_CALLS = {"getattr", "hasattr", "setattr", "delattr", "attrgetter", "getattr_static"}
INTROSPECTION_CALLS = {"vars", "dir", "getmembers", "getmembers_static"}
_DISPLAYS = (ast.Dict, ast.Set, ast.List, ast.Tuple)


class Fallback(Exception):
    """The narrowing cannot be proven safe; the message says why."""


# ── 1. what changed in a body ────────────────────────────────────────────────────────────


@dataclass
class BodyChange:
    names: dict[str, bool] = field(default_factory=dict)  # changed name → copies may be followed
    lambda_spans: list[tuple[int, int]] = field(default_factory=list)  # old-file line spans
    lambda_lines: list[int] = field(default_factory=list)  # each lambda's own (`lambda`) line
    new_names: set[str] = field(default_factory=set)  # names added or removed (dir() sees them)
    # a changed statement builds a set: its iteration order can follow the literal's order,
    # which the state snapshot (sets digested sorted) does not see
    sets: bool = False
    # name → what an in-place edit provably keeps of the value ("truth", "keys"; Site.use)
    same: dict[str, frozenset[str]] = field(default_factory=dict)


def _key(stmt: ast.stmt) -> str:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return f"{type(stmt).__name__}:{stmt.name}"  # nested scopes are separate regions
    return ast.dump(stmt)


def _targets(stmt: ast.stmt) -> tuple[str, ...] | None:
    """Assigned names of `NAME = value` / `NAME: T = value`; None for anything else."""
    if isinstance(stmt, ast.Assign) and all(isinstance(t, ast.Name) for t in stmt.targets):
        return tuple(sorted(t.id for t in stmt.targets))
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value:
        return (stmt.target.id,)
    return None


def body_change(old_body: list[ast.stmt], new_body: list[ast.stmt],
                old_tree: ast.AST) -> BodyChange:
    """Diff two statement lists of one class or module body. Raises Fallback."""
    old_keys, new_keys = list(map(_key, old_body)), list(map(_key, new_body))
    removed = _unmatched(old_body, old_keys, new_keys)
    added = _unmatched(new_body, new_keys, old_keys)
    out = BodyChange()
    out.sets = any(isinstance(n, (ast.Set, ast.SetComp)) or (
        isinstance(n, ast.Call) and _last_name(n.func) in ("set", "frozenset"))
        for stmt in removed + added for n in ast.walk(stmt))
    known = _KnownNames(old_tree)
    old_by_target: dict[tuple[str, ...], ast.stmt] = {}
    for stmt in removed:
        names = _targets(stmt)
        if names is None:
            raise Fallback(f"line {stmt.lineno}: {type(stmt).__name__} changed")
        if names in old_by_target:
            raise Fallback(f"{names[0]} assigned twice")
        old_by_target[names] = stmt
    for stmt in added:
        names = _targets(stmt)
        if names is None:
            raise Fallback(f"{type(stmt).__name__} added")
        old = old_by_target.pop(names, None)
        if old is None:  # a new assignment: the whole value is evaluated at import
            if not _inert(stmt.value, known):
                raise Fallback(f"new {names[0]} = <non-literal>")
            for n in names:
                out.names[n] = False
                out.new_names.add(n)
            continue
        if ast.dump(getattr(old, "annotation", None) or ast.Pass()) != ast.dump(
                getattr(stmt, "annotation", None) or ast.Pass()):
            raise Fallback(f"{names[0]}: annotation changed")
        value_changed = False
        for o, n, ancestors, lam in _diff(old.value, stmt.value, []):
            if lam is not None:
                out.lambda_spans.append(_runtime_lines(lam))
                out.lambda_lines.append(lam.lineno)
                continue
            if not _inert(n, known) or not _safe_path(ancestors, n):
                raise Fallback(f"{names[0]}: change at line {getattr(n, 'lineno', '?')} is "
                               "evaluated at import")
            value_changed = True
        if value_changed:
            same_kind = _kind(old.value) == _kind(stmt.value)
            for name in names:
                out.names[name] = out.names.get(name, True) and same_kind
            if len(names) == 1:
                kept = _kept(old.value, stmt.value)
                if kept:
                    out.same[names[0]] = kept
    for names in old_by_target:  # removed assignments: readers now see the inherited value
        for n in names:
            out.names[n] = False
            out.new_names.add(n)
    return out


def _shape(node: ast.AST) -> tuple[bool | None, frozenset | None]:
    """(truth, set of keys / elements) of a literal display, statically; None: unknown."""
    if isinstance(node, ast.Constant):
        try:
            return bool(node.value), None
        except Exception:  # noqa: BLE001
            return None, None
    if isinstance(node, ast.Dict):
        items = node.keys
    elif isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        items = node.elts
    else:
        return None, None
    if any(k is None or isinstance(k, ast.Starred) for k in items):
        return None, None  # ** / * unpacking: size and members unknown
    truth = bool(items)
    if not all(isinstance(k, ast.Constant) for k in items):
        return truth, None
    try:
        return truth, frozenset((type(k.value), k.value) for k in items)
    except TypeError:
        return truth, None


def _kept(old: ast.AST, new: ast.AST) -> frozenset[str]:
    """What reads of truth / membership see unchanged between two literal values."""
    if type(old) is not type(new):
        return frozenset()
    (t0, k0), (t1, k1) = _shape(old), _shape(new)
    kept = set()
    if t0 is not None and t0 == t1:
        kept.add("truth")
    if k0 is not None and k0 == k1:
        kept.add("keys")
    return frozenset(kept)


def _unmatched(body: list[ast.stmt], keys: list[str], other_keys: list[str]) -> list[ast.stmt]:
    """Statements of `body` (with `keys`) left over once each is paired with an equal one on
    the other side (multiset difference, in order)."""
    theirs = Counter(other_keys)
    out = []
    for stmt, k in zip(body, keys):
        if theirs[k] > 0:
            theirs[k] -= 1
        else:
            out.append(stmt)
    return out


class _KnownNames:
    """Dumps of every name / attribute in the old file (`in` test; walked on first use)."""

    def __init__(self, tree: ast.AST) -> None:
        self._tree = tree
        self._dumps: set[str] | None = None

    def __contains__(self, dump: str) -> bool:
        if self._dumps is None:
            self._dumps = {ast.dump(n) for n in ast.walk(self._tree)
                           if isinstance(n, (ast.Name, ast.Attribute))}
        return dump in self._dumps


def _kind(node: ast.AST) -> str:
    return f"Constant:{type(node.value).__name__}" if isinstance(node, ast.Constant) \
        else type(node).__name__


def _diff(o: ast.AST, n: ast.AST, ancestors: list[ast.AST], lam: ast.Lambda | None = None):
    """Yield (old, new, new-side ancestors, enclosing old lambda if in its body) per change."""
    if type(o) is not type(n):
        yield o, n, ancestors, lam
        return
    if isinstance(o, ast.Dict) and _unique_keys(o) and _unique_keys(n):
        old_items = {ast.dump(k) if k else f"**{i}": (k, v) for i, (k, v) in
                     enumerate(zip(o.keys, o.values))}
        new_order = [ast.dump(k) if k else f"**{i}" for i, k in enumerate(n.keys)]
        if ([k for k in old_items if k in new_order]
                != [k for k in new_order if k in old_items]):
            # entries reordered: dict order is observable (`arg_types` order is the
            # positional order of a function's arguments)
            yield o, n, ancestors, lam
        for i, (k, v) in enumerate(zip(n.keys, n.values)):
            key = ast.dump(k) if k else f"**{i}"
            if key not in old_items:
                yield None, ast.Tuple(elts=[k or ast.Constant(None), v]), ancestors + [n], lam
                continue
            ok, ov = old_items.pop(key)
            yield from _diff(ov, v, ancestors + [n], lam)
        if old_items:  # removed entries: no new code runs
            yield None, ast.Constant(None), ancestors + [n], lam
        return
    for name, ov in ast.iter_fields(o):
        if name in ("ctx", "type_comment"):
            continue
        nv = getattr(n, name, None)
        inner_lam = o if isinstance(o, ast.Lambda) and name == "body" else lam
        if isinstance(ov, ast.AST):
            yield from _diff(ov, nv, ancestors + [n], inner_lam)
        elif isinstance(ov, list):
            if len(ov) != len(nv or []):
                yield o, n, ancestors, lam
                return
            for a, b in zip(ov, nv):
                if isinstance(a, ast.AST):
                    yield from _diff(a, b, ancestors + [n], inner_lam)
                elif a != b:
                    yield o, n, ancestors, lam
                    return
        elif ov != nv or type(ov) is not type(nv):
            yield o, n, ancestors, lam
            return


def _unique_keys(d: ast.Dict) -> bool:
    keys = [ast.dump(k) for k in d.keys if k is not None]
    return len(keys) == len(set(keys))


def _inert(node: ast.AST, known: _KnownNames | set[str]) -> bool:
    """Evaluating `node` cannot raise or run code: literals, containers, lambdas, known names."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.operand, ast.Constant) and (
            isinstance(node.op, ast.Not) or isinstance(node.operand.value, (int, float, complex))
            and not isinstance(node.operand.value, bool))
    if isinstance(node, (ast.Name, ast.Attribute)):
        return ast.dump(node) in known  # already evaluated somewhere in the old file
    if isinstance(node, ast.Lambda):
        return all(_inert(d, known) for d in node.args.defaults + node.args.kw_defaults if d)
    if isinstance(node, ast.Dict):
        return all((k is None or (_hashable(k) and _inert(k, known))) and _inert(v, known)
                   for k, v in zip(node.keys, node.values))
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        if isinstance(node, ast.Set) and not all(_hashable(e) for e in node.elts):
            return False
        return all(_inert(e, known) for e in node.elts)
    return False


def _hashable(node: ast.AST) -> bool:
    if isinstance(node, ast.Tuple):
        return all(_hashable(e) for e in node.elts)
    return isinstance(node, (ast.Constant, ast.Name, ast.Attribute, ast.UnaryOp))


def _safe_path(ancestors: list[ast.AST], node: ast.AST) -> bool:
    """Every enclosing expression only builds containers, so a new value cannot make it raise."""
    for a in ancestors:
        if isinstance(a, (ast.Dict, ast.Set, ast.List, ast.Tuple, ast.Starred, ast.keyword)):
            continue
        if isinstance(a, ast.Lambda):  # a lambda default argument: evaluated, not called
            continue
        if isinstance(a, ast.UnaryOp) and isinstance(node, ast.Constant):
            continue
        return False
    return True


# ── 2. who can read a name ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Site:
    path: str
    lines: tuple[int, int]  # lines whose execution performs the read
    when: str  # "run" or "def"
    func: tuple[int, int] | None = None  # span of the enclosing def / lambda ("run" sites)
    copy: tuple[tuple[str, ...], tuple[str, ...]] | None = None  # (owner chain, targets)
    what: str = ""
    dynamic: bool = False  # getattr(x, v) / vars(x) / …: may read any name, of any object
    # getattr / hasattr / attrgetter (an attribute lookup): on an object it reaches class
    # attributes only through the object's class. vars() / dir() / __dict__ / getmembers list
    # a namespace (an object's own dict never holds class attributes): False.
    lookup: bool = False
    memo: bool = False  # "def" site whose result is cached across tests (not just at import)
    typecheck: bool = False  # the class argument of isinstance / issubclass (not handed on)
    # `self.NAME` / `cls.NAME` in a method defined directly in this class body (same file),
    # whose receiver parameter is never rebound: the object is an instance of the class (or
    # the class) or of a subclass, so the read can only reach NAME as defined on a class
    # related to this one. None: any receiver.
    recv: tuple[str, ...] | None = None
    # Where the value read here goes, when it can only be observed further on (run sites):
    #   ("local", ((first, last), ...))  `v = x.NAME` in a def, v bound once: the lines that
    #                                    load v
    #   ("attr", path, chain, attr)      `Cls(..., kw=x.NAME)`, whose __init__ only does
    #                                    `self.attr = kw`: the readers of `attr`
    flow: tuple | None = None
    # How a run-time `x.NAME` read uses the value, when that is all it can observe:
    #   "truth"  only its truth (`if x.NAME:`, `not`, `bool()`, an and / or operand in such a
    #            test)
    #   "keys"   only membership (`k in x.NAME`)
    #   ""       anything else (the value itself)
    use: str = ""


@dataclass
class _Ctx:
    kind: str  # module | class | def | lambda
    chain: tuple[str, ...]
    deftime: bool
    func: ast.AST | None = None
    memo: bool = False  # inside code whose result is cached across tests
    recv: tuple[str, tuple[str, ...]] | None = None  # (receiver parameter, class chain)


class Index:
    """Static facts about the pinned source, computed lazily on first use."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.sources = sources
        self._trees: dict[str, ast.Module] = {}
        self._deftime: set[int] | None = None  # id() of defs that run at import / are memoized
        self._memo_defs: set[int] = set()  # id() of defs whose result is cached across tests
        self._memo_stmts: set[int] = set()  # id() of statements whose value is cached
        self._classes: dict[str, list[tuple[str, ast.ClassDef, tuple[str, ...]]]] | None = None
        self._dynamic: list[tuple] | None = None
        self._file_facts: dict[str, dict] = {}
        self._parent_maps: dict[str, dict[int, ast.AST]] = {}
        self._class_attr_reads: set[tuple[str, str]] | None = None

    # ── parsing and scopes ───────────────────────────────────────────────

    def tree(self, path: str) -> ast.Module:
        t = self._trees.get(path)
        if t is None:
            t = self._trees[path] = ast.parse(self.sources[path])
        return t

    def _lib_paths(self):
        return [p for p in self.sources if p.startswith("sqlglot/")]

    def walk(self, path: str):
        """Yield (node, ctx, stmt) for every node of a file, with its evaluation context."""
        deftime = self.deftime_defs()
        memo = self._memo_stmts
        top = _Ctx("module", (), True)
        stack = [(child, top, child) for child in reversed(self.tree(path).body)]
        funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
        while stack:  # iterative pre-order walk (a recursive generator is ~3x slower)
            node, ctx, stmt = stack.pop()
            if isinstance(node, ast.stmt):
                stmt = node
                if id(node) in memo:  # its value is cached across tests
                    ctx = _Ctx(ctx.kind, ctx.chain, True, ctx.func, True, ctx.recv)
            yield node, ctx, stmt
            # Scopes change the context of their bodies; decorators, default arguments and
            # base classes run in the enclosing context.
            if isinstance(node, funcs):
                inner = _Ctx("def", ctx.chain + (node.name,), id(node) in deftime, node,
                             ctx.memo or id(node) in self._memo_defs,
                             self._receiver(node, ctx.chain) if ctx.kind == "class" else None)
                stack.extend((b, inner, b) for b in reversed(node.body))
                stack.extend((d, ctx, stmt) for d in node.decorator_list)
                stack.extend((d, ctx, stmt) for d in node.args.defaults + node.args.kw_defaults
                             if d is not None)
            elif isinstance(node, ast.Lambda):
                stack.append((node.body, _Ctx("lambda", ctx.chain, False, node), stmt))
                stack.extend((d, ctx, stmt) for d in node.args.defaults + node.args.kw_defaults
                             if d is not None)
            elif isinstance(node, ast.ClassDef):
                inner = _Ctx("class", ctx.chain + (node.name,), True, None, ctx.memo)
                stack.extend((b, inner, b) for b in reversed(node.body))
                stack.extend((d, ctx, stmt) for d in node.decorator_list + node.bases
                             + [k.value for k in node.keywords])
            else:
                stack.extend((c, ctx, stmt) for c in ast.iter_child_nodes(node))

    def _receiver(self, node: ast.FunctionDef | ast.AsyncFunctionDef,
                  chain: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
        """(first parameter, class chain) for a method defined directly in class `chain`
        that is always called with an object of a class related to it as that parameter:
        a plain, property or class method (no other decorator), whose first parameter is
        never rebound, and that no code reads off the class object (`Cls.method` could be
        stored and called with any object)."""
        params = node.args.posonlyargs + node.args.args
        if not params:
            return None
        for d in node.decorator_list:
            if isinstance(d, ast.Name) and d.id in ("property", "classmethod", "cached_property"):
                continue
            if (isinstance(d, ast.Attribute) and d.attr in ("setter", "getter", "deleter")
                    and isinstance(d.value, ast.Name)):
                continue
            return None
        first = params[0].arg
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id == first and not isinstance(n.ctx, ast.Load):
                return None
            if isinstance(n, ast.arg) and n.arg == first and n is not params[0]:
                return None  # a nested scope's own parameter of that name
        if (chain[-1], node.name) in self._class_reads():
            return None
        return first, chain

    def _class_reads(self) -> set[tuple[str, str]]:
        """(name, attr) of every `Name.attr` and `getattr(Name, "attr")` anywhere."""
        if self._class_attr_reads is None:
            out: set[tuple[str, str]] = set()
            for path in self.sources:
                for n in ast.walk(self.tree(path)):
                    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                        out.add((n.value.id, n.attr))
                    elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                          and n.func.id in DYNAMIC_CALLS and len(n.args) >= 2
                          and isinstance(n.args[0], ast.Name)
                          and isinstance(n.args[1], ast.Constant)
                          and isinstance(n.args[1].value, str)):
                        out.add((n.args[0].id, n.args[1].value))
            self._class_attr_reads = out
        return self._class_attr_reads

    # ── definition-time and memoized code ────────────────────────────────

    def deftime_defs(self) -> set[int]:
        """Defs whose body runs at import / class creation, or whose result is cached."""
        if self._deftime is not None:
            return self._deftime
        functions: dict[str, list[ast.AST]] = {}  # callable by bare name (not methods)
        methods: dict[str, list[ast.AST]] = {}  # callable as receiver.name(...)
        for path in self._lib_paths():
            for node, _, in_class in _defs(self.tree(path)):
                (methods if in_class else functions).setdefault(node.name, []).append(node)
        seeds: list[ast.AST] = []
        memo_seeds: list[ast.AST] = []  # results cached across tests
        for path in self._lib_paths():
            module_names = _module_names(self.tree(path))
            for node, in_meta, _ in _defs(self.tree(path)):
                decos = {_last_name(d) for d in node.decorator_list}
                if decos & CACHE_DECORATORS:
                    memo_seeds.append(node)
                    continue
                if ((in_meta and node.name in METACLASS_CREATION_METHODS)
                        or node.name in DEFINITION_TIME_METHODS):
                    seeds.append(node)
                    continue
                memo = self._memo(node, module_names)
                if memo is True:
                    memo_seeds.append(node)
                else:
                    for kind, name in memo:
                        memo_seeds.extend(
                            (functions if kind == "function" else methods).get(name, ()))
        self._functions = functions

        def closure(todo: list[ast.AST]) -> set[int]:
            done: set[int] = set()
            while todo:  # what definition-time code calls also runs at definition time
                f = todo.pop()
                if id(f) in done:
                    continue
                done.add(id(f))
                for kind, name in _calls(f, _own_nodes(f)):
                    todo.extend((functions if kind == "function" else methods).get(name, ()))
            return done

        self._memo_defs = closure(list(memo_seeds))
        self._deftime = closure(seeds) | self._memo_defs
        return self._deftime

    def _memo(self, f: ast.AST, module_names: set[str]) -> set[tuple[str, str]] | bool:
        """Callees whose result `f` stores in module-level state (so later tests reuse it
        without running them); True: all of `f` must be treated as cached."""
        callees: set[tuple[str, str]] = set()
        local_calls: dict[str, set[tuple[str, str]]] = {}
        stored: list[ast.AST] = []
        for sub in _own_nodes(f):
            if isinstance(sub, ast.Global):
                return True
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if isinstance(t, ast.Name):
                        local_calls.setdefault(t.id, set()).update(_calls(f, ast.walk(sub.value)))
                    if isinstance(t, ast.Subscript) and _root_name(t.value) in module_names:
                        stored.append(sub.value)
                        self._memo_stmts.add(id(sub))
                    if isinstance(t, ast.Attribute) and _writes_class(f, t.value, module_names):
                        return True  # derived state written onto a class at run time
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in ("setdefault", "update", "append", "add", "extend")
                    and _root_name(sub.func.value) in module_names):
                stored.extend(sub.args)
        for value in stored:
            callees |= _calls(f, ast.walk(value))
            for n in ast.walk(value):
                if isinstance(n, ast.Name):
                    callees |= local_calls.get(n.id, set())
        return callees

    # ── classes ──────────────────────────────────────────────────────────

    def classes(self) -> dict[str, list[tuple[str, ast.ClassDef, tuple[str, ...]]]]:
        if self._classes is None:
            self._classes = {}
            for path in self.sources:
                stack = [(self.tree(path), ())]
                while stack:
                    node, chain = stack.pop()
                    for child in ast.iter_child_nodes(node):
                        if isinstance(child, ast.expr):
                            continue  # classes are statements
                        c = chain
                        if isinstance(child, ast.ClassDef):
                            c = chain + (child.name,)
                            self._classes.setdefault(child.name, []).append((path, child, c))
                        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            c = chain + (child.name,)
                        stack.append((child, c))
        return self._classes

    def _facts(self, path: str) -> dict:
        """One walk per file: name occurrences, definition-time calls, class-body references."""
        hit = self._file_facts.get(path)
        if hit is not None:
            return hit
        occ: dict[str, list] = {}
        calls: list[tuple[int, set[str], set[str]]] = []
        class_refs: list[tuple[str, str, ast.stmt]] = []
        for node, ctx, stmt in self.walk(path):
            if isinstance(node, ast.Attribute):
                occ.setdefault(node.attr, []).append((node, ctx, stmt))
                if ctx.kind == "class" and getattr(stmt, "value", None) is node:
                    class_refs.append((node.attr, ctx.chain[-1], stmt))
            elif isinstance(node, ast.Name):
                occ.setdefault(node.id, []).append((node, ctx, stmt))
                if ctx.kind == "class" and getattr(stmt, "value", None) is node:
                    class_refs.append((node.id, ctx.chain[-1], stmt))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and len(node.value) < 80:
                occ.setdefault(node.value, []).append((node, ctx, stmt))
            elif isinstance(node, ast.Call) and ctx.deftime:
                calls.append((node, _handed(node)))
        hit = self._file_facts[path] = {"occ": occ, "deftime_calls": calls,
                                         "class_refs": class_refs}
        return hit

    # ── export: everything the run-time queries need, as plain data ──────

    def export(self) -> dict:
        """Scan the whole source once and return the data `NameIndex` answers from.

        Run at image build time (`python -m selector.names build`): the run then never parses
        the library. See `NameIndex` for the meaning of each table.
        """
        occ: dict[str, list] = {}
        imports: dict[str, list[tuple[str, str]]] = {}
        stars: list[tuple[str, str]] = []
        class_refs: dict[str, set[str]] = {}
        calls: list[tuple] = []
        for path in sorted(self.sources):
            checked: set[int] = set()  # isinstance(x, C) / issubclass(x, (C, D)): C, D
            for node in ast.walk(self.tree(path)):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in ("isinstance", "issubclass") and len(node.args) == 2):
                    arg = node.args[1]
                    checked |= {id(e) for e in (arg.elts if isinstance(arg, ast.Tuple) else [arg])}
                if isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        if a.name == "*":
                            stars.append((path, node.module or ""))
                        else:
                            imports.setdefault(a.name, []).append((path, a.asname or a.name))
            facts = self._facts(path)
            for key, rows in facts["occ"].items():
                if not key.isidentifier():
                    continue
                for node, ctx, stmt in rows:
                    if isinstance(node, ast.Attribute):
                        if isinstance(node.ctx, ast.Store) and not isinstance(stmt, ast.AugAssign):
                            continue
                        kind = "attr"
                    elif isinstance(node, ast.Constant):
                        kind = "str"
                    elif isinstance(node.ctx, ast.Store):
                        continue
                    else:
                        kind = "name"
                    site = self._site(path, node, ctx, stmt)
                    if id(node) in checked:
                        site = dataclasses.replace(site, typecheck=True)
                    if kind == "attr" and site.when == "run" and ctx.func is not None:
                        flow = self._flow(path, node, ctx.func, stmt)
                        if flow is not None:
                            site = dataclasses.replace(site, flow=flow)
                    if kind == "attr" and site.when == "run" and not site.dynamic:
                        use = _use(node, self._parents(path))
                        if use:
                            site = dataclasses.replace(site, use=use)
                    occ.setdefault(key, []).append((path, kind, ctx.kind, ctx.chain, site))
            for ref, holder, _ in facts["class_refs"]:
                class_refs.setdefault(ref, set()).add(holder)
            for call, slots in facts["deftime_calls"]:
                callee = call.func.id if isinstance(call.func, ast.Name) else None
                calls.append((path, call.lineno, callee, slots))
        classes = {
            name: [(p, ch, [_last_name(b.value if isinstance(b, ast.Subscript) else b)
                            for b in c.bases + [k.value for k in c.keywords
                                                if k.arg == "metaclass"]],
                    [_dotted(b) for b in c.bases],
                    [(_last_name(d), isinstance(d, ast.Call)) for d in c.decorator_list])
                   for p, c, ch in entries]
            for name, entries in self.classes().items()}
        self.deftime_defs()
        functions = {}
        for name, defs in self._functions.items():
            functions[name] = [self._function_facts(f) for f in defs]
        dynamic = [(site, allowed.pattern if allowed else None, names_only,
                    on_module.pattern if on_module else None)
                   for _, _, site, allowed, names_only, on_module in self._dynamic_sites()]
        return {"occ": occ, "imports": imports, "stars": stars, "class_refs": class_refs,
                "calls": calls, "classes": classes, "functions": functions, "dynamic": dynamic}

    def _function_facts(self, f: ast.AST) -> tuple:
        """(positional params, keyword-only params, names its own code / defaults load,
        `return <first param>` decorator, `return lambda x: x` decorator factory)."""
        params = [a.arg for a in f.args.posonlyargs + f.args.args]
        kwonly = [a.arg for a in f.args.kwonlyargs]
        used = {n.id for n in _own_nodes(f) if isinstance(n, ast.Name)}
        for d in f.args.defaults + [d for d in f.args.kw_defaults if d is not None]:
            used |= {n.id for n in ast.walk(d) if isinstance(n, ast.Name)}
        body = [s for s in f.body if not (isinstance(s, ast.Expr)
                                          and isinstance(s.value, ast.Constant))]
        value = body[0].value if len(body) == 1 and isinstance(body[0], ast.Return) else None
        plain = isinstance(value, ast.Name) and bool(params) and value.id == params[0]
        factory = (isinstance(value, ast.Lambda) and len(value.args.args) == 1
                   and isinstance(value.body, ast.Name)
                   and value.body.id == value.args.args[0].arg)
        return params, kwonly, frozenset(used), plain, factory

    def _site(self, path: str, node: ast.AST, ctx: _Ctx, stmt: ast.stmt | None) -> Site:
        first = node.lineno
        last = node.end_lineno or first
        what = f"{path}:{first}"
        if ctx.kind == "lambda" and not ctx.deftime:
            f = ctx.func
            return Site(path, (f.lineno, f.end_lineno), "run", (f.lineno, f.end_lineno), what=what)
        if ctx.kind == "def" and not ctx.deftime:
            if stmt is not None:
                body = getattr(stmt, "body", None)
                head_end = body[0].lineno - 1 if isinstance(body, list) and body else stmt.end_lineno
                first, last = min(first, stmt.lineno), max(last, max(stmt.lineno, head_end))
            f = ctx.func
            recv = (ctx.recv[1] if ctx.recv is not None and isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name) and node.value.id == ctx.recv[0]
                    else None)
            return Site(path, (first, last), "run", (f.body[0].lineno, f.end_lineno), what=what,
                        recv=recv)
        copy = None
        if ctx.kind in ("class", "module") and stmt is not None and not ctx.func:
            targets = _targets(stmt)
            if targets and _copied(stmt.value, node):
                copy = (ctx.chain, targets)
        return Site(path, (first, last), "def", copy=copy, what=what, memo=ctx.memo)

    # ── where a read value goes (Site.flow) ──────────────────────────────

    def _parents(self, path: str) -> dict[int, ast.AST]:
        hit = self._parent_maps.get(path)
        if hit is None:
            hit = self._parent_maps[path] = {
                id(c): p for p in ast.walk(self.tree(path)) for c in ast.iter_child_nodes(p)}
        return hit

    def _flow(self, path: str, node: ast.Attribute, func: ast.AST,
              stmt: ast.stmt | None) -> tuple | None:
        parents = self._parents(path)
        parent = parents.get(id(node))
        if (isinstance(stmt, ast.Assign) and stmt.value is node and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))):
            return self._local_flow(path, func, stmt.targets[0].id, parents)
        if (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                and parent.func.id == "bool" and parent.args == [node] and not parent.keywords
                and not self._rebinds(path, "bool")):
            # `Cls(..., kw=bool(x.NAME))`: the stored value is only NAME's truth
            outer = parents.get(id(parent))
            if isinstance(outer, ast.keyword) and outer.arg:
                call = parents.get(id(outer))
                if isinstance(call, ast.Call):
                    got = self._init_flow(call, outer.arg, None)
                    return got + ("truth",) if got is not None else None
            return None
        if isinstance(parent, ast.keyword) and parent.arg:
            call = parents.get(id(parent))
            if isinstance(call, ast.Call):
                return self._init_flow(call, parent.arg, None)
        if isinstance(parent, ast.Call) and node in parent.args:
            return self._init_flow(parent, None, parent.args.index(node))
        return None

    def _rebinds(self, path: str, name: str) -> bool:
        """Does the module bind `name` anywhere (shadowing the builtin)?"""
        return any(isinstance(n, ast.Name) and n.id == name and not isinstance(n.ctx, ast.Load)
                   or isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name
                   or isinstance(n, ast.alias) and (n.asname or n.name) == name
                   or isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
                   for n in ast.walk(self.tree(path)))

    def _local_flow(self, path: str, func: ast.AST, var: str,
                    parents: dict[int, ast.AST]) -> tuple | None:
        """`var = <read>` in `func`: the lines that load `var`, if that is its only binding."""
        args = func.args
        params = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
        params |= {a.arg for a in (args.vararg, args.kwarg) if a}
        if var in params:
            return None
        stores, loads = 0, []
        for n in ast.walk(func):
            if isinstance(n, (ast.Global, ast.Nonlocal)) and var in n.names:
                return None
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and n is not func:
                inner = n.args
                if var in {a.arg for a in inner.posonlyargs + inner.args + inner.kwonlyargs}:
                    return None  # shadowed in a nested scope
            if isinstance(n, ast.Name) and n.id == var:
                if isinstance(n.ctx, ast.Load):
                    loads.append(n)
                else:
                    stores += 1
            elif isinstance(n, ast.ExceptHandler) and n.name == var:
                return None
            elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name == var:
                return None
            elif isinstance(n, ast.alias) and (n.asname or n.name).split(".")[0] == var:
                return None
        if stores != 1 or not loads:
            return None
        spans = []
        uses: dict[tuple[int, int], set[str]] = {}
        for n in loads:
            first, last = n.lineno, n.end_lineno or n.lineno
            up = parents.get(id(n))
            while up is not None and not isinstance(up, ast.stmt):
                up = parents.get(id(up))
            if up is not None:  # the map may credit the statement's first line(s)
                body = getattr(up, "body", None)
                head_end = body[0].lineno - 1 if isinstance(body, list) and body else up.end_lineno
                first, last = min(first, up.lineno), max(last, up.lineno, head_end or up.lineno)
            spans.append((first, last))
            uses.setdefault((first, last), set()).add(_use(n, parents))
        spans = sorted(set(spans))
        # per span, how its loads use the value ("" if any load uses it otherwise)
        return ("local", tuple(spans),
                tuple(next(iter(uses[sp])) if len(uses[sp]) == 1 else "" for sp in spans))

    def _init_flow(self, call: ast.Call, kw: str | None, pos: int | None) -> tuple | None:
        """`Cls(..., kw=<read>)` (or positional) where `Cls` is one repo class whose own
        `__init__` only stores that parameter: `self.attr = param`."""
        name = _last_name(call.func)
        if not name or any(isinstance(a, ast.Starred) for a in call.args) or any(
                k.arg is None for k in call.keywords):
            return None
        found = self.classes().get(name, [])
        if len(found) != 1:
            return None
        cpath, cls, chain = found[0]
        if cls.decorator_list or cls.keywords or any(
                _last_name(b) not in ("object",) for b in cls.bases):
            return None  # bases / metaclass / decorators could intercept construction
        body = {s.name: s for s in cls.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))}
        init = body.get("__init__")
        if init is None or "__new__" in body or init.decorator_list:
            return None
        args = init.args
        positional = [a.arg for a in args.posonlyargs + args.args][1:]
        if args.vararg or args.kwarg or not (args.posonlyargs + args.args):
            return None
        me = (args.posonlyargs + args.args)[0].arg
        if kw is not None:
            param = kw if kw in positional + [a.arg for a in args.kwonlyargs] else None
        else:
            param = positional[pos] if pos is not None and pos < len(positional) else None
        if param is None:
            return None
        uses = [n for n in ast.walk(init) if isinstance(n, ast.Name) and n.id == param]
        target = None
        for st in ast.walk(init):
            if (isinstance(st, ast.Assign) and len(st.targets) == 1
                    and isinstance(st.value, ast.Name) and st.value.id == param
                    and isinstance(st.targets[0], ast.Attribute)
                    and isinstance(st.targets[0].value, ast.Name)
                    and st.targets[0].value.id == me):
                if target is not None:
                    return None
                target = st.targets[0].attr
        if target is None or len(uses) != 1:
            return None  # the parameter is used for something else too
        return ("attr", cpath, chain, target)

    # ── the values a computed attribute name can take (moon-20) ──────────

    def _var_constants(self, var: str, ctx: _Ctx, path: str) -> set[str] | None:
        """Every string `var` can hold at a site in `ctx.func`, when each binding of `var`
        anywhere in that function (nested scopes included: conservative) is one of
          * `var = "const"`;
          * a for-loop / comprehension target (`var`, or `var` at a fixed position of a
            tuple target) over a literal tuple / list / set of string constants (or of
            tuples of them);
          * a for-loop / comprehension target over `self.X` / `cls.X` (the method's
            receiver, `ctx.recv`), where X is a class-level literal tuple (not list) of strings in
            that class and X is bound nowhere else in the source (no subclass or instance
            can rebind it, and no string "X" can reach setattr).
        None: any value (a parameter, any other binding, global / nonlocal)."""
        f = ctx.func
        if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        a = f.args
        if var in {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs} | {
                x.arg for x in (a.vararg, a.kwarg) if x}:
            return None
        out: set[str] = set()
        bound = False
        for n in ast.walk(f):
            if isinstance(n, (ast.Global, ast.Nonlocal)) and var in n.names:
                return None
            if isinstance(n, ast.arg) and n.arg == var:
                return None  # a nested scope's parameter (conservative)
            if isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and n.name == var:
                return None
            if isinstance(n, ast.alias) and (n.asname or n.name).split(".")[0] == var:
                return None
            if isinstance(n, ast.Name) and n.id == var and not isinstance(n.ctx, ast.Load):
                bound = True
                got = self._binding_constants(n, var, ctx, path, f)
                if got is None:
                    return None
                out |= got
        return out if bound and out else None

    def _binding_constants(self, name: ast.Name, var: str, ctx: _Ctx, path: str,
                           func: ast.AST) -> set[str] | None:
        parents = self._parents(path)
        up = parents.get(id(name))
        if (isinstance(up, ast.Assign) and len(up.targets) == 1 and up.targets[0] is name
                and isinstance(up.value, ast.Constant) and isinstance(up.value.value, str)):
            return {up.value.value}
        pos = None
        loop = up
        if isinstance(up, ast.Tuple) and not any(isinstance(e, ast.Starred) for e in up.elts):
            pos = up.elts.index(name)
            loop = parents.get(id(up))
            target = up
        else:
            target = name
        if not isinstance(loop, (ast.For, ast.comprehension)) or loop.target is not target:
            return None
        items = self._literal_items(loop.iter, ctx, path)
        if items is None:
            return None
        out = set()
        for item in items:
            if pos is None:
                if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
                    return None
                out.add(item.value)
            else:
                if not (isinstance(item, ast.Tuple) and len(item.elts) == len(target.elts)):
                    return None
                el = item.elts[pos]
                if not (isinstance(el, ast.Constant) and isinstance(el.value, str)):
                    return None
                out.add(el.value)
        return out

    def _literal_items(self, it: ast.AST, ctx: _Ctx, path: str) -> list | None:
        if isinstance(it, (ast.Tuple, ast.List, ast.Set)):
            return None if any(isinstance(e, ast.Starred) for e in it.elts) else list(it.elts)
        if (isinstance(it, ast.Attribute) and isinstance(it.value, ast.Name)
                and ctx.recv is not None and it.value.id == ctx.recv[0]):
            chain = ctx.recv[1]
            cls = next((c for p, c, ch in self.classes().get(chain[-1], ())
                        if p == path and tuple(ch) == tuple(chain)), None)
            if cls is None or self._stores_of(it.attr) != 1:
                return None
            for st in cls.body:
                tgt = (st.targets[0] if isinstance(st, ast.Assign) and len(st.targets) == 1
                       else st.target if isinstance(st, ast.AnnAssign) else None)
                if isinstance(tgt, ast.Name) and tgt.id == it.attr and st.value is not None:
                    v = st.value
                    if isinstance(v, ast.Tuple) and not any(  # immutable: a list could grow
                            isinstance(e, ast.Starred) for e in v.elts):
                        return list(v.elts)
                    return None
        return None

    def _stores_of(self, attr: str) -> int:
        """How many places can bind `attr` as a name or attribute (a string constant equal to
        it counts as one too: setattr / __dict__ writes)."""
        memo = self.__dict__.setdefault("_stores_memo", {})
        if attr not in memo:
            n = 0
            for path in self.sources:
                if attr not in self.sources[path]:
                    continue
                for node in ast.walk(self.tree(path)):
                    if isinstance(node, ast.Name) and node.id == attr and not isinstance(
                            node.ctx, ast.Load):
                        n += 1
                    elif isinstance(node, ast.Attribute) and node.attr == attr and not isinstance(
                            node.ctx, ast.Load):
                        n += 1
                    elif isinstance(node, ast.Constant) and node.value == attr:
                        n += 1
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                           ast.ClassDef)) and node.name == attr:
                        n += 1
            memo[attr] = n
        return memo[attr]

    def _dynamic_sites(self):
        """Reads whose attribute name is computed: getattr(x, v), vars(x), dir(x), x.__dict__."""
        if self._dynamic is None:
            self._dynamic = []
            modules = {p[:-3].replace("/", ".").removesuffix(".__init__")
                       for p in self.sources if p.endswith(".py")}
            for path, text in self.sources.items():
                if not re.search(r"getattr|hasattr|setattr|delattr|vars\(|dir\(|__dict__|getmembers",
                                 text):
                    continue
                parents: dict[int, ast.AST] = {}
                for p in ast.walk(self.tree(path)):
                    for c in ast.iter_child_nodes(p):
                        parents[id(c)] = p
                for node, ctx, stmt in self.walk(path):
                    found = _dynamic(node, parents)
                    if found is None:
                        continue
                    kind, name_arg = found
                    allowed = None
                    if name_arg is not None:
                        allowed = _name_pattern(name_arg, ctx, node, parents)
                        if allowed is None and isinstance(name_arg, ast.Name):
                            consts = self._var_constants(name_arg.id, ctx, path)
                            if consts:
                                allowed = re.compile("|".join(re.escape(c) for c in sorted(consts)),
                                                     re.S)
                    elif kind == "names":
                        allowed = _loop_filter(node, parents)
                    lookup = (isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
                              and _last_name(node.func) in DYNAMIC_CALLS)
                    site = dataclasses.replace(self._site(path, node, ctx, stmt), dynamic=True,
                                               lookup=lookup)
                    self._dynamic.append((path, node, site, allowed,
                                          kind == "names",
                                          _on_module(node, ctx.func, self.tree(path), modules)))
        return self._dynamic



# ── helpers ──────────────────────────────────────────────────────────────────────────────


def _handed(call: ast.Call) -> list[tuple[int | str, set[str], set[str]]]:
    """Per argument slot (position, keyword or "receiver"): names handed to the call as
    objects (not as `X.attr` reads) and lower-cased string constants."""
    pieces: list[tuple[int | str, ast.AST]] = list(enumerate(call.args))
    pieces += [(k.arg or "**", k.value) for k in call.keywords]
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        pieces.append(("receiver", call.func.value))  # C.method(...) runs with C in hand
    out = []
    for slot, piece in pieces:
        if isinstance(piece, ast.Starred):
            slot = "*"
        handed, strings = set(), set()
        # `C.X` hands over the value of X (a read the name scan sees), not C itself
        read_only = {id(a.value) for a in ast.walk(piece) if isinstance(a, ast.Attribute)}
        for sub in ast.walk(piece):
            if isinstance(sub, ast.Name) and (id(sub) not in read_only or sub is piece):
                handed.add(sub.id)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                strings.add(sub.value.lower())
        if handed or strings:
            out.append((slot, handed, strings))
    return out


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Subscript):
        node = node.value
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _on_module(node: ast.AST, func: ast.AST | None, tree: ast.Module | None = None,
               modules: set[str] | frozenset[str] = frozenset()) -> re.Pattern | None:
    """When the dynamic access is applied to a module object, a pattern for the module names
    it can be; else None. The receiver is a module when it is `import_module(<name>)` /
    `__import__(<name>, ...)`, a module-level import alias of a repo module (`exp` after
    `from sqlglot import expressions as exp`), or a local of the enclosing def whose every
    binding is a plain assignment of one of those (`m = __import__(p) if ... else exp`)."""
    if not (isinstance(node, ast.Call) and node.args):
        return None
    receiver = node.args[0]
    aliases = _module_aliases(tree, modules) if tree is not None else {}
    if (isinstance(receiver, ast.Name) and receiver.id not in aliases
            and isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))):
        sources = _local_values(func, receiver.id)
        if not sources:
            return None
    else:
        sources = [receiver]
    parts = []
    for value in sources:
        part = _module_value(value, aliases)
        if part is None:
            return None
        parts.append(part)
    return re.compile("|".join(f"(?:{p})" for p in sorted(set(parts))), re.S)


def _use(node: ast.AST, parents: dict[int, ast.AST]) -> str:
    """How the expression `node` is used, when only part of its value matters ("truth",
    "keys"; see Site.use), else ""."""
    parent = parents.get(id(node))
    if (isinstance(parent, ast.Compare) and len(parent.ops) == 1
            and isinstance(parent.ops[0], (ast.In, ast.NotIn)) and parent.comparators[0] is node):
        return "keys"
    return "truth" if _truth_only(node, parents) else ""


def _truth_only(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """`node` is evaluated only for its truth: an if / while / ternary / assert test, a
    comprehension filter, the operand of `not` or of `bool(...)`, or an and / or operand of
    an expression that is itself used only for its truth (`x and y` alone returns y)."""
    parent = parents.get(id(node))
    if isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)):
        return parent.test is node
    if isinstance(parent, ast.comprehension):
        return any(c is node for c in parent.ifs)
    if isinstance(parent, ast.UnaryOp):
        return isinstance(parent.op, ast.Not)
    if isinstance(parent, ast.Call):
        return (isinstance(parent.func, ast.Name) and parent.func.id == "bool"
                and len(parent.args) == 1 and parent.args[0] is node and not parent.keywords)
    if isinstance(parent, ast.BoolOp):
        return _truth_only(parent, parents)
    return False


def _module_value(value: ast.AST, aliases: dict[str, str]) -> str | None:
    """A regex for the module names `value` can evaluate to, or None (maybe not a module)."""
    if isinstance(value, ast.Name):
        # a module also holds the names it imports (`getattr(exp, "Concat")` reads a class
        # defined in sqlglot.expressions.string): any module's globals (r9)
        return ".*" if value.id in aliases else None
    if not (isinstance(value, ast.Call) and isinstance(value.func, (ast.Name, ast.Attribute))):
        return None
    callee = _last_name(value.func)
    if callee not in ("import_module", "__import__"):
        return None
    if callee == "__import__":
        # `__import__("a.b")` returns the top package "a" unless fromlist is non-empty;
        # only the fromlist form is the named module itself
        fromlist = next((k.value for k in value.keywords if k.arg == "fromlist"),
                        value.args[3] if len(value.args) > 3 else None)
        if not (isinstance(fromlist, (ast.List, ast.Tuple)) and fromlist.elts):
            return ".*"
    arg = value.args[0] if value.args else None
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return re.escape(arg.value)
    if isinstance(arg, ast.JoinedStr):
        return "".join(re.escape(v.value) if isinstance(v, ast.Constant) else ".*"
                       for v in arg.values)
    return ".*"


def _local_values(func: ast.AST, var: str) -> list[ast.AST] | None:
    """The values a local of `func` is bound to, when every binding is a plain single-target
    assignment in `func`'s own code (not a parameter, global / nonlocal, loop, with, walrus,
    import, del, except or match target, nor rebound by a nested scope)."""
    args = func.args
    if var in {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs} | {
            a.arg for a in (args.vararg, args.kwarg) if a}:
        return None
    values, stores = [], 0
    own = {id(n) for n in _own_nodes(func)}
    # `var: T` alone only annotates: it binds nothing
    declared = {id(n.target) for n in ast.walk(func)
                if isinstance(n, ast.AnnAssign) and n.value is None}
    for n in ast.walk(func):
        if id(n) in declared:
            continue
        if isinstance(n, (ast.Global, ast.Nonlocal)) and var in n.names:
            return None
        if isinstance(n, ast.Name) and n.id == var and not isinstance(n.ctx, ast.Load):
            stores += 1
            if id(n) not in own:
                return None
        elif isinstance(n, ast.ExceptHandler) and n.name == var:
            return None
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name == var:
            return None
        elif isinstance(n, ast.alias) and (n.asname or n.name).split(".")[0] == var:
            return None
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == var:
            return None
        if id(n) in own and isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(
                n.targets[0], ast.Name) and n.targets[0].id == var:
            values.append(n.value)
        elif id(n) in own and isinstance(n, ast.AnnAssign) and isinstance(
                n.target, ast.Name) and n.target.id == var and n.value is not None:
            values.append(n.value)
    if stores != len(values):
        return None  # some binding is not a plain assignment
    return values


def _module_aliases(tree: ast.Module, modules: set[str] | frozenset[str]) -> dict[str, str]:
    """Top-level names bound once, by an import statement, to a module in `modules`."""
    found: dict[str, str] = {}
    for st in tree.body:
        if isinstance(st, ast.Import):
            for a in st.names:
                if a.asname:
                    found[a.asname] = a.name
        elif isinstance(st, ast.ImportFrom) and st.level == 0 and st.module:
            for a in st.names:
                found[a.asname or a.name] = f"{st.module}.{a.name}"
    found = {k: v for k, v in found.items() if v in modules}
    if not found:
        return found
    bound: Counter = Counter()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            bound[n.id] += 1
        elif isinstance(n, ast.alias):
            bound[(n.asname or n.name).split(".")[0]] += 1
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound[n.name] += 1
        elif isinstance(n, ast.arg):  # a parameter of that name shadows it (r9)
            bound[n.arg] += 2
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update({x: 2 for x in n.names})
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound[n.name] += 1
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            bound[n.name] += 1
    return {k: v for k, v in found.items() if bound[k] == 1}


def _defs(tree: ast.Module):
    """(def node, directly in a metaclass, directly in a class) for every def of a module."""
    # defs only occur as statements, so walking statement bodies is enough (and fast)
    stack: list[tuple[ast.AST, bool, bool]] = [(tree, False, False)]
    while stack:
        node, in_meta, in_class = stack.pop()
        for name in ("body", "orelse", "finalbody", "handlers", "cases"):
            for child in getattr(node, name, None) or ():
                if not isinstance(child, ast.AST) or isinstance(child, ast.expr):
                    continue
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield child, in_meta, in_class
                    stack.append((child, False, False))
                elif isinstance(child, ast.ClassDef):
                    stack.append((child, _is_metaclass(child), True))
                else:
                    stack.append((child, in_meta, in_class))


def _calls(f: ast.AST, nodes) -> set[tuple[str, str]]:
    """Calls we can resolve by name: ("function", f) for `f(...)`, ("method", m) for
    `self.m(...)` / `cls.m(...)` / `super().m(...)` (the receiver is `f`'s first argument,
    or the class a metaclass `__new__` just created)."""
    nodes = list(nodes)
    receivers = {a.arg for a in f.args.args[:1]}
    if f.name == "__new__":
        for sub in _own_nodes(f):  # klass = super().__new__(...)
            if (isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call)
                    and _last_name(sub.value.func) == "__new__"):
                receivers |= {t.id for t in sub.targets if isinstance(t, ast.Name)}
    out = set()
    for sub in nodes:
        if not isinstance(sub, ast.Call):
            continue
        if isinstance(sub.func, ast.Name):
            out.add(("function", sub.func.id))
        elif isinstance(sub.func, ast.Attribute):
            base = sub.func.value
            if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and \
                    base.func.id == "super":
                out.add(("method", sub.func.attr))
            elif isinstance(base, ast.Name) and base.id in receivers:
                out.add(("method", sub.func.attr))
    return out


def _own_nodes(f: ast.AST):
    """Nodes of a def's body, not descending into nested defs, lambdas or classes."""
    # A nested class body runs right away; a nested def / lambda body only when called
    # (its decorators and default arguments run now).
    deferred = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    stack = list(f.body)
    while stack:
        node = stack.pop()
        if isinstance(node, deferred):
            stack.extend(d for d in node.args.defaults + node.args.kw_defaults if d is not None)
            stack.extend(getattr(node, "decorator_list", []))
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _module_names(tree: ast.Module) -> set[str]:
    names = set()
    for stmt in tree.body:
        for t in (stmt.targets if isinstance(stmt, ast.Assign) else
                  [stmt.target] if isinstance(stmt, ast.AnnAssign) else []):
            if isinstance(t, ast.Name):
                names.add(t.id)
    return names


def _writes_class(f: ast.AST, target: ast.AST, module_names: set[str]) -> bool:
    """`target.x = ...` stores onto a class object (shared by every later test)."""
    if isinstance(target, ast.Call) and _last_name(target.func) == "type":
        return True  # type(self).x = ...
    if not isinstance(target, ast.Name):
        return False
    first = f.args.args[0].arg if f.args.args else None
    is_classmethod = any(_last_name(d) == "classmethod" for d in f.decorator_list)
    return (target.id == first and is_classmethod) or target.id in module_names


def _last_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _root_name(node: ast.AST) -> str:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _find_class(tree: ast.AST, chain: tuple[str, ...]) -> ast.ClassDef | None:
    node = tree
    for part in chain:
        node = next((c for c in getattr(node, "body", []) if isinstance(c, ast.ClassDef)
                     and c.name == part), None)
        if node is None:
            return None
    return node if isinstance(node, ast.ClassDef) else None


def _copied(value: ast.AST, target: ast.AST) -> bool:
    """`value` copies `target` into a container (or is `target`), with no code in between."""
    if value is target:
        return True
    if isinstance(value, ast.Dict):
        return any(_copied(v, target) for k, v in zip(value.keys, value.values) if k is None)
    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        return any(isinstance(e, ast.Starred) and _copied(e.value, target) for e in value.elts)
    if isinstance(value, ast.BinOp) and isinstance(value.op, (ast.BitOr, ast.Add)):
        return _copied(value.left, target) or _copied(value.right, target)
    return False


def _dynamic(node: ast.AST, parents: dict[int, ast.AST]) -> tuple[str, ast.AST | None] | None:
    """('value' | 'names', computed-name expression or None) for a dynamic attribute access."""
    if isinstance(node, ast.Call):
        fn = _last_name(node.func) if isinstance(node.func, (ast.Name, ast.Attribute)) else ""
        if fn in DYNAMIC_CALLS and fn != "attrgetter":
            if len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant):
                return "value", node.args[1]
            return None
        if fn == "attrgetter" and any(not isinstance(a, ast.Constant) for a in node.args):
            return "value", None
        if fn in INTROSPECTION_CALLS:
            return ("names" if fn == "dir" else "value"), None
        return None
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        parent = parents.get(id(node))
        if isinstance(parent, ast.Attribute) and parent.attr == "get":
            call = parents.get(id(parent))
            if isinstance(call, ast.Call) and call.args and isinstance(call.args[0], ast.Constant):
                return None  # x.__dict__.get("NAME"): the string constant is the read
        if isinstance(parent, ast.Subscript) and isinstance(parent.slice, ast.Constant):
            return None
        if isinstance(parent, ast.Compare) and isinstance(parent.left, ast.Constant):
            return None  # "NAME" in x.__dict__
        return "value", None
    return None


def _name_pattern(arg: ast.AST, ctx: _Ctx, site: ast.AST,
                  parents: dict[int, ast.AST]) -> re.Pattern | None:
    """A regex every name read at this site matches, or None (any name)."""
    if isinstance(arg, ast.JoinedStr):
        parts = [re.escape(v.value) if isinstance(v, ast.Constant) else ".*" for v in arg.values]
        return re.compile("".join(parts), re.S)
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
        parts = []
        for side in (arg.left, arg.right):
            parts.append(re.escape(side.value) if isinstance(side, ast.Constant)
                         and isinstance(side.value, str) else ".*")
        return re.compile("".join(parts), re.S)
    if isinstance(arg, ast.Name):
        return _guard(arg.id, site, parents)
    return None


def _guard(var: str, site: ast.AST, parents: dict[int, ast.AST]) -> re.Pattern | None:
    """Patterns from `if v.endswith(C) ...:` around the site, or `if not v.endswith(C) ...: continue`."""
    node = site
    while id(node) in parents:
        parent = parents[id(node)]
        if isinstance(parent, ast.If) and node in parent.body:
            for test in _conjuncts(parent.test):
                p = _affix(test, var)
                if p:
                    return p
        if isinstance(parent, (ast.For, ast.While, ast.If, ast.With, ast.Try)) and node in getattr(
                parent, "body", []):
            for prev in parent.body[:parent.body.index(node)]:
                if (isinstance(prev, ast.If) and len(prev.body) == 1
                        and isinstance(prev.body[0], ast.Continue)):
                    for test in _disjuncts(prev.test):
                        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                            p = _affix(test.operand, var)
                            if p:
                                return p
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            break
        node = parent
    return None


def _loop_filter(site: ast.AST, parents: dict[int, ast.AST]) -> re.Pattern | None:
    """`for v in dir(x):` whose body first filters v by a suffix / prefix."""
    loop = parents.get(id(site))
    if not (isinstance(loop, ast.For) and loop.iter is site and isinstance(loop.target, ast.Name)
            and loop.body):
        return None
    var, first = loop.target.id, loop.body[0]
    if not isinstance(first, ast.If):
        return None
    if len(first.body) == 1 and isinstance(first.body[0], ast.Continue):
        for test in _disjuncts(first.test):  # if not v.endswith(C) or ...: continue
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                p = _affix(test.operand, var)
                if p:
                    return p
    elif len(loop.body) == 1 and not first.orelse:  # if v.endswith(C) and ...: <only use>
        for test in _conjuncts(first.test):
            p = _affix(test, var)
            if p:
                return p
    return None


def _conjuncts(test: ast.AST):
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return test.values
    return [test]


def _disjuncts(test: ast.AST):
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return test.values
    return [test]


def _affix(test: ast.AST, var: str) -> re.Pattern | None:
    if (isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute)
            and isinstance(test.func.value, ast.Name) and test.func.value.id == var
            and test.func.attr in ("endswith", "startswith") and len(test.args) == 1
            and isinstance(test.args[0], ast.Constant) and isinstance(test.args[0].value, str)):
        c = re.escape(test.args[0].value)
        return re.compile(f".*{c}" if test.func.attr == "endswith" else f"{c}.*", re.S)
    return None
