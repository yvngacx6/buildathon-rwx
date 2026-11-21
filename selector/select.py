"""Turn one patch into a set of test numbers (the change-kind table in research/PLAN.md §3).

Each changed file gives a selection; the commit's selection is their union, and any file
that answers ALL makes the commit ALL. Every rule is about code structure (where the edit
is, which tests executed / instantiated / read / import it at the pinned commit); nothing
here knows which tests any particular edit breaks.

  file                                   selection
  ─────────────────────────────────────  ───────────────────────────────────────────────
  .py, same syntax tree (or docstrings)  nothing
  .py that does not parse after patch    ALL
  sqlglot/ function body                 tests that ran any line of the function (lines map)
    … slow tests (≥ SLOW_SECONDS)        only if they ran a touched statement (see below)
    … signature / decorator              + users of the enclosing classes (classes map)
  sqlglot/ class body                    users of the class chain ∪ tests that ran the file
  sqlglot/ module level                  users of the module's classes ∪ tests that ran the file
                                         (ALL if the changed names are used in other modules)
  base class / module body (BASE_FILES)  ALL
  tests/ test function                   that test (and same-named inherited copies)
  tests/ other test-file code            the file's tests ∪ importers (test_imports map)
  tests/ helper module                   importers (test_imports map)
  tests/fixtures/**                      tests that read the file (reads map); never AST-filtered
  new .py file (not conftest)            nothing: nothing at the pinned commit imports it
  deleted / renamed .py file             ALL
  docs, *.md, benchmarks/, posts/        nothing
  pyproject.toml / setup.cfg             nothing, unless a pytest section changes → ALL
  anything else, or a hunk that fails    ALL

Then: tests no map knows about join every non-empty selection, and a selection that would
cost nearly as much as ALL becomes ALL (same price, no risk).

Slow tests in function bodies. A test that never executes the statements an edit touches
runs exactly the code it ran before, so it cannot notice the edit. Cheap tests stay at the
whole-function rule (they cost almost nothing, and the function span absorbs any line-level
noise in the map). A slow test (≥ SLOW_SECONDS, from tests.json) is kept only if the lines
map shows it ran a line of a touched statement: the whole statement for simple statements,
the header lines for an if / for / while whose header alone changed. The statement view
exists only for clean in-place edits (classify.touched_statements); otherwise, or when no
test at all ran the touched statements, slow tests stay at the whole-function rule.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path, PurePosixPath

from . import classify, diff
from .maps import Maps

# Class and module bodies of these files shape nearly every test (map gives ~1550 of 1620).
BASE_FILES = {
    "sqlglot/__init__.py",
    "sqlglot/expressions/core.py",
    "sqlglot/dialects/dialect.py",
    "sqlglot/tokens.py",
    "sqlglot/parser.py",
    "sqlglot/generator.py",
}
DOC_DIRS = ("docs/", "posts/", "benchmarks/", ".github/")
DOC_SUFFIXES = (".md", ".rst", ".txt", ".html", ".png", ".svg", ".jpg")
DOC_FILES = {"LICENSE", "Makefile", "CHANGELOG", ".gitignore", ".pre-commit-config.yaml"}
CONFIG_FILES = {"pyproject.toml", "setup.cfg"}
ALWAYS_ALL = {"pytest.ini", "tox.ini", "conftest.py", "setup.py"}
ALL_FRACTION = 0.85  # selection cost ≥ this × ALL cost → just run ALL
SLOW_SECONDS = 5.0  # tests at least this slow get the statement-level rule in function bodies


class AllTests(Exception):
    """Raised by a rule that cannot narrow the selection; the message is the reason."""


class Selector:
    def __init__(self, tests: list[dict], sqlglot: Path, maps: Path) -> None:
        self.tests = tests
        self.sqlglot = sqlglot
        self.maps = Maps(maps, tests)
        self._all = {t["number"] for t in tests}
        self._by_file: dict[str, set[int]] = {}
        self._id_of: dict[int, str] = {}
        for t in tests:
            self._by_file.setdefault(t["file"], set()).add(t["number"])
            self._id_of[t["number"]] = t["id"]
        self._number_of = {t["id"]: t["number"] for t in tests}
        self._slow = {t["number"] for t in tests if t["seconds"] >= SLOW_SECONDS}
        self._untraced: set[int] | None = None
        self._sources: dict[str, str] | None = None

    def all_numbers(self) -> set[int]:
        return set(self._all)

    # ── entry point ────────────────────────────────────────────────────────────

    def select(self, patch_text: str) -> tuple[set[int], str]:
        """Return (test numbers, human-readable reason)."""
        files = diff.parse(patch_text)
        if not files:
            return self.all_numbers(), "ALL (no files parsed)"
        chosen: set[int] = set()
        reasons = []
        try:
            for fp in files:
                got, why = self._select_file(fp)
                chosen |= got
                reasons.append(f"{fp.path}: {why} [{len(got)}]")
        except AllTests as exc:
            return self.all_numbers(), f"ALL ({exc})"
        if chosen:
            if self._untraced is None:
                self._untraced = self.maps.untraced()
            chosen |= self._untraced
        return chosen & self._all, "; ".join(reasons)

    # ── one file ──────────────────────────────────────────────────────────────

    def _select_file(self, fp: diff.FilePatch) -> tuple[set[int], str]:
        path = fp.path
        name = PurePosixPath(path).name
        if path.startswith("tests/fixtures/") or (fp.old_path or "").startswith("tests/fixtures/"):
            # Fixture text is data: a '#' line can be metadata, so never AST-filter it.
            # A rename or delete breaks the readers of the old path too.
            readers = set()
            for p in {fp.old_path, fp.new_path} - {None}:
                readers |= self.maps.reads.get(p)
            return readers, "fixture → readers"
        if fp.old_path and fp.new_path and fp.old_path != fp.new_path:
            raise AllTests(f"{path} renamed")
        if name in ALWAYS_ALL:
            raise AllTests(f"{path} is test configuration")
        if path.startswith(DOC_DIRS) or path.endswith(DOC_SUFFIXES) or name in DOC_FILES:
            return set(), "docs"
        if name in CONFIG_FILES:
            return self._config(fp)
        if fp.is_binary or not path.endswith(".py"):
            readers = self.maps.reads.get(path)
            if readers:
                return set(readers), "data file → readers"
            raise AllTests(f"{path}: unknown file kind")
        if fp.old_path is None:
            if path.startswith(("sqlglot/", "tests/")) and name != "__init__.py":
                return set(), "new module, nothing imports it"
            raise AllTests(f"{path}: new file outside sqlglot/ and tests/")
        if fp.new_path is None:
            raise AllTests(f"{path} deleted")
        return self._python(fp)

    def _config(self, fp: diff.FilePatch) -> tuple[set[int], str]:
        """pyproject.toml / setup.cfg: only pytest sections can change a test run."""
        old_text = self._old_text(fp.path)
        new_text = diff.apply(old_text, fp)
        removed, added = diff.changed_lines(fp)
        for text, lines in ((old_text, removed), (new_text, added)):
            rows = text.splitlines()
            for line in lines:
                section = ""
                for row in rows[:line]:
                    if row.strip().startswith("["):
                        section = row.strip()
                if "pytest" in section or "pytest" in rows[line - 1]:
                    raise AllTests(f"{fp.path}: pytest configuration changed")
        return set(), "config, no pytest section"

    def _python(self, fp: diff.FilePatch) -> tuple[set[int], str]:
        path = fp.path
        old_bytes = (self.sqlglot / path).read_bytes()
        try:
            old_text = old_bytes.decode("utf-8")
            new_text = diff.apply(old_text, fp)
            old_tree = ast.parse(old_bytes)
            new_tree = ast.parse(new_text.encode("utf-8"))
            compile(new_tree, path, "exec")
        except (diff.PatchError, SyntaxError, ValueError, UnicodeDecodeError) as exc:
            raise AllTests(f"{path}: {type(exc).__name__}") from None
        classify.strip_docstrings(old_tree)
        classify.strip_docstrings(new_tree)
        if ast.dump(old_tree) == ast.dump(new_tree):
            return set(), "same syntax tree (ignoring comments and docstrings)"
        removed, added = diff.changed_lines(fp)
        found = classify.regions(old_tree, new_tree, removed, added)
        if not found:
            return set(), "no behaviour-changing region"
        if path.startswith("tests/"):
            return self._test_code(path, found)
        if path.startswith("sqlglot/"):
            return self._library(path, old_tree, new_tree, found)
        raise AllTests(f"{path}: .py outside sqlglot/ and tests/")

    # ── tests/*.py ────────────────────────────────────────────────────────────

    def _test_code(self, path: str, found: list[classify.Region]) -> tuple[set[int], str]:
        if name_of(path) == "__init__.py":
            raise AllTests(f"{path}: test package init")
        importers = set(self.maps.test_imports.get(path))
        own = self._by_file.get(path, set())
        if not name_of(path).startswith("test_"):  # a helper module
            if not importers:
                raise AllTests(f"{path}: helper with no known importers")
            return importers | own, "test helper → importing test files"
        chosen: set[int] = set()
        whole_file = False
        for r in found:
            test_name = r.chain[-1] if r.kind == "function" and r.chain else ""
            if r.kind == "function" and test_name.startswith("test") and len(r.chain) <= 2:
                suffix = "::" + "::".join(r.chain)
                chosen |= {n for n in own if self._id_of[n].endswith(suffix)}
                method = "::" + test_name  # copies inherited by test classes in importers
                chosen |= {n for n in importers if self._id_of[n].endswith(method)}
            else:
                whole_file = True
        if whole_file:
            return own | importers | chosen, "test-file setup / helper code → whole file"
        return chosen, "test functions"

    # ── sqlglot/*.py ──────────────────────────────────────────────────────────

    def _library(self, path: str, old_tree: ast.Module, new_tree: ast.Module,
                 found: list[classify.Region]) -> tuple[set[int], str]:
        module = _module_of(path)
        chosen: set[int] = set(self.maps.reads.get(path))
        kinds = []
        for r in found:
            if r.kind == "function":
                ran = self.maps.span(path, *r.span)
                got = self._slow_tests_off_statements(path, ran, r)
                if r.signature:
                    got |= self._class_users(module, r.classes)
                if not got:
                    # Never ran under a test: may run only at import or class creation.
                    # Treat it as a change to the scope that defines it.
                    got = self._definition_time(path, module, old_tree, new_tree, r)
                    kinds.append(f"unrun def {'.'.join(r.chain) or '<lambda>'} → definition-time")
                else:
                    dropped = len(ran - got)
                    kinds.append(f"def {'.'.join(r.chain) or '<lambda>'}"
                                 + (f" (-{dropped} slow, off touched statements)" if dropped else ""))
            else:
                got = self._definition_time(path, module, old_tree, new_tree, r)
                kinds.append(f"{r.kind} {'.'.join(r.chain) or module}")
            chosen |= got
        return chosen, ", ".join(kinds)

    def _slow_tests_off_statements(self, path: str, ran_function: set[int],
                                   r: classify.Region) -> set[int]:
        """Drop the slow tests that ran the function but none of the touched statements."""
        slow = ran_function & self._slow
        if not slow or r.statements is None:
            return ran_function
        ran_statements: set[int] = set()
        for first, last in r.statements:
            ran_statements |= self.maps.span(path, first, last)
        if not ran_statements:
            return ran_function  # no test ran them: map blind spot, keep the function rule
        return ran_function - (slow - ran_statements)

    def _definition_time(self, path: str, module: str, old_tree: ast.Module,
                         new_tree: ast.Module, r: classify.Region) -> set[int]:
        """Class or module body: runs once at import, so line coverage cannot see who uses it."""
        if path in BASE_FILES:
            raise AllTests(f"{path}: base class / module body")
        got = set(self.maps.whole_file(path))
        users = self._class_users(module, r.classes) if r.classes else set()
        if not users:
            if r.kind == "module" or not r.classes:
                got |= self._importer_users(path, old_tree, new_tree)
            users = self.maps.module_users(module, classify.top_level_classes(old_tree))
        got |= users
        if not got:
            raise AllTests(f"{path}: definition-time change with no known users")
        return got

    def _class_users(self, module: str, chains) -> set[int]:
        out: set[int] = set()
        for chain in chains:
            for cut in range(1, len(chain) + 1):  # outer classes copy nested-class attributes
                out |= self.maps.classes.get(f"{module}.{'.'.join(chain[:cut])}")
        return out

    def _importer_users(self, path: str, old_tree: ast.Module, new_tree: ast.Module) -> set[int]:
        """Module-level change: tests reached through modules that import the changed names.

        A module-level name can only be seen elsewhere by importing it from this module (or
        from a module that re-imports it). For each such importer we take the tests that ran
        it or used its classes. Anything we cannot follow statically → ALL.
        """
        old = {ast.dump(s): s for s in old_tree.body}
        new = {ast.dump(s): s for s in new_tree.body}
        changed = [s for k, s in old.items() if k not in new] + [s for k, s in new.items() if k not in old]
        names: set[str] = set()
        for stmt in changed:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # handled as function / class regions
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                raise AllTests(f"{path}: module-level {type(stmt).__name__} changed")
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for t in targets:
                names |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        got: set[int] = set()
        seen, todo = {path}, [path]
        while todo:  # follow re-imports
            source = todo.pop()
            for other in self._importers(source, names):
                if other in seen:
                    continue
                seen.add(other)
                if other in BASE_FILES or other.endswith("__init__.py"):
                    raise AllTests(f"{path}: names re-exported by {other}")
                if other.startswith("tests/"):
                    got |= self._by_file.get(other, set()) | self.maps.test_imports.get(other)
                    continue
                users = self.maps.whole_file(other) | self.maps.module_users(
                    _module_of(other), classify.top_level_classes(ast.parse(self._all_sources()[other])))
                if not users:
                    raise AllTests(f"{path}: names imported by {other}, which has no known users")
                got |= users
                todo.append(other)
        return got

    def _importers(self, path: str, names: set[str]) -> list[str]:
        """Files that import module `path` and mention one of `names`."""
        module = _module_of(path)
        parent, _, leaf = module.rpartition(".")
        refers = re.compile(rf"\b{re.escape(module)}\b|from\s+{re.escape(parent)}\s+import\b[^\n]*\b{leaf}\b"
                            rf"|from\s+\.+{leaf}\s+import")
        mentions = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names))) + r")\b") if names else None
        out = []
        for other, text in self._all_sources().items():
            if other == path or not refers.search(text):
                continue
            if f"{module} import *" in text or f".{leaf} import *" in text:
                raise AllTests(f"{path}: star-imported by {other}")
            if mentions is not None and mentions.search(text):
                out.append(other)
        return out

    # ── helpers ───────────────────────────────────────────────────────────────

    def _all_sources(self) -> dict[str, str]:
        if self._sources is None:
            self._sources = {}
            for root in ("sqlglot", "tests"):
                for p in (self.sqlglot / root).rglob("*.py"):
                    self._sources[p.relative_to(self.sqlglot).as_posix()] = p.read_text(
                        errors="replace")
        return self._sources

    def _old_text(self, path: str) -> str:
        source = self.sqlglot / path
        return source.read_text() if source.is_file() else ""


def _module_of(path: str) -> str:
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def name_of(path: str) -> str:
    return PurePosixPath(path).name
