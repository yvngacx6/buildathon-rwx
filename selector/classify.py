"""Turn the edit of one .py file into change regions.

A region says *where* behaviour changed, in terms the maps understand:

  function  the body (or signature) of a def or lambda; tests = the line map over its span
  class     a class body outside any def; runs once, when the class is created
  module    module level outside any def or class; runs once, at import

Rules:
  * A removed line is placed in the OLD file's syntax tree.
  * An added line is placed in the NEW file's tree, then resolved to the deepest scope with
    the same qualified name in the OLD file (a new method is a change to its class body,
    a new nested def is a change to the enclosing function).
  * A region whose scope has the same syntax tree before and after (comments, whitespace,
    docstrings) is dropped.
  * Code that runs at class-creation time even though it sits in a def (metaclass methods,
    `__init_subclass__`) is a class-body change: tests never "execute" it under a test context.
  * A function region also carries the old-file spans of the statements the edit touches,
    when the edit is a clean in-place statement edit (`touched_statements`). The selector
    uses them to keep the few slow tests out when they never ran those statements.
"""

from __future__ import annotations

import ast
import bisect
from dataclasses import dataclass, field, replace

DEFINITION_TIME_METHODS = {"__init_subclass__", "__set_name__", "__class_getitem__"}


@dataclass(frozen=True)
class Region:
    kind: str  # "function" | "class" | "module"
    chain: tuple[str, ...]  # qualified name parts of the scope, e.g. ("TSQL", "Parser", "_parse_x")
    span: tuple[int, int] = (0, 0)  # function: body line range in the OLD file
    signature: bool = False  # function: a decorator / def line changed
    classes: tuple[tuple[str, ...], ...] = ()  # enclosing class chains, innermost last
    via_def: tuple[str, ...] = ()  # class: the definition-time def that put us here
    is_lambda: bool = False  # function: span starts on a `lambda` line (also runs at creation)
    # function: OLD-file spans of the statements the edit touches (see touched_statements);
    # None when the edit is not a clean in-place statement edit.
    statements: tuple[tuple[int, int], ...] | None = None


@dataclass
class _Scope:
    kind: str  # "def" | "class" | "lambda"
    chain: tuple[str, ...]  # named scopes only (lambdas add nothing)
    node: ast.AST
    start: int
    end: int
    body_start: int
    depth: int
    classes: tuple[tuple[str, ...], ...] = field(default=())
    metaclass: bool = False  # a def directly inside a metaclass


def is_docstring(stmt: ast.stmt) -> bool:
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str))


def strip_docstrings(tree: ast.AST) -> ast.AST:
    """Replace the docstring of every module, class and def with `pass` (in place).

    Docstrings are not behaviour (no doctests run under pytest). Keeping a placeholder
    statement keeps every body non-empty and every line number where it was.
    Defs and classes are statements, so only statement lists are walked (expressions
    cannot hold one).
    """
    todo = [tree]
    while todo:
        node = todo.pop()
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and body and is_docstring(body[0])):
            body[0] = ast.copy_location(ast.Pass(), body[0])
        for name in NESTED_STMTS:
            todo.extend(getattr(node, name, None) or ())
    return tree


# Fields that hold statements (or handlers / match cases, which hold statements).
NESTED_STMTS = ("body", "orelse", "finalbody", "handlers", "cases")


