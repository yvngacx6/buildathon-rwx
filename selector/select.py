"""Turn one patch into a set of test numbers (the change-kind table in research/PLAN.md §3).

Each changed file gives a selection; the commit's selection is their union, and any file
that answers ALL makes the commit ALL. Every rule is about code structure (where the edit
is, which tests executed / instantiated / read / import it at the pinned commit); nothing
here knows which tests any particular edit breaks.

  file                                   selection
  ─────────────────────────────────────  ───────────────────────────────────────────────
  .py, same syntax tree (or docstrings)  nothing
  .py that does not parse after patch    import bound: tests that load the module ∪ read it
  sqlglot/ function body                 tests that ran any line of the function (lines map)
    … slow tests (≥ SLOW_SECONDS)        only if they ran a touched statement (see below)
    … signature / decorator              + users of the enclosing classes (classes map)
  sqlglot/ class body / module level,    tests that ran a line reading a changed name, ∩ the
    assignments with inert new values    conservative set below; ∪ test-file reads; a changed
                                         lambda body → tests that called it (selector/names.py)
    … else, values consumed at class     import old and patched package, diff all class /
      creation / import (metaclass,      module values (selector/state.py); per changed value:
      __init_subclass__, import calls)   tests that ran a run-time reader, among the tests that
                                         can hold the owner class (`_by_state`)
  sqlglot/ class body (otherwise)        users of the class chain ∪ tests that ran the file
  sqlglot/ module level (otherwise)      users of the module's classes ∪ tests that ran the file
                                         (ALL if the changed names are used in other modules)
  base class / module body (BASE_FILES)  ALL
  tests/ test function                   that test (and same-named inherited copies)
  tests/ other test-file code            the file's tests ∪ importers (test_imports map)
  tests/ helper module                   importers (test_imports map)
  tests/fixtures/**                      tests that read the file (reads map); never AST-filtered
  new .py file (not conftest)            nothing: nothing at the pinned commit imports it
  deleted / renamed .py file             import bound of the old path
  any rule above that gives up (removed  import bound of the module (instead of ALL)
    binding, changed import, no known
    users, base-class body, …)
  docs, *.md, benchmarks/, posts/        nothing
  pyproject.toml / setup.cfg             nothing, unless a pytest section changes → ALL
  anything else, or a hunk that fails    ALL

Import bound (imports map, mapbuild/import_map.py): each test was run alone in a fresh
process and every module it loaded was recorded (collection-time closure of its file, eager
`import sqlglot` set, lazy dialect loads, function-level imports, subprocesses). An edit
confined to one module can only reach tests that load it or read it as a file; a module
that fails to import breaks exactly those. Tests that fail when run alone, or that the trace
does not know, are always added. Without the map, or for a hunk that does not apply: ALL.
Only for modules loaded lazily at run time: a module loaded while pytest collects the test
files (`pytest --collect-only tests`, traced at map build) would fail at collection, and
pytest's default interrupts the whole session → ALL. A deleted / renamed test file that no
other module imports is not a collection error (its tests just vanish) → import bound.

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
import os
import re
from pathlib import Path, PurePosixPath

from . import classify, diff, nameindex, names, pack
from . import maps as maps_module
from .maps import ImportTimeMap, Maps

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
# An import-state diff costs ~1.2 s of container CPU (EC2, pinned image). Run it only when the
# wide set costs at least this much more than the cheapest selection it could give.
STATE_MIN_GAIN = 2.0
# Class hierarchies the classes map records (maps/MANIFEST.json notes): a test that made an
# instance of one of these classes (or used it as a dialect) is credited to it and its bases.
TRACKED_ROOTS = {
    "sqlglot.dialects.dialect:Dialect",
    "sqlglot.parser:Parser",
    "sqlglot.generator:Generator",
    "sqlglot.tokens:_TokenizerBase",
    "sqlglot.expressions.core:Expr",
}


class AllTests(Exception):
    """Raised by a rule that cannot narrow the selection; the message is the reason."""


class Selector:
    def __init__(self, tests: list[dict], sqlglot: Path, maps: Path) -> None:
        self.tests = tests
        self.sqlglot = sqlglot
        self.maps = Maps(maps, tests, Path(os.environ.get("BR_MAPS_COMPILED", maps_module.COMPILED)))
        self._all = {t["number"] for t in tests}
        self._by_file: dict[str, set[int]] = {}
        self._id_of: dict[int, str] = {}
        for t in tests:
            self._by_file.setdefault(t["file"], set()).add(t["number"])
            self._id_of[t["number"]] = t["id"]
        self._number_of = {t["id"]: t["number"] for t in tests}
        self._secs = {t["number"]: float(t.get("seconds", 0.0)) for t in tests}
        self._slow = {n for n, s in self._secs.items() if s >= SLOW_SECONDS}
        self._untraced: set[int] | None = None
        self._sources: dict[str, str] | None = None
        self._some_sources: dict[str, str | None] = {}  # `_source` before `_all_sources`
        self._classes_of: dict[str, list[str]] = {}
        self._old_trees: dict[str, tuple[ast.Module, list[str] | None]] = {}
        self._scopes: dict[str, classify.Scopes] = {}
        self._name_index: nameindex.NameIndex | None = None
        self._files: list[diff.FilePatch] = []  # the current commit's files
        self._state: dict | None | bool = False  # its import-state diff (False: not yet run)
        self._direct: dict[str, bool] | None = None  # names `_by_names` saw edited
        self.notes: list[str] = []  # why a definition-time region was (not) narrowed
        self._dependents: dict[str, set[int]] = {}

    def all_numbers(self) -> set[int]:
        return set(self._all)

    # ── entry point ────────────────────────────────────────────────────────────

    def select(self, patch_text: str) -> tuple[set[int], str]:
        """Return (test numbers, human-readable reason)."""
        files = diff.parse(patch_text)
        if not files:
            return self.all_numbers(), "ALL (no files parsed)"
        self._files, self._state = files, False
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
            # The old module is gone; nothing at the pinned commit imports the new path –
            # unless the new path is one pytest or Python picks up by itself.
            new = fp.new_path
            if not (new.endswith(".py") and new.startswith(("sqlglot/", "tests/"))
                    and name_of(new) not in ALWAYS_ALL and name_of(new) != "__init__.py"):
                raise AllTests(f"{fp.old_path} renamed to {new}")
            return self._import_bound(fp.old_path, f"{fp.old_path} renamed", removed=True)
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
            return self._import_bound(path, f"{path} deleted", removed=True)
        try:
            return self._python(fp)
        except AllTests as exc:
            if str(exc).startswith(f"{path}: hunk"):
                raise
            # Every narrower rule gave up: fall back to the import bound of this one module.
            return self._import_bound(path, str(exc))

    def _import_bound(self, path: str, why: str, removed: bool = False) -> tuple[set[int], str]:
        """Every test an edit confined to module `path` can reach: the tests that load it
        (imports map: its import closure, traced per test in a fresh process, lazy loads
        included) ∪ the tests that read it as a file. If it stops importing, these break.

        Only for modules loaded lazily, after collection: if the module is loaded while
        pytest collects the test files, a failure there is a collection error, and pytest's
        default interrupts the whole session → ALL. A removed (deleted / renamed) test file
        that no other module imports is not a collection error: its tests just vanish."""
        loaders = self.maps.loaders(path)
        if (loaders is None or not path.endswith(".py") or name_of(path) in ALWAYS_ALL
                or not path.startswith(("sqlglot/", "tests/"))):
            raise AllTests(why)
        if self.maps.collected(path, by_import=removed):
            raise AllTests(f"{why}; {path} is loaded at collection")
        if removed and path.startswith("tests/"):
            # A deleted/renamed test file: if the labeler passes explicit node ids, pytest
            # stops with "file or directory not found" for the whole run. Unknown → ALL.
            raise AllTests(f"{why}; removed test file")
        return loaders | self.maps.reads.get(path), f"{why} → tests that load it (run time)"

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
        except (diff.PatchError, UnicodeDecodeError) as exc:
            raise AllTests(f"{path}: hunk does not apply ({type(exc).__name__})") from None
        same = classify.SameAst(old_text, new_text)
        try:
            old_tree = self._old_tree(path, old_bytes)
            new_tree = ast.parse(new_text.encode("utf-8"))
            _check_compiles(path, old_tree, new_tree, same)
        except (SyntaxError, ValueError) as exc:  # the module no longer imports
            raise AllTests(f"{path}: {type(exc).__name__}") from None
        classify.strip_docstrings(new_tree)
        if same(old_tree, new_tree):
            return set(), "same syntax tree (ignoring comments and docstrings)"
        lost = _bindings(old_tree) - _bindings(new_tree)
        if lost:
            # A removed/renamed module- or class-level name breaks every importer or caller
            # (ImportError at collection, AttributeError at call) – far beyond its own tests.
            raise AllTests(f"{path}: binding removed ({', '.join(sorted(lost)[:3])})")
        removed, added = diff.changed_lines(fp)
        found = classify.regions(old_tree, new_tree, removed, added, same, self._old_scopes(path))
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
                # Called while some module is imported (trie builders, decorator factories,
                # `X = build()` tables, module __getattr__): every importer depends on it,
                # which no per-test coverage can show.
                # A lambda's first line is where it is created (class-body dicts run it at
                # import); only its later lines are body that can run at import.
                first, last = r.span
                at_import = self._import_time(path, first + r.is_lambda, last)
                if not got:
                    # Never ran under a test: may run only at import or class creation.
                    # Treat it as a change to the scope that defines it.
                    got = self._definition_time(path, module, old_tree, new_tree, r)
                    kinds.append(f"unrun def {'.'.join(r.chain) or '<lambda>'} → definition-time")
                else:
                    dropped = len(ran - got)
                    kinds.append(f"def {'.'.join(r.chain) or '<lambda>'}"
                                 + (f" (-{dropped} slow, off touched statements)" if dropped else ""))
                if at_import:
                    kinds.append(f"(runs at import: +{len(at_import - got)})")
                got |= at_import
            else:
                self.notes.clear()
                got = self._definition_time(path, module, old_tree, new_tree, r)
                note = f" [{'; '.join(self.notes)}]" if self.notes else ""
                kinds.append(f"{r.kind} {'.'.join(r.chain) or module}{note}")
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
        """Class or module body: runs once at import, so line coverage cannot see who uses it.

        First try to narrow by the changed names (`_by_names`); if that cannot be proven
        safe, take the conservative set: users of the class chain ∪ tests that ran the file.
        """
        if path in BASE_FILES:
            raise AllTests(f"{path}: base class / module body")
        try:
            wide: set[int] | None = self._conservative(path, module, old_tree, new_tree, r)
            wide_error = None
        except AllTests as exc:
            wide, wide_error = None, exc
        if wide_error is not None:
            raise wide_error  # v1 could not bound it (star import, re-export, …): never narrow
        if r.kind in ("class", "module") and not r.via_def:
            self._direct = None
            try:
                got, why = self._by_names(path, old_tree, new_tree, r, wide)
                if wide is not None and self._seconds(got) >= self._seconds(wide):
                    raise names.Fallback(f"{why}, but no cheaper")
                self.notes.append(f"{'.'.join(r.chain) or module}: {why}")
                return got
            except names.Fallback as exc:
                self.notes.append(f"{'.'.join(r.chain) or module}: no narrowing ({exc})")
                try:
                    direct, self._direct = self._direct, None
                    if wide is not None:
                        # A selection is never cheaper than this floor (2 s if unknown).
                        floor = (self._cost(self._state_floor(path, r, direct, wide))
                                 if direct else 0.0)
                        if self._cost(wide) - max(floor, 2.0) < STATE_MIN_GAIN:
                            raise names.Fallback(f"at most {self._cost(wide) - max(floor, 2.0):.1f}"
                                                 " s to win: not worth an import-state diff")
                    got, why = self._by_state(path, r, wide)
                    if wide is not None and self._seconds(got) >= self._seconds(wide):
                        raise names.Fallback(f"{why}, but no cheaper")
                    self.notes.append(f"{'.'.join(r.chain) or module}: {why}")
                    return got
                except names.Fallback as exc2:
                    self.notes.append(f"{'.'.join(r.chain) or module}: no state narrowing "
                                      f"({exc2})")
        if wide_error is not None:
            raise wide_error
        return wide

    def _conservative(self, path: str, module: str, old_tree: ast.Module,
                      new_tree: ast.Module, r: classify.Region) -> set[int]:
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

    def _by_names(self, path: str, old_tree: ast.Module, new_tree: ast.Module,
                  r: classify.Region, wide: set[int] | None) -> tuple[set[int], str]:
        """Tests that can observe the edit, found by the names it changes (selector/names.py).

        = tests that ran a changed lambda
        ∪ (tests that ran a line reading a changed name in sqlglot/) ∩ the conservative set
        ∪ tests whose own code reads a changed name (tests/).
        Raises names.Fallback when any step cannot be proven safe.
        """
        index = self._index()
        if r.kind == "class":
            old_node = names._find_class(old_tree, r.chain)
            new_node = names._find_class(new_tree, r.chain)
            if old_node is None or new_node is None:
                raise names.Fallback("class added or removed")
            head = lambda c: [ast.dump(x) for x in c.bases + c.keywords + c.decorator_list]  # noqa: E731
            if head(old_node) != head(new_node):
                raise names.Fallback("class bases / keywords / decorators changed")
            old_body, new_body = old_node.body, new_node.body
        else:
            old_body, new_body = old_tree.body, new_tree.body
        change = names.body_change(old_body, new_body, old_tree)
        self._direct = change.names  # for `_state_floor`, if this falls back later
        direct: set[int] = set()
        for first, last in change.lambda_spans:  # a lambda body only runs when called
            got = self.maps.span(path, first, last)
            if not got:
                raise names.Fallback(f"lambda at line {first} never ran under a test")
            direct |= got
        readers: set[int] = set()
        in_tests: set[int] = set()
        touch = set(wide) if wide is not None else None
        todo = [(n, path, r.chain if r.kind == "class" else (), follow)
                for n, follow in change.names.items()]
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        n_sites = 0
        while todo:
            name, owner_path, owner_chain, follow = todo.pop()
            if (name, owner_path, owner_chain) in seen:
                continue
            seen.add((name, owner_path, owner_chain))
            if name.startswith("__") and name.endswith("__"):
                raise names.Fallback(f"{name} is a special name")
            if owner_chain:
                family = index.check_class(owner_path, owner_chain)
                blocker = index.handles(family)
                if blocker:
                    raise names.Fallback(blocker)
                if touch is not None and (owner_path, owner_chain) != (path, r.chain):
                    touch |= self.maps.whole_file(owner_path) | self._class_users(
                        _module_of(owner_path), [owner_chain])
            for site in index.sites(name, owner_path, owner_chain, name in change.new_names):
                n_sites += 1
                if site.path.startswith("tests/") and not site.dynamic:
                    if site.when == "run":
                        in_tests |= self.maps.span(site.path, *site.lines)
                    else:  # test-module import code: every test of that file depends on it
                        in_tests |= self._by_file.get(site.path, set()) | set(
                            self.maps.test_imports.get(site.path))
                elif site.when == "run" or (site.path.startswith("tests/") and site.dynamic):
                    readers |= self.maps.span(site.path, *site.lines)
                elif site.copy is not None and follow:
                    chain, targets = site.copy
                    todo.extend((t, site.path, chain, True) for t in targets)
                else:
                    raise names.Fallback(f"{name} read at import / in cached code ({site.what})")
        if change.names and not (readers or in_tests):
            raise names.Fallback(f"no test ran a line reading {sorted(change.names)}")
        if touch is not None:
            readers &= touch
        shown = ", ".join(sorted(change.names)) or "lambda bodies"
        return readers | in_tests | direct, f"names {shown} → {n_sites} reader sites"

    # ── import-time state (selector/state.py) ──────────────────────────────────

    def _state_diff(self) -> dict | None:
        """What the commit changes in the state sqlglot builds at import (once per commit)."""
        if self._state is False:
            patched = {}
            for fp in self._files:
                if fp.path.startswith("sqlglot/") and fp.path.endswith(".py") and fp.old_path:
                    try:
                        patched[fp.path] = diff.apply(self._old_text(fp.path), fp)
                    except diff.PatchError:
                        self._state = None
                        return None
            from . import state  # imported on first use: few commits need it

            snapshot = Path(os.environ.get("BR_STATE", state.DEFAULT_PATH))
            self._state = state.diff(self.sqlglot, snapshot, patched)
        return self._state  # type: ignore[return-value]

    def _by_state(self, path: str, r: classify.Region,
                  wide: set[int] | None) -> tuple[set[int], str]:
        """Tests that can observe what the edit changes in import-time state (idea I22).

        Class-creation consumers (metaclass `__new__`, `__init_subclass__`, calls handed the
        class at import, call-valued table entries) are followed by running them:
        `state.diff` imports the old and the patched package and lists every module global
        and class attribute whose value differs: the edited ones and all values derived
        from them. For each changed value, its readers (name sites, names.py):

          * run-time reads of a class attribute → tests that ran the line AND can hold the
            class: users of the class / its subclasses / its outer classes (classes map,
            which credits bases), or tests that ran a line naming the class;
          * … unless objects of the class are built at import, the hierarchy is not in the
            classes map, or the read is dynamic → tests that ran the line ∩ the users of
            the owner class / its file (as in `_by_names`);
          * run-time reads of a module global → tests that ran the line;
          * reads at import → their products are themselves in the diff (the line map still
            adds any test that ran the line later);
          * reads in code whose result is cached across tests → Fallback;
          * reads in test code → as in `_by_names`.
        """
        found = self._state_diff()
        if found is None:
            raise names.Fallback("import-state diff unavailable")
        changed = [k.split(" ", 2) for k in found["changed"]]
        own_module = _module_of(path)
        own = (f"{own_module}:{'.'.join(r.chain)}" if r.kind == "class" else own_module)
        if not any(kind == ("C" if r.kind == "class" else "M") and owner == own
                   for kind, owner, _ in changed):
            # the edit must show up: else the snapshot is blind to it
            raise names.Fallback(f"no import-time change seen in {own}")
        index = self._index()
        info, with_instances = found["info"], set(found["instances"])
        touch = set(wide) if wide is not None else set()
        free: set[int] = set()  # readers of module globals
        loose: set[int] = set()  # class-attribute readers restricted only by `touch`
        held: set[int] = set()  # class-attribute readers among tests that can hold the class
        in_tests: set[int] = set()
        n_sites = 0
        owners: dict[str, tuple] = {}
        for kind, owner, attr in changed:
            if attr.startswith("__") and attr.endswith("__"):
                raise names.Fallback(f"{owner}.{attr} is a special name")
            if kind == "M":
                owner_path = self._module_path(owner)
                if owner_path is None:
                    raise names.Fallback(f"module {owner} not found")
                chain: tuple[str, ...] = ()
                users = None
            else:
                if owner not in owners:
                    owners[owner] = self._class_owner(owner, info, with_instances, index)
                owner_path, chain, users = owners[owner]
                touch |= self.maps.whole_file(owner_path) | self._class_users(
                    _module_of(owner_path), [chain])
            for site in index.sites(attr, owner_path, chain, True):
                for bucket, got in self._site_reads(index, site, attr):
                    n_sites += 1
                    if bucket == "tests":
                        in_tests |= got
                    elif kind == "M":
                        free |= got
                    elif users is None or bucket == "loose":
                        loose |= got
                    else:
                        held |= got & users
        # Class-attribute reads stay within the users of the owner classes and their files
        # (the bound `_by_names` relies on); `held` narrows that further.
        got = free | ((loose | held) & touch) | in_tests
        if not got:
            raise names.Fallback("no test ran a line reading a changed value")
        shown = sorted({f"{o.rsplit(':', 1)[-1]}.{a}" for _, o, a in changed})
        more = f" +{len(shown) - 6}" if len(shown) > 6 else ""
        return got, (f"state: {', '.join(shown[:6])}{more} → {n_sites} reader sites "
                     f"(globals {len(free)}, unrestricted {len(loose & touch)}, "
                     f"held {len(held)}, tests {len(in_tests)})")

    def _site_reads(self, index: nameindex.NameIndex, site: names.Site, attr: str,
                    depth: int = 0) -> list[tuple[str, set[int]]]:
        """Tests that can observe the value read at `site`, as (bucket, tests): "tests" (test
        code reads it), "loose" (a dynamic read: any object), "held" (a read through an
        object of the owner). Follows `site.flow`: a value bound to a local that is bound
        once is observed where the local is loaded; a value handed to a constructor that only
        stores it is observed where that attribute is read."""
        if site.path.startswith("tests/") and not site.dynamic:
            if site.when == "run":
                return [("tests", self.maps.span(site.path, *site.lines))]
            # test-module import code: every test of that file depends on it
            return [("tests", self._by_file.get(site.path, set())
                     | set(self.maps.test_imports.get(site.path)))]
        if site.when == "def" and site.memo:
            raise names.Fallback(f"{attr} read in code cached across tests ({site.what})")
        bucket = "loose" if site.dynamic else "held"
        flow = site.flow if site.when == "run" and not site.dynamic and depth < 3 else None
        if flow is not None and flow[0] == "local":
            got: set[int] = set()
            for first, last in flow[1]:
                got |= self.maps.span(site.path, first, last)
            return [(bucket, got)]
        if flow is not None and flow[0] == "attr":
            _, path, chain, stored = flow
            out = []
            for inner in index.sites(stored, path, tuple(chain), False):
                out += self._site_reads(index, inner, stored, depth + 1)
            return out
        # "def" sites outside cached code run at import: their products are in the diff; the
        # line map still shows any test that ran them later.
        return [(bucket, self.maps.span(site.path, *site.lines))]

    def _module_path(self, module: str) -> str | None:
        base = module.replace(".", "/")
        for rel in (f"{base}.py", f"{base}/__init__.py"):
            if (self.sqlglot / rel).is_file():
                return rel
        return None

    def _class_owner(self, key: str, info: dict, with_instances: set[str],
                     index: nameindex.NameIndex) -> tuple[str, tuple[str, ...], set[int] | None]:
        """(source path, class chain, tests that can hold the class; None: do not restrict)
        for a changed class. A class made by `type(...)` at import (a dialect's default
        Tokenizer) stands for its nearest ancestor defined in the source."""
        meta = info.get(key)
        if not meta:
            raise names.Fallback(f"no class info for {key}")
        mro = [k for k in meta["mro"] if k]
        real = key if meta["real"] else next((k for k in mro if not k.endswith("#")), "")
        module, _, qual = real.partition(":")
        path = self._module_path(module) if module.startswith("sqlglot") else None
        if path is None or not qual or "<" in qual:
            raise names.Fallback(f"{key}: no class in the source")
        chain = tuple(qual.split("."))
        family = index.check_class(path, chain)  # Fallback: bases / decorators outside view
        if {key, real} & with_instances:
            return path, chain, None  # objects built at import: a test need not make one
        if not ({key, real, *mro} & TRACKED_ROOTS):
            return path, chain, None  # the classes map does not record this hierarchy
        return path, chain, self._holders(family, index)

    def _holders(self, family: list[tuple[str, tuple[str, ...]]],
                 index: nameindex.NameIndex) -> set[int]:
        """Tests that can hold one of these classes (or an object of one)."""
        users: set[int] = set()
        for p, ch in family:  # the class, its subclasses and their outer classes
            users |= self._class_users(_module_of(p), [ch])
            if len(ch) == 1:  # class attributes that hold it (`Parser = MySQLParser`)
                for hp, hch in index.holders(ch[0]):
                    users |= self._class_users(_module_of(hp), [hch])
            for site in index.mentions(ch[0]):  # lines that name the class (or its outer)
                if site.typecheck:
                    continue  # isinstance(x, C) does not hand C on
                users |= self.maps.span(site.path, *site.lines)
                if site.path.startswith("tests/") and site.when == "def":
                    users |= self._by_file.get(site.path, set())
        return users

    def _state_floor(self, path: str, r: classify.Region, direct: dict[str, bool],
                     wide: set[int]) -> set[int]:
        """A subset of what `_by_state` would select, from the directly edited names alone
        (every value the diff adds only adds readers). Raises Fallback where `_by_state`
        would. Lets the selector skip the subprocess when even this costs as much as wide."""
        index = self._index()
        users: set[int] | None = None
        chain: tuple[str, ...] = ()
        if r.kind == "class":
            chain = r.chain
            users = self._holders(index.check_class(path, chain), index)
        got: set[int] = set()
        for name in direct:
            for site in index.sites(name, path, chain, True):
                for bucket, tests in self._site_reads(index, site, name):
                    if bucket == "tests" or users is None:
                        got |= tests
                    elif bucket == "loose":
                        got |= tests & wide
                    else:
                        got |= tests & users & wide
        return got

    def _cost(self, numbers: set[int]) -> float:
        """Scorer cost of running `numbers` alone (best packing), in seconds."""
        ms = {n: pack.duration_ms(self._secs.get(n, 0.0)) for n in numbers}
        return pack.cost_ms(pack.pack(set(numbers), ms), ms) / 1000

    def _seconds(self, numbers: set[int]) -> float:
        return sum(self._secs.get(n, 0.0) for n in numbers)

    def _index(self) -> nameindex.NameIndex:
        """The prebuilt name index (image build) if it describes this source; else build it
        in memory (same answers, ~2 s more CPU)."""
        if self._name_index is None:
            path = Path(os.environ.get("BR_INDEX", nameindex.DEFAULT_PATH))
            self._name_index = nameindex.load(path, self.sqlglot) or nameindex.NameIndex(
                nameindex.build(self._all_sources(), []))
        return self._name_index
    def _name_readers(self, path: str, old_tree: ast.Module, names: set[str]) -> set[int]:
        """Functions of this module that read a changed module-level name changed with it."""
        got: set[int] = set()
        if not names:
            return got
        for s in self._old_scopes(path).scopes:
            if s.kind != "def":
                continue
            body = s.node.body
            if any(isinstance(n, ast.Name) and n.id in names for st in body for n in ast.walk(st)):
                got |= self.maps.span(path, s.body_start, s.end)
                got |= self._import_time(path, s.body_start, s.end)
        return got

    def _import_time(self, path: str, first: int, last: int) -> set[int]:
        """Tests that depend on lines path:first..last having run while modules were imported."""
        modules = self.maps.import_time.modules(path, first, last)
        if ImportTimeMap.ROOT in modules:
            raise AllTests(f"{path}:{first}-{last} runs during `import sqlglot`")
        got: set[int] = set()
        for m in modules:
            got |= self._module_dependents(m)
        return got

    def _import_dependents(self, path: str) -> set[int]:
        """Tests that fail if module `path` cannot be imported: dependents of every module
        whose import (per the import-time map) runs the module's top-level statements."""
        source = self._source(path)
        if source is None:
            raise KeyError(path)
        got: set[int] = set()
        for stmt in ast.parse(source).body:
            modules = self.maps.import_time.modules(path, stmt.lineno, stmt.lineno)
            if any(m == "tests" or m.startswith("tests.") for m in modules):
                # A test module fails to import: a collection error, which by default
                # interrupts the whole pytest session.
                raise AllTests(f"{path} would fail to import while tests are collected")
            got |= self._import_time(path, stmt.lineno, stmt.lineno)
        return got

    def _module_dependents(self, module: str) -> set[int]:
        """Tests that need `module` to import: its test file, or its users and runners."""
        hit = self._dependents.get(module)
        if hit is None:
            rel = module.replace(".", "/")
            path = rel + ".py" if (self.sqlglot / (rel + ".py")).is_file() else rel + "/__init__.py"
            if path.startswith("tests/"):
                hit = set(self._by_file.get(path, set())) | self.maps.test_imports.get(path)
            else:
                hit = set(self.maps.whole_file(path)) | self.maps.module_users(
                    module, self._top_level_classes(path))
            self._dependents[module] = hit
        return hit

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
        defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        kept = ({s.name for s in old_tree.body if isinstance(s, defs)}
                & {s.name for s in new_tree.body if isinstance(s, defs)})
        names: set[str] = set()
        for stmt in changed:
            if isinstance(stmt, defs):
                if stmt.name not in kept:  # deleted, renamed or new: importers of the name break
                    names.add(stmt.name)
                continue  # a body change is handled as a function / class region
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                raise AllTests(f"{path}: module-level {type(stmt).__name__} changed")
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for t in targets:
                names |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        names = _derived_names(names, old_tree, new_tree)
        # A name the module no longer defines makes `from <module> import name` fail, so every
        # module importing it fails to import, and so does everything that imports those.
        removed = _top_level_names(old_tree) - _top_level_names(new_tree)
        breaks = re.compile(r"\b(" + "|".join(map(re.escape, sorted(removed))) + r")\b") if removed else None
        got = self._name_readers(path, old_tree, names)
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
                if breaks is not None and breaks.search(self._all_sources()[other]):
                    got |= self._import_dependents(other)
                users = self.maps.whole_file(other) | self.maps.module_users(
                    _module_of(other), self._top_level_classes(other))
                if not users:
                    raise AllTests(f"{path}: names imported by {other}, which has no known users")
                got |= users
                todo.append(other)
        return got

    def _importers(self, path: str, names: set[str]) -> list[str]:
        """Files that import module `path` and mention one of `names`."""
        module = _module_of(path)
        parent, _, leaf = module.rpartition(".")
        # `module`, `from parent import … leaf`, `from .leaf import`; each one only searched
        # for in texts that contain its literal part (a regex scan of every file is slow)
        refers = [(module, re.compile(rf"\b{re.escape(module)}\b")),
                  (parent, re.compile(rf"from\s+{re.escape(parent)}\s+import\b[^\n]*\b{leaf}\b")),
                  (f".{leaf}", re.compile(rf"from\s+\.+{leaf}\s+import"))]
        mentions = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names))) + r")\b") if names else None
        out = []
        for other, text in self._all_sources().items():
            if other == path or not any(part in text and pattern.search(text)
                                        for part, pattern in refers):
                continue
            if f"{module} import *" in text or f".{leaf} import *" in text:
                raise AllTests(f"{path}: star-imported by {other}")
            if mentions is not None and mentions.search(text):
                out.append(other)
        return out

    # ── helpers ───────────────────────────────────────────────────────────────

    def _source(self, path: str) -> str | None:
        """`self._all_sources().get(path)`, reading only that file."""
        if self._sources is not None:
            return self._sources.get(path)
        if path not in self._some_sources:
            file = self.sqlglot / path
            self._some_sources[path] = (file.read_text(errors="replace")
                                        if path.startswith(("sqlglot/", "tests/"))
                                        and path.endswith(".py") and file.is_file() else None)
        return self._some_sources[path]

    def _top_level_classes(self, path: str) -> list[str]:
        """Top-level classes of a pinned source file (none if it is not one)."""
        if path not in self._classes_of:
            source = self._source(path)
            self._classes_of[path] = (classify.top_level_classes(ast.parse(source))
                                      if source else [])
        return self._classes_of[path]

    def _old_tree(self, path: str, old_bytes: bytes) -> ast.Module:
        """The pinned file's syntax tree, docstrings stripped (parsed once per run: nothing
        changes the trees after `strip_docstrings`)."""
        if path not in self._old_trees:
            tree = classify.strip_docstrings(ast.parse(old_bytes))
            lines = None if b"\r" in old_bytes else old_bytes.decode("utf-8").split("\n")
            self._old_trees[path] = tree, lines
        return self._old_trees[path][0]

    def _old_scopes(self, path: str) -> classify.Scopes:
        """`Scopes` of a file `_old_tree` parsed (built once per run)."""
        if path not in self._scopes:
            self._scopes[path] = classify.Scopes(*self._old_trees[path])
        return self._scopes[path]

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


