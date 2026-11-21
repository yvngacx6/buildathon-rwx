"""Does importing a test module run any sqlglot code of its own? (idea I25)

The import-time map lists, per sqlglot line, every module whose import (fresh interpreter)
runs that line; test modules show up there because they import sqlglot. When a test module's
own import-time code only imports modules, defines functions and classes, and evaluates
literals and a few known-harmless library calls, then every sqlglot line its import runs is
run by importing sqlglot modules – whose resulting state the import-state diff (state.py)
compares in full. Such a test module is *explained* by the diff: if the diff is empty, its
import builds what it built before.

`Checker(root).explained(module)` checks the module, its packages' `__init__`, and every test
module it imports, statically:

  statements   import, def (decorators / defaults / annotations checked), class (bases,
               keywords, decorators checked; a base imported from sqlglot runs sqlglot's
               class machinery → not explained), assignment to plain names, if / try
               (`if __name__ == "__main__":` bodies never run on import), docstrings, pass;
               anything else → not explained
  expressions  literals and containers, names, lambdas (not called), operators on values
               that are not sqlglot objects, attribute chains through module objects only
               (a sqlglot class attribute may be a metaclass property), subscripts of
               typing / library objects
  calls        unittest.skip* / expectedFailure, unittest.mock.patch*, os.path.*,
               os.environ.get, os.getenv, re.compile, a few builtins on such values,
               str methods on their results, and test helper functions whose body only
               uses those (`is_compiled(module)`, `string_to_bool(...)`)
"""

from __future__ import annotations

import ast

ALLOWED_CALLS = {
    "unittest.skip", "unittest.skipIf", "unittest.skipUnless", "unittest.expectedFailure",
    "unittest.mock.patch", "unittest.mock.patch.object", "unittest.mock.patch.dict",
    "os.environ.get", "os.getenv", "re.compile",
    "os.path.join", "os.path.dirname", "os.path.abspath", "os.path.normpath",
    "os.path.isdir", "os.path.exists", "os.path.realpath", "os.path.basename",
}
BUILTINS = {"frozenset", "set", "tuple", "list", "dict", "str", "bool", "int", "len",
            "isinstance", "getattr", "hasattr", "sorted"}
STR_METHODS = {"lower", "upper", "strip", "endswith", "startswith", "split", "splitlines",
               "join", "replace", "format", "get"}