def _is_metaclass(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if name == "type" or name.endswith("Meta"):
            return True
    return False


class SameAst:
    """`ast.dump(a) == ast.dump(b)` for a node `a` of the old file and `b` of the new one.

    Compares field by field, as the dump would, but skips a statement when both cover
    identical source lines at the same columns: a statement's syntax tree depends on its
    own text only (docstring stripping also on its place in its body, and callers pair
    statements by place). The text shortcut is off when the texts have a carriage return
    (line numbering) or different first two lines (a coding cookie changes how text decodes).
    """

    _MISSING = object()

    def __init__(self, old_text: str | None = None, new_text: str | None = None) -> None:
        self.old = self.new = None
        if old_text is not None and new_text is not None and "\r" not in old_text + new_text:
            old, new = old_text.split("\n"), new_text.split("\n")
            if old[:2] == new[:2]:
                self.old, self.new = old, new

    def __call__(self, a: ast.AST, b: ast.AST) -> bool:
        if type(a) is not type(b):
            return False
        if self.old is not None and isinstance(a, ast.stmt) and self._same_text(a, b):
            return True
        for name in a._fields:
            x, y = getattr(a, name, self._MISSING), getattr(b, name, self._MISSING)
            if isinstance(x, list) and isinstance(y, list):
                if len(x) != len(y) or not all(map(self._same_value, x, y)):
                    return False
            elif not self._same_value(x, y):
                return False
        return True

    def _same_value(self, x: object, y: object) -> bool:
        if isinstance(x, ast.AST) or isinstance(y, ast.AST):
            return isinstance(x, ast.AST) and isinstance(y, ast.AST) and self(x, y)
        return repr(x) == repr(y)

    def _same_text(self, a: ast.stmt, b: ast.stmt) -> bool:
        return text_key(a, self.old) == text_key(b, self.new)


def text_key(stmt: ast.stmt, lines: list[str]) -> tuple:
    """All a statement's syntax tree depends on: its source lines and its place in them
    (a def or class starts at its first decorator)."""
    first = _first_line(stmt)
    return (stmt.col_offset, stmt.end_col_offset, stmt.lineno - first, stmt.end_lineno - first,
            tuple(lines[first - 1:stmt.end_lineno]))


def _first_line(node: ast.AST) -> int:
    """First line of a node's source (a def or class starts at its first decorator)."""
    return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", ())])


# Leaves for `Scopes`: nodes that cannot contain a def, class or lambda.
_NO_SCOPES = (ast.Name, ast.Constant, ast.expr_context, ast.operator, ast.boolop, ast.cmpop,
              ast.unaryop, ast.alias)


class Scopes:
    """Every def, class and lambda of one parsed file, with line spans."""

    def __init__(self, tree: ast.Module, lines: list[str] | None = None) -> None:
        """`lines`: the source lines (no carriage returns). With them, the walk skips the
        expressions of any node none of whose lines says `lambda`."""
        self.tree = tree
        self.scopes: list[_Scope] = []
        self.by_chain: dict[tuple[str, ...], _Scope] = {}
        self._lambda_lines = None
        if lines is not None:
            self._lambda_lines = [i for i, text in enumerate(lines, 1) if "lambda" in text]
        self._walk(tree)

    def _children(self, node: ast.AST) -> list[ast.AST]:
        """`ast.iter_child_nodes(node)` as a list, minus nodes that cannot hold a scope: the
        leaves in `_NO_SCOPES` and, when no line of `node` has a lambda, all but statements
        (defs and classes are statements)."""
        fields = node._fields
        if self._lambda_lines is not None and hasattr(node, "end_lineno"):
            i = bisect.bisect_left(self._lambda_lines, _first_line(node))
            if i == len(self._lambda_lines) or self._lambda_lines[i] > node.end_lineno:
                fields = [f for f in fields if f in NESTED_STMTS]
        out = []
        for name in fields:
            value = getattr(node, name, None)
            if isinstance(value, list):
                out.extend(v for v in value
                           if isinstance(v, ast.AST) and not isinstance(v, _NO_SCOPES))
            elif isinstance(value, ast.AST) and not isinstance(value, _NO_SCOPES):
                out.append(value)
        return out

    def _walk(self, tree: ast.Module) -> None:
        """Depth-first, children in field order (the order of `ast.iter_child_nodes`): the
        order of `scopes` decides ties in `innermost` and which scope `by_chain` keeps."""
        todo = [(tree, (), (), 0, False)]
        while todo:
            node, chain, classes, depth, in_meta = todo.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                chain = chain + (node.name,)
                is_class = isinstance(node, ast.ClassDef)
                scope = _Scope("class" if is_class else "def", chain, node, first,
                               node.end_lineno, node.body[0].lineno, depth + 1, classes,
                               metaclass=in_meta and not is_class)
                self.scopes.append(scope)
                self.by_chain.setdefault(chain, scope)
                depth += 1
                classes = classes + (chain,) if is_class else classes
                in_meta = is_class and _is_metaclass(node)
            elif isinstance(node, ast.Lambda):
                self.scopes.append(_Scope("lambda", chain, node, node.lineno, node.end_lineno,
                                          node.lineno, depth + 1, classes))
                depth += 1
                in_meta = False
            todo.extend((child, chain, classes, depth, in_meta)
                        for child in reversed(self._children(node)))

    def innermost(self, line: int) -> _Scope | None:
        best = None
        for s in self.scopes:
            if s.start <= line <= s.end and (best is None or s.depth > best.depth):
                best = s
        return best


def lambda_call_lines(lam: ast.Lambda) -> tuple[int, int]:
    """Lines whose coverage means "this lambda was called".

    The `lambda` line also runs when the lambda is *created* (at import, for a lambda in a
    class-level table), so the line map credits it to whichever test imported first. When
    the body is a call whose arguments start on later lines, and everything on the first
    line is a plain name / constant / dotted name (cannot raise or short-circuit), every
    call of the lambda executes those later lines: use only them.
    """
    first, last = lam.lineno, lam.end_lineno or lam.lineno
    body = lam.body
    if last == first or not isinstance(body, ast.Call):
        return first, last
    parts = [body.func] + list(body.args) + [k.value for k in body.keywords]
    later = [p for p in parts if p.lineno > first]
    early = [p for p in parts if p.lineno == first]
    if later and all(_plain(p) and p.end_lineno == first for p in early):
        return min(p.lineno for p in later), last
    return first, last


def _plain(node: ast.AST) -> bool:
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, (ast.Name, ast.Constant))