def _check_compiles(path: str, old_tree: ast.Module, new_tree: ast.Module,
                    same: classify.SameAst) -> None:
    """Raise what `compile(new_tree, path, "exec")` raises, without compiling again the
    top-level statements whose text is the old file's (the old file compiles).

    A compile error stays inside one top-level statement but for two module-wide rules:
    `from __future__` imports must lead the file (and change how the rest compiles), and
    module-level `global` / `nonlocal`. So when the leading docstring and future imports
    are the old ones, no other future import exists, and the module scope has no global /
    nonlocal, only the leading statements and the new top-level statements are compiled.
    Otherwise (or without both texts, see SameAst) the whole module is."""
    lead = _leading_futures(new_tree.body)
    if same.old is not None and _same_lead(old_tree, new_tree, lead, same):
        old = {classify.text_key(s, same.old) for s in old_tree.body}
        new = [s for s in new_tree.body[lead:] if classify.text_key(s, same.new) not in old]
        new_tree = ast.Module(body=new_tree.body[:lead] + new, type_ignores=new_tree.type_ignores)
    compile(new_tree, path, "exec")


def _same_lead(old_tree: ast.Module, new_tree: ast.Module, lead: int,
               same: classify.SameAst) -> bool:
    """The first `lead` statements (docstring, future imports) have the old text, the old
    file has no more future imports, and the new module scope has no other future import
    and no global / nonlocal."""
    scope = list(_module_scope(new_tree.body))
    return (sum(map(_is_future, scope)) == lead - _has_docstring(new_tree.body)
            and not any(isinstance(s, (ast.Global, ast.Nonlocal)) for s in scope)
            and len(old_tree.body) >= lead
            and not (len(old_tree.body) > lead and _is_future(old_tree.body[lead]))
            and [classify.text_key(s, same.old) for s in old_tree.body[:lead]]
            == [classify.text_key(s, same.new) for s in new_tree.body[:lead]])