class _Module:
    def __init__(self, checker: "Checker", name: str, tree: ast.Module) -> None:
        self.checker = checker
        self.name = name
        self.package = name if checker.is_package(name) else name.rpartition(".")[0]
        self.kinds: dict[str, str] = {}  # bound name → module | sqlglot | ext | local | def
        self.targets: dict[str, str] = {}  # bound name → dotted origin (imports)
        self.defs: dict[str, ast.FunctionDef] = {}
        self.classes: set[str] = set()  # top-level classes (checked: no sqlglot base)
        self.future_annotations = any(
            isinstance(s, ast.ImportFrom) and s.module == "__future__"
            and any(a.name == "annotations" for a in s.names) for s in tree.body)
        self.tree = tree

    # ── statements ──────────────────────────────────────────────────────

    def ok(self) -> bool:
        return self.block(self.tree.body, top=True)

    def block(self, body: list[ast.stmt], top: bool) -> bool:
        return all(self.stmt(s, top) for s in body)

    def stmt(self, s: ast.stmt, top: bool) -> bool:
        if isinstance(s, ast.Import):
            for a in s.names:
                if not self.checker.imported(a.name):
                    return False
                if a.asname:
                    self.bind(a.asname, "module" if self.checker.is_module(a.name) else "ext",
                              a.name)
                else:
                    root = a.name.split(".")[0]
                    self.bind(root, "module" if self.checker.is_module(root) else "ext", root)
            return True
        if isinstance(s, ast.ImportFrom):
            base = self.resolve(s)
            if base is None or not self.checker.imported(base):
                return False
            for a in s.names:
                if a.name == "*":
                    return False
                full = f"{base}.{a.name}"
                if self.checker.is_module(full):
                    if not self.checker.imported(full):
                        return False
                    kind = "module"
                elif base.split(".")[0] == "sqlglot":
                    kind = "sqlglot"
                elif self.checker.is_module(base) and base.split(".")[0] == "tests":
                    kind = "testobj"
                else:
                    kind = "ext"
                self.bind(a.asname or a.name, kind, full)
            return True
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exprs = s.decorator_list + [d for d in s.args.defaults + s.args.kw_defaults if d]
            if not self.future_annotations:
                exprs += [a.annotation for a in s.args.posonlyargs + s.args.args
                          + s.args.kwonlyargs if a.annotation is not None]
                exprs += [s.returns] if s.returns is not None else []
            if not all(self.expr(e) for e in exprs):
                return False
            if top:
                self.defs[s.name] = s
            self.bind(s.name, "def" if top else "local", s.name)
            return True
        if isinstance(s, ast.ClassDef):
            if not all(self.expr(e) for e in s.bases + [k.value for k in s.keywords]
                       + s.decorator_list):
                return False
            if any(self.sqlglot_rooted(b) for b in s.bases + [k.value for k in s.keywords]):
                return False  # sqlglot's metaclass / __init_subclass__ would run
            self.bind(s.name, "local", s.name)
            if top:
                self.classes.add(s.name)
            return self.block(s.body, top=False)
        if isinstance(s, ast.Assign):
            if not all(self.plain_target(t) for t in s.targets):
                return False
            ok = self.expr(s.value)
            for t in s.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        self.bind(n.id, "local", n.id)
            return ok
        if isinstance(s, ast.AnnAssign):
            if not isinstance(s.target, ast.Name):
                return False
            if not self.future_annotations and not self.expr(s.annotation):
                return False
            self.bind(s.target.id, "local", s.target.id)
            return s.value is None or self.expr(s.value)
        if isinstance(s, ast.Expr):
            return isinstance(s.value, ast.Constant) or self.expr(s.value)
        if isinstance(s, ast.If):
            if _is_main_guard(s.test):
                return self.block(s.orelse, top)
            return self.expr(s.test) and self.block(s.body, top) and self.block(s.orelse, top)
        if isinstance(s, ast.Try):
            return (self.block(s.body, top) and self.block(s.orelse, top)
                    and self.block(s.finalbody, top)
                    and all((h.type is None or self.expr(h.type)) and self.block(h.body, top)
                            for h in s.handlers))
        return isinstance(s, ast.Pass)

    def bind(self, name: str, kind: str, target: str) -> None:
        self.kinds[name] = kind
        self.targets[name] = target

    def resolve(self, s: ast.ImportFrom) -> str | None:
        if not s.level:
            return s.module
        parts = self.package.split(".")
        if s.level - 1 > len(parts):
            return None
        base = ".".join(parts[:len(parts) - (s.level - 1)])
        return f"{base}.{s.module}" if s.module else base

    @staticmethod
    def plain_target(t: ast.expr) -> bool:
        if isinstance(t, ast.Name):
            return True
        if isinstance(t, (ast.Tuple, ast.List)):
            return all(_Module.plain_target(e) for e in t.elts)
        return False

    # ── expressions ─────────────────────────────────────────────────────

    def root(self, e: ast.expr) -> tuple[str | None, list[str]]:
        """(kind of the root name, dotted parts) of a Name / Attribute chain."""
        parts = []
        while isinstance(e, ast.Attribute):
            parts.append(e.attr)
            e = e.value
        if not isinstance(e, ast.Name):
            return None, []
        parts.append(e.id)
        parts.reverse()
        kind = self.kinds.get(e.id, "builtin" if e.id in BUILTINS or e.id == "__name__"
                              else None)
        return kind, parts

    def dotted(self, e: ast.expr) -> str | None:
        kind, parts = self.root(e)
        if kind is None or not parts:
            return None
        if kind in ("module", "ext", "sqlglot", "testobj"):
            return ".".join([self.targets[parts[0]]] + parts[1:])
        return ".".join(parts)

    def sqlglot_rooted(self, e: ast.expr) -> bool:
        """Could evaluating / operating on `e` touch a sqlglot object (not a module)?"""
        for n in ast.walk(e):
            if isinstance(n, (ast.Name, ast.Attribute)):
                kind, parts = self.root(n)
                if kind == "sqlglot":
                    return True
                if kind == "testobj" and not self.checker.test_class_or_def(self.targets[parts[0]]):
                    return True  # a test module may re-export a sqlglot object
                if kind == "module" and len(parts) > 1:
                    full = self.dotted(n)
                    if full and full.split(".")[0] == "sqlglot" and not self.checker.is_module(full):
                        return True
        return False

    def expr(self, e: ast.expr | None) -> bool:
        if e is None or isinstance(e, ast.Constant):
            return True
        if isinstance(e, ast.Name):
            return True
        if isinstance(e, ast.Attribute):
            # Loading an attribute runs code only through a metaclass (properties,
            # __getattr__) or a module __getattr__ (sqlglot's only import and return).
            kind, parts = self.root(e)
            if kind is None:
                return self.expr(_base(e))
            return not (set(parts[1:]) & self.checker.meta_names())
        if isinstance(e, (ast.Tuple, ast.List)):
            return all(self.expr(x) for x in e.elts)
        if isinstance(e, ast.Set):  # hashing a sqlglot class runs its metaclass __hash__
            return all(self.expr(x) and not self.sqlglot_rooted(x) for x in e.elts)
        if isinstance(e, ast.Dict):
            return (all(self.expr(x) and not self.sqlglot_rooted(x) for x in e.keys if x)
                    and all(self.expr(x) for x in e.values))
        if isinstance(e, ast.Lambda):
            return all(self.expr(d) for d in e.args.defaults + e.args.kw_defaults if d)
        if isinstance(e, ast.JoinedStr):
            return all(self.expr(v) for v in e.values)
        if isinstance(e, ast.FormattedValue):
            return not self.sqlglot_rooted(e.value) and self.expr(e.value)
        if isinstance(e, (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp)):
            kids = [c for c in ast.iter_child_nodes(e) if isinstance(c, ast.expr)]
            return all(self.expr(k) and not self.sqlglot_rooted(k) for k in kids)
        if isinstance(e, ast.Subscript):
            return (not self.sqlglot_rooted(e.value) and self.expr(e.value)
                    and self.expr(e.slice))
        if isinstance(e, ast.Starred):
            return self.expr(e.value)
        if isinstance(e, ast.Call):
            return self.call(e)
        return False

    def call(self, c: ast.Call) -> bool:
        args = c.args + [k.value for k in c.keywords]
        if not all(self.expr(a) for a in args):
            return False
        f = c.func
        if isinstance(f, ast.Attribute) and f.attr in STR_METHODS and (
                isinstance(f.value, ast.Constant)
                or (isinstance(f.value, ast.Call) and self.call(f.value))):
            return True  # a str method on a literal or an allowed call's result
        full = self.dotted(f)
        if full is None:
            return False
        kind, parts = self.root(f)
        if kind == "builtin" and len(parts) == 1:
            if parts[0] in ("getattr", "hasattr"):
                # attribute lookup by a constant name on a module object or a plain value
                return (len(c.args) >= 2 and isinstance(c.args[1], ast.Constant)
                        and not self.sqlglot_rooted(c.args[0]))
            return not any(self.sqlglot_rooted(a) for a in args)
        if kind == "ext" and full in ALLOWED_CALLS:
            return True
        if kind == "def" and len(parts) == 1:
            return self.checker.helper_ok(self, self.defs[parts[0]]) and not any(
                self.sqlglot_rooted(a) for a in args)
        if kind == "testobj" and len(parts) == 1:
            owner, _, name = full.rpartition(".")
            other = self.checker.module(owner)
            return (other is not None and name in other.defs
                    and self.checker.helper_ok(other, other.defs[name])
                    and not any(self.sqlglot_rooted(a) for a in args))
        return False