def _function_region(s: _Scope, line_in_scope: int | None) -> Region:
    if s.kind == "def" and (s.metaclass or s.chain[-1] in DEFINITION_TIME_METHODS):
        enclosing = s.chain[:-1]
        return Region("class", enclosing, classes=s.classes, via_def=s.chain)
    if s.kind == "lambda":
        span = lambda_call_lines(s.node)
        # is_lambda: the span starts on the `lambda` line, which also runs at creation.
        return Region("function", s.chain, span, False, s.classes,
                      is_lambda=span[0] == s.node.lineno)
    first = s.body_start if s.kind == "def" else s.start
    sig = s.kind == "def" and line_in_scope is not None and line_in_scope < s.body_start
    return Region("function", s.chain, (first, s.end), sig, s.classes)


def _region_for_old_line(old: Scopes, line: int) -> Region:
    s = old.innermost(line)
    if s is None:
        return Region("module", ())
    if s.kind == "class":
        return Region("class", s.chain, classes=s.classes + (s.chain,))
    return _function_region(s, line)


def _region_for_new_line(old: Scopes, new: Scopes, line: int) -> Region:
    s = new.innermost(line)
    while s is not None and s.kind == "lambda":  # lambdas have no name to match on
        s = _named_parent(new, s)
    if s is None:
        return Region("module", ())
    chain = s.chain
    for cut in range(len(chain), 0, -1):
        o = old.by_chain.get(chain[:cut])
        if o is None:
            continue
        if o.kind == "class":
            return Region("class", o.chain, classes=o.classes + (o.chain,))
        exact = cut == len(chain)
        sig_line = None
        if exact and line < s.body_start:
            sig_line = o.start  # a signature line of the matching old def
        return _function_region(o, sig_line)
    return Region("module", ())


def _named_parent(scopes: Scopes, s: _Scope) -> _Scope | None:
    return scopes.by_chain.get(s.chain) if s.chain else None


def _same_shell(old: list[ast.stmt], new: list[ast.stmt], same: SameAst) -> bool:
    """Same own statements in a scope's body; nested defs and classes compare by name only."""
    if len(old) != len(new):
        return False
    defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for a, b in zip(old, new):
        if isinstance(a, defs) or isinstance(b, defs):
            if type(a) is not type(b) or a.name != b.name:
                return False
        elif not same(a, b):
            return False
    return True


def _class_head(node: ast.ClassDef) -> list[str]:
    return [ast.dump(x) for x in node.bases + node.keywords + node.decorator_list]


def _unchanged(region: Region, old: Scopes, new: Scopes, same: SameAst) -> bool:
    """True when the region's scope has the same syntax tree before and after."""
    if region.kind == "module":
        return _same_shell(old.tree.body, new.tree.body, same)
    chain = region.via_def or region.chain
    o, n = old.by_chain.get(chain), new.by_chain.get(chain)
    if o is None or n is None or o.kind != n.kind:
        return False
    if region.kind == "class" and not region.via_def:
        return (_class_head(o.node) == _class_head(n.node)
                and _same_shell(o.node.body, n.node.body, same))
    if o.kind == "def" and (region.via_def or region.span[0] == o.body_start):  # the def itself
        return same(o.node, n.node)
    return False  # a lambda inside a named scope: no cheap way to match it


