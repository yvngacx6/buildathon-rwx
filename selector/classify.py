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
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

DEFINITION_TIME_METHODS = {"__init_subclass__", "__set_name__", "__class_getitem__"}


@dataclass(frozen=True)
class Region:
    kind: str  # "function" | "class" | "module"
    chain: tuple[str, ...]  # qualified name parts of the scope, e.g. ("TSQL", "Parser", "_parse_x")
    span: tuple[int, int] = (0, 0)  # function: body line range in the OLD file
    signature: bool = False  # function: a decorator / def line changed
    classes: tuple[tuple[str, ...], ...] = ()  # enclosing class chains, innermost last
    via_def: tuple[str, ...] = ()  # class: the definition-time def that put us here


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


def _is_docstring(stmt: ast.stmt) -> bool:
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str))


def strip_docstrings(tree: ast.AST) -> ast.AST:
    """Replace the docstring of every module, class and def with `pass` (in place).

    Docstrings are not behaviour (no doctests run under pytest). Keeping a placeholder
    statement keeps every body non-empty and every line number where it was.
    """
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and body and _is_docstring(body[0])):
            body[0] = ast.copy_location(ast.Pass(), body[0])
    return tree


def _is_metaclass(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if name == "type" or name.endswith("Meta"):
            return True
    return False


class Scopes:
    """Every def, class and lambda of one parsed file, with line spans."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.scopes: list[_Scope] = []
        self.by_chain: dict[tuple[str, ...], _Scope] = {}
        self._walk(tree, (), (), 0, False)

    def _walk(self, node: ast.AST, chain, classes, depth: int, in_meta: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = min([child.lineno] + [d.lineno for d in child.decorator_list])
                c = chain + (child.name,)
                is_class = isinstance(child, ast.ClassDef)
                scope = _Scope("class" if is_class else "def", c, child, first, child.end_lineno,
                               child.body[0].lineno, depth + 1, classes,
                               metaclass=in_meta and not is_class)
                self.scopes.append(scope)
                self.by_chain.setdefault(c, scope)
                self._walk(child, c, classes + (c,) if is_class else classes, depth + 1,
                           is_class and _is_metaclass(child))
            elif isinstance(child, ast.Lambda):
                self.scopes.append(_Scope("lambda", chain, child, child.lineno, child.end_lineno,
                                          child.lineno, depth + 1, classes))
                self._walk(child, chain, classes, depth + 1, False)
            else:
                self._walk(child, chain, classes, depth, in_meta)

    def innermost(self, line: int) -> _Scope | None:
        best = None
        for s in self.scopes:
            if s.start <= line <= s.end and (best is None or s.depth > best.depth):
                best = s
        return best

    def top_level_classes(self) -> list[str]:
        return [s.chain[0] for s in self.scopes if s.kind == "class" and len(s.chain) == 1]


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
        return Region("function", s.chain, lambda_call_lines(s.node), False, s.classes)
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


def _shell(body: list[ast.stmt]) -> list[str]:
    """A scope's own statements; nested defs and classes appear by name only."""
    out = []
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(f"{type(stmt).__name__}:{stmt.name}")
        else:
            out.append(ast.dump(stmt))
    return out


def _class_shell(node: ast.ClassDef) -> list[str]:
    head = [ast.dump(x) for x in node.bases + node.keywords + node.decorator_list]
    return head + _shell(node.body)


def _unchanged(region: Region, old: Scopes, new: Scopes) -> bool:
    """True when the region's scope has the same syntax tree before and after."""
    if region.kind == "module":
        return _shell(old.tree.body) == _shell(new.tree.body)
    chain = region.via_def or region.chain
    o, n = old.by_chain.get(chain), new.by_chain.get(chain)
    if o is None or n is None or o.kind != n.kind:
        return False
    if region.kind == "class" and not region.via_def:
        return _class_shell(o.node) == _class_shell(n.node)
    if o.kind == "def" and (region.via_def or region.span[0] == o.body_start):  # the def itself
        return ast.dump(o.node) == ast.dump(n.node)
    return False  # a lambda inside a named scope: no cheap way to match it


def regions(old_tree: ast.Module, new_tree: ast.Module,
            removed: list[int], added: list[int]) -> list[Region]:
    """Behaviour-changing regions of one file. Pass trees through `strip_docstrings` first."""
    old, new = Scopes(old_tree), Scopes(new_tree)
    found: dict[Region, None] = {}
    for line in removed:
        found.setdefault(_region_for_old_line(old, line))
    for line in added:
        found.setdefault(_region_for_new_line(old, new, line))
    return [r for r in found if not _unchanged(r, old, new)]


def top_level_classes(tree: ast.Module) -> list[str]:
    return Scopes(tree).top_level_classes()