class Checker:
    def __init__(self, root) -> None:
        self.root = root  # the source checkout (sqlglot/ and tests/)
        self._done: dict[str, _Module | None] = {}
        self._ok: dict[str, bool] = {}
        self._helpers: dict[int, bool] = {}
        self._files: dict[str, object] = {}
        self._meta: set[str] | None = None
        self._getattrs: list | None | bool = False

    def _file(self, name: str):
        if name not in self._files:
            rel = name.replace(".", "/")
            found = None
            for cand in (f"{rel}.py", f"{rel}/__init__.py"):
                if (self.root / cand).is_file():
                    found = self.root / cand
                    break
            self._files[name] = found
        return self._files[name]

    def source_of(self, name: str) -> str | None:
        f = self._file(name)
        return f.read_text(errors="replace") if f is not None else None

    def test_class_or_def(self, full: str) -> bool:
        """`full` (module.name) is bound once at top level of an explained test module, by a
        def, a class, or an assignment whose value involves no sqlglot object."""
        owner, _, name = full.rpartition(".")
        m = self.module(owner)
        if m is None or not self.explained(owner):
            return False
        bound = [s for s in m.tree.body if name in _bound_names(s)]
        if len(bound) != 1:
            return False
        if name in m.defs or name in m.classes:
            return True
        s = bound[0]  # `NAME = <value>`: a plain value unless it involves a sqlglot object
        return (isinstance(s, (ast.Assign, ast.AnnAssign)) and s.value is not None
                and not m.sqlglot_rooted(s.value))

    def meta_names(self) -> set[str]:
        """Names defined in the body of any sqlglot class deriving from `type` (metaclass
        properties and methods run when looked up on a class)."""
        if self._meta is None:
            self._meta = {"__getattr__", "__getattribute__"}
            for f in (self.root / "sqlglot").rglob("*.py"):
                text = f.read_text(errors="replace")
                if "(type)" not in text and "Meta" not in text:
                    continue
                for node in ast.walk(ast.parse(text)):
                    if isinstance(node, ast.ClassDef) and any(
                            (isinstance(b, ast.Name) and (b.id == "type" or "Meta" in b.id))
                            or (isinstance(b, ast.Attribute) and "Meta" in b.attr)
                            for b in node.bases):
                        self._meta |= {n.name for n in node.body
                                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        return self._meta

    def is_module(self, name: str) -> bool:
        return name.split(".")[0] in ("sqlglot", "tests") and self._file(name) is not None

    def is_package(self, name: str) -> bool:
        f = self._file(name)
        return f is not None and f.name == "__init__.py"

    def module(self, name: str) -> _Module | None:
        if name not in self._done:
            source = self.source_of(name)
            self._done[name] = _Module(self, name, ast.parse(source)) if source is not None else None
        return self._done[name]

    def imported(self, name: str) -> bool:
        """Importing `name` runs no test-module code of concern (non-test modules: yes)."""
        if name.split(".")[0] != "tests":
            return True
        parts = name.split(".")
        return all(self.explained(".".join(parts[:i])) for i in range(1, len(parts) + 1))

    def explained(self, name: str) -> bool:
        if name.split(".")[0] != "tests":
            return True
        if name not in self._ok:
            self._ok[name] = False  # an import cycle: not explained
            m = self.module(name)
            parent = name.rpartition(".")[0]
            self._ok[name] = (m is not None and (not parent or self.explained(parent))
                              and m.ok())
        return self._ok[name]

    def explained_for(self, name: str, path: str, first: int, last: int) -> bool:
        """`explained(name)`, for an edit of lines path:first..last. Test modules do call one
        kind of sqlglot code: a package's module-level `__getattr__` (`from sqlglot.dialects
        import BigQuery`), which the state snapshot never calls. Those only import a module
        and return / cache one of its attributes – unless the edit is in one of them."""
        if name.split(".")[0] != "tests":
            return True
        spans = self.lazy_getattrs()
        if spans is None:
            return False
        if any(p == path and a <= last and first <= b for p, a, b in spans):
            return False
        return self.explained(name)

    def lazy_getattrs(self) -> list[tuple[str, int, int]] | None:
        """(path, first, last) of every module-level `__getattr__` in sqlglot/; None when one
        calls anything but importlib.import_module / getattr / dict.get / globals."""
        if self._getattrs is False:
            spans: list[tuple[str, int, int]] | None = []
            for f in sorted((self.root / "sqlglot").rglob("*.py")):
                text = f.read_text(errors="replace")
                if "def __getattr__" not in text:
                    continue
                for node in ast.parse(text).body:
                    if not (isinstance(node, ast.FunctionDef) and node.name == "__getattr__"):
                        continue
                    for n in ast.walk(node):
                        if isinstance(n, ast.Call) and not (
                                (isinstance(n.func, ast.Name)
                                 and n.func.id in ("getattr", "globals", "AttributeError"))
                                or (isinstance(n.func, ast.Attribute)
                                    and n.func.attr in ("import_module", "get"))):
                            spans = None
                            break
                    if spans is None:
                        break
                    spans.append((str(f.relative_to(self.root)), node.lineno, node.end_lineno))
                if spans is None:
                    break
            self._getattrs = spans
        return self._getattrs

    def helper_ok(self, m: _Module, f: ast.FunctionDef) -> bool:
        """A test helper whose body only uses builtins and str methods (no other calls, no
        sqlglot names): it cannot run sqlglot code given plain or module arguments."""
        if id(f) not in self._helpers:
            ok = True
            for n in ast.walk(f):
                if isinstance(n, (ast.Name, ast.Attribute)) and m.root(n)[0] in ("sqlglot", "module"):
                    ok = False
                elif isinstance(n, ast.Call):
                    g = n.func
                    if isinstance(g, ast.Name) and g.id in BUILTINS:
                        if g.id in ("getattr", "hasattr") and not (
                                len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)):
                            ok = False
                    elif not (isinstance(g, ast.Attribute) and g.attr in STR_METHODS):
                        ok = False
                elif isinstance(n, (ast.FunctionDef, ast.Lambda, ast.ClassDef)) and n is not f:
                    ok = False
            self._helpers[id(f)] = ok
        return self._helpers[id(f)]


def _bound_names(s: ast.stmt) -> set[str]:
    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {s.name}
    if isinstance(s, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in s.names}
    return {n.id for n in ast.walk(s) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}


def _base(e: ast.expr) -> ast.expr:
    while isinstance(e, ast.Attribute):
        e = e.value
    return e


def _is_main_guard(test: ast.expr) -> bool:
    return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
            and test.left.id == "__name__" and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__")