def regions(old_tree: ast.Module, new_tree: ast.Module, removed: list[int], added: list[int],
            same: SameAst | None = None, old_scopes: Scopes | None = None) -> list[Region]:
    """Behaviour-changing regions of one file. Pass trees through `strip_docstrings` first.
    `same`: the trees' SameAst, if the caller has one (else nodes are compared by dump);
    `old_scopes`: `Scopes(old_tree)`, if the caller has it."""
    same = same or SameAst()
    old, new = old_scopes or Scopes(old_tree, same.old), Scopes(new_tree, same.new)
    found: dict[Region, None] = {}
    for line in removed:
        found.setdefault(_region_for_old_line(old, line))
        s = old.innermost(line)
        if s is not None and s.kind == "lambda" and line == s.node.lineno:
            # The `lambda` line also belongs to the enclosing scope (the lambda is created
            # there, next to e.g. the dict key that holds it): deleting a table entry
            # `KEY: lambda self: …` changes the table, not just the lambda.
            p = _named_parent(old, s)
            if p is None:
                found.setdefault(Region("module", ()))
            elif p.kind == "class":
                found.setdefault(Region("class", p.chain, classes=p.classes + (p.chain,)))
            else:
                found.setdefault(_function_region(p, line))
    for line in added:
        found.setdefault(_region_for_new_line(old, new, line))
    return [replace(r, statements=touched_statements(r, old, new, same))
            for r in found if not _unchanged(r, old, new, same)]


# ── statement-level view of a function edit ──────────────────────────────────
#
# A test that never executes any statement the edit touches runs the same bytecode, in the
# same order, as before: the edited function behaves identically for it. That holds only
# when the edit is "in place": the function keeps its statement skeleton, binds the same
# names in the same scopes, and stays (or stays not) a generator. Anything else → None, and
# the caller keeps the whole-function rule.

_STMT_LISTS = ("body", "orelse", "finalbody")
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
           ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def touched_statements(region: Region, old: Scopes, new: Scopes,
                       same: SameAst | None = None) -> tuple[tuple[int, int], ...] | None:
    """OLD-file line spans of the statements an edit touches inside one def, or None.

    Walks the old and new def bodies in parallel. Equal statements are skipped. A touched
    simple statement gives its full span; a touched compound statement (if / for / while /
    with) whose header alone changed gives its header span (the lines up to its first body
    statement), else the walk goes into its bodies. None (no statement view) when:
      * the region is not exactly one def body (lambda, signature, metaclass method,
        a name defined twice);
      * statements were added, removed, split or merged (a body changed length);
      * a nested def / class header changed (it runs whenever the outer body reaches it);
      * a touched statement binds different names, has a global / nonlocal, or adds or
        removes a yield (each of these changes code outside the touched statement);
      * the def writes state that outlives one call (_writes_shared_state): then a
        statement run by an earlier test in the same process (a cache fill) can change
        what a later test sees, and the map credits it only to whichever test ran first.
    """
    if region.kind != "function" or region.signature or region.via_def:
        return None
    olds = [s for s in old.scopes if s.kind == "def" and s.chain == region.chain]
    news = [s for s in new.scopes if s.kind == "def" and s.chain == region.chain]
    if len(olds) != 1 or len(news) != 1 or region.span != (olds[0].body_start, olds[0].end):
        return None
    o, n = olds[0].node, news[0].node
    if _header(o) != _header(n) or _writes_shared_state(o) or _writes_shared_state(n):
        return None
    spans: list[tuple[int, int]] = []
    if not _diff_bodies(o.body, n.body, spans, same or SameAst()) or not spans:
        return None
    return tuple(spans)


def _header(node: ast.stmt) -> list[str]:
    """Every field of a statement except its statement lists, dumped."""
    return [ast.dump(v) if isinstance(v, ast.AST) else repr(v) if not isinstance(v, list)
            else repr([ast.dump(x) if isinstance(x, ast.AST) else x for x in v])
            for k, v in ast.iter_fields(node) if k not in _STMT_LISTS]


def _diff_bodies(old: list[ast.stmt], new: list[ast.stmt], spans: list[tuple[int, int]],
                 same: SameAst) -> bool:
    """Append the spans of touched statements; False when the edit is not in place."""
    if len(old) != len(new):
        return False
    for a, b in zip(old, new):
        if same(a, b):
            continue
        if not _same_scoping(a, b):
            return False
        lists = [k for k in _STMT_LISTS if isinstance(getattr(a, k, None), list)]
        if type(a) is not type(b) or not lists:
            spans.append((a.lineno, a.end_lineno))  # a simple statement, or one replaced
            continue
        if _header(a) != _header(b):
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return False
            if any(isinstance(v, list) and v and not isinstance(v[0], ast.expr)
                   for k, v in ast.iter_fields(a) if k not in _STMT_LISTS):
                spans.append((a.lineno, a.end_lineno))  # except handlers / match cases
                continue
            first_body = min(getattr(a, k)[0].lineno for k in lists if getattr(a, k))
            spans.append((a.lineno, max(a.lineno, first_body - 1)))
        for k in lists:
            if not _diff_bodies(getattr(a, k), getattr(b, k), spans, same):
                return False
    return True