def _module_scope(body: list[ast.stmt]):
    """Statements that run in the module's own scope (not inside a def or class body)."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for name in ("body", "orelse", "finalbody"):
            yield from _module_scope(getattr(stmt, name, None) or [])
        for part in getattr(stmt, "handlers", None) or getattr(stmt, "cases", None) or []:
            yield from _module_scope(part.body)


def _is_future(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__"


def _has_docstring(body: list[ast.stmt]) -> bool:
    return bool(body) and classify._is_docstring(body[0])


def _leading_futures(body: list[ast.stmt]) -> int:
    """Length of the leading docstring + `from __future__` imports."""
    n = int(_has_docstring(body))
    while n < len(body) and _is_future(body[n]):
        n += 1
    return n


def _module_of(path: str) -> str:
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def name_of(path: str) -> str:
    return PurePosixPath(path).name


def _bindings(tree: ast.Module) -> set[str]:
    """Names bound at module level and in class bodies (nested classes included), as
    dotted paths: `func`, `Class`, `Class.attr`, `Class.method`. Function bodies are skipped."""
    out: set[str] = set()

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add(prefix + node.name)
            elif isinstance(node, ast.ClassDef):
                out.add(prefix + node.name)
                walk(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            out.add(prefix + n.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    out.add(prefix + (a.asname or a.name).split(".")[0])
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(node, field, []) or [], prefix)
                for h in getattr(node, "handlers", []) or []:
                    walk(h.body, prefix)

    walk(tree.body, "")
    return out
def _derived_names(names: set[str], *trees: ast.Module) -> set[str]:
    """Close `names` over module-level assignments: `B = f(A)` changes when A does."""
    names = set(names)
    grew = True
    while grew:
        grew = False
        for tree in trees:
            for stmt in tree.body:
                if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)) or stmt.value is None:
                    continue
                if not any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(stmt.value)):
                    continue
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name) and n.id not in names:
                            names.add(n.id)
                            grew = True
    return names


def _top_level_names(tree: ast.Module) -> set[str]:
    """Names a module defines at top level (defs, classes, assignment targets)."""
    out: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(stmt.name)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            out |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    return out