def _same_scoping(a: ast.stmt, b: ast.stmt) -> bool:
    """Same name bindings (per nesting depth), no global/nonlocal, same yield presence."""
    return _bindings(a) == _bindings(b) and _yields(a) == _yields(b) and not any(
        isinstance(x, (ast.Global, ast.Nonlocal)) for s in (a, b) for x in ast.walk(s))


def _bindings(node: ast.AST, depth: int = 0) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.append((depth, node.name))
    elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
        out.append((depth, node.id))
    elif isinstance(node, ast.alias):
        out.append((depth, node.asname or node.name.split(".")[0]))
    elif isinstance(node, ast.arg):
        out.append((depth, node.arg))
    elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
        out.append((depth, node.name))
    elif isinstance(node, ast.MatchMapping) and node.rest:
        out.append((depth, node.rest))
    inner = depth + 1 if isinstance(node, _SCOPES) else depth
    for child in ast.iter_child_nodes(node):
        out += _bindings(child, inner)
    return sorted(out)


def _writes_shared_state(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Does the def store into module- or class-level objects (caches, registries)?

    True for `global` / `nonlocal`, and for an item or attribute store / delete whose root
    object is not a local of the def (a module-level name, a call result) or is the `cls`
    of a classmethod. Stores rooted at other parameters (`self.x = ...`, `node.args[k] = ...`)
    count as per-call state: the object comes from the caller. Mutating method calls count
    like stores when rooted at a non-local name (`_REGISTRY.append(x)`) or at a class-level
    table reached through the instance (`self.NO_PAREN_FUNCTION_PARSERS.pop(k)`).
    """
    params = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
    params |= {a.arg for a in (fn.args.vararg, fn.args.kwarg) if a}
    if any(isinstance(d, ast.Name) and d.id == "classmethod" for d in fn.decorator_list):
        first = (fn.args.posonlyargs + fn.args.args)[:1]
        params -= {a.arg for a in first}
    local = params | {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)
                      and isinstance(n.ctx, ast.Store)}

    def shared_ref(value: ast.AST) -> bool:
        """A reference (not a copy) to a module-level object or a class-level table."""
        if isinstance(value, ast.Name):
            return value.id not in local
        if isinstance(value, ast.Attribute):
            root = value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in ("self", "cls"):
                return value.attr.lstrip("_").isupper()
            return isinstance(root, ast.Name) and root.id not in local
        return False

    # locals bound to shared objects (`table = _TABLES`, `kw = self.KEYWORDS`): mutating them
    # mutates the shared object
    aliases = {st.targets[0].id for st in ast.walk(fn)
               if isinstance(st, ast.Assign) and len(st.targets) == 1
               and isinstance(st.targets[0], ast.Name) and shared_ref(st.value)}
    for node in ast.walk(fn):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return True
        target = None
        if isinstance(node, (ast.Attribute, ast.Subscript)) and not isinstance(node.ctx, ast.Load):
            target = node.value
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in _MUTATORS):
            # `_REGISTRY.append(x)`, `self.TABLE.pop(k)`: an in-place change, like a store
            target = node.func.value
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id in ("self", "cls")
                    and target.attr.lstrip("_").isupper()):
                return True  # a class-level table reached through the instance
            while isinstance(target, (ast.Attribute, ast.Subscript)):
                target = target.value
            if not isinstance(target, ast.Name):
                continue  # rooted at a call result: per call, as far as we can tell
        if target is not None:
            root = target
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if not (isinstance(root, ast.Name) and root.id in local) or (
                    isinstance(root, ast.Name) and root.id in aliases):
                return True
    return False


_MUTATORS = {"pop", "popitem", "update", "append", "add", "extend", "setdefault", "clear",
             "remove", "discard", "insert"}


def _yields(node: ast.AST) -> bool:
    return any(isinstance(x, (ast.Yield, ast.YieldFrom)) for x in ast.walk(node))


def top_level_classes(tree: ast.Module) -> list[str]:
    """Classes outside any def or class body (module level, also inside module-level
    if / try / with / match), in source order."""
    out = []
    todo = list(reversed(tree.body))
    while todo:
        node = todo.pop()
        if isinstance(node, ast.ClassDef):
            out.append(node.name)
        elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for name in reversed(node._fields):
                if name in NESTED_STMTS:
                    todo.extend(reversed(getattr(node, name, None) or ()))
    return out
