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
    … writes a class-level table at run  + tests that ran a line naming that table (the
      time (`self.TABLE[k] = …`, `.pop`)   change can outlive the call: `_class_state_writes`)
    … run while modules are imported     + ALL (`import sqlglot`) / the modules' dependents,
                                         unless the import-state diff shows the patched
                                         sqlglot/ imports build the same state: then nothing
  sqlglot/ class body / module level,    tests that ran a line reading a changed name, ∩ the
    assignments with inert new values    conservative set below; ∪ test-file reads; a changed
                                         lambda body → tests that called it (selector/names.py)
    … else, values consumed at class     import old and patched package, diff all class /
      creation / import (metaclass,      module values (selector/state.py); per changed value:
      __init_subclass__, import calls)   tests that ran a run-time reader, among the tests that
                                         can hold the owner class (`_by_state`)
    … body mutates objects / now        import-state diff first: a change outside the patched
      aliases one (`X = Base.X`)         modules → ALL (`_foreign_state`)
  sqlglot/ class body (otherwise)        users of the class chain ∪ tests that ran the file
  sqlglot/ module level (otherwise)      users of the module's classes ∪ tests that ran the file
                                         (ALL if the changed names are used in other modules)
  sqlglot/ removed / renamed def or      tests that ran the old body ∪ `from … import name` in
    assignment (module or class level)   defs ∪ readers of every changed import-time value
                                         (`_removed_bindings`); test code reading / importing
                                         it at collection, removed classes / imports, cached
                                         readers → import bound / ALL
  base class / module body (BASE_FILES)  the name / state narrowing above, not bounded by
                                         the conservative set (~ALL); if neither applies → ALL
  tests/ test function                   that test (and same-named inherited copies)
  tests/ other function (helper, setUp,  tests that ran its body (lines map), unless the def
    Validator method, nested def)        line changed / no test ran it / it runs at import
  tests/ module-level assignment         readers of the changed names (as for sqlglot/)
  tests/ other test-file code            the file's tests ∪ importers (test_imports map)
  tests/ helper module (otherwise)       importers (test_imports map)
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

test_generator_memory (6.7 s), selected through any route but ALL: its exact body runs on
the patched package in a fresh interpreter for the dialects the patch can reach; it is
dropped only if that passes with margin (selector/genmem.py).
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path, PurePosixPath

from . import classify, diff, nameindex, names, pack
from . import maps as maps_module
from .maps import ImportTimeMap, Maps

# Class and module bodies of these files shape nearly every test (map gives ~1550 of 1620):
# no conservative set for them, only the name / state narrowing, else ALL.
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
# Selection cost ≥ this × ALL cost → just run ALL. r5-tune: 0.85 fired on 32.8 s selections
# (0.89 × ALL, 4 s dearer, never needed on any set); 0.95 keeps the free hedge near equal cost.
ALL_FRACTION = 0.95
SLOW_SECONDS = 5.0  # tests at least this slow get the statement-level rule in function bodies
# An import-state diff costs ~0.8 s of container CPU (r3-cpu). Run it only when the wide set
# costs at least this much more than the cheapest selection it could give. r5-tune: 1.2 adds
# diffs that save ~1.8 s each; ≤1.0 adds diffs that save ~0.5 s (< their CPU).
STATE_MIN_GAIN = 1.2
EXPR_ROOT = "sqlglot.expressions.core:Expr"
# Site.use values: a read that only uses part of the value. The floor (`_state_floor`) leaves
# such reads out, since the diff may show that part unchanged.
PARTIAL_USES = frozenset({"truth", "keys"})
# Names whose value Python itself consumes while building a class / module (not stored as a
# plain attribute the snapshot compares like any other).
DEFINITION_TIME_NAMES = frozenset({"__slots__", "__init_subclass__", "__class_getitem__",
                                   "__set_name__", "__all__", "__getattr__", "__path__"})
# What the TypedDict metaclass stores on the class: data, never looked up implicitly by
# Python except by the same metaclass building a subclass.
TYPEDDICT_KEYS = frozenset({"__total__", "__required_keys__", "__optional_keys__",
                            "__readonly_keys__", "__mutable_keys__"})
MAX_HOLDER_ENTRIES = 200  # more entries hold import-time objects of a class: do not restrict
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


class GlobalEffect(AllTests):
    """The edit changes import-time state owned by modules it does not touch (a class body
    that aliases another class's dict and mutates it): every test that runs after the module
    is imported, in the same process, can see it. ALL, never the import bound."""


class Selector:
    def __init__(self, tests: list[dict], sqlglot: Path, maps: Path) -> None:
        self.tests = tests
        self.sqlglot = sqlglot
        compiled = Path(os.environ.get("BR_MAPS_COMPILED", maps_module.COMPILED))
        self.maps = Maps(maps, tests, compiled)
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
        self._state_error: str | None = None  # file that failed the patched import, if known
        self._state_unread: list[str] = []  # `_state_readers`: changed keys with no seen reader
        self._direct: dict[str, bool] | None = None  # names `_by_names` saw edited
        self._inert: names.BodyChange | None = None  # … and the whole change it accepted
        self.notes: list[str] = []  # why a definition-time region was (not) narrowed
        self._dependents: dict[str, set[int]] = {}
        self._jobs: list = []
        self._mixed_py = False
        self._chains: dict[str, list] = {}
        self._defaults: dict[str, list] = {}
        self._no_default_readers: bool | None = None
        self._families: dict[tuple, list] = {}  # `_related`: owner class → its family
        self._ancestor_memo: dict[tuple, set | None] = {}
        self._test_imports = None  # testimport.Checker, on first use
        self._all_test_modules: set[str] | None = None
        self.diffexec = None
        try:
            from .diffexec import DiffExec
            self.diffexec = DiffExec.create(self)
        except Exception:  # noqa: BLE001  (diffexec is an optional pruner)
            self.diffexec = None
        self.subtest = None
        try:
            from .subtest import SubTest
            self.subtest = SubTest.create(self)
        except Exception:  # noqa: BLE001  (optional pruner)
            self.subtest = None
        self.slowverify = None
        try:
            from .slowverify import SlowVerify
            self.slowverify = SlowVerify.create(self)
        except Exception:  # noqa: BLE001  (optional pruner)
            self.slowverify = None
        self.genmem = None
        try:
            from .genmem import GenMem
            self.genmem = GenMem.create(self)
        except Exception:  # noqa: BLE001  (optional pruner)
            self.genmem = None
        self.runverify = None
        try:
            from .runverify import RunVerify
            self.runverify = RunVerify.create(self)
        except Exception:  # noqa: BLE001  (optional pruner)
            self.runverify = None

    def all_numbers(self) -> set[int]:
        return set(self._all)

    # ── entry point ────────────────────────────────────────────────────────────

    def select(self, patch_text: str) -> tuple[set[int], str]:
        """Return (test numbers, human-readable reason)."""
        files = diff.parse(patch_text)
        if not files:
            return self.all_numbers(), "ALL (no files parsed)"
        self._files, self._state, self._state_error = files, False, None
        chosen: set[int] = set()
        reasons = []
        self._jobs = []  # diffexec: (path, new text, prunable tests) per file
        self._mixed_py = False  # a .py behaviour change diffexec cannot replay
        self._chains = {}  # diffexec: changed function chains per file
        self._defaults = {}  # diffexec: default-only files → [(name, first line, {param: (old, new)})]
        self._spans = {}  # subtest: OLD-file spans of the touched statements per file
        per_file: list[tuple[str, set[int]]] = []
        try:
            for fp in files:
                got, why = self._select_file(fp)
                per_file.append((fp.path, got))
                reasons.append(f"{fp.path}: {why} [{len(got)}]")
        except AllTests as exc:
            return self.all_numbers(), f"ALL ({exc})"
        if self._jobs and self.diffexec is not None:
            dropped, note = self.diffexec.prune(self._jobs, self._benefit(per_file),
                                                lambda only: self._benefit(per_file, only))
            if note:
                reasons.append(note)
            per_file = [(p, got - dropped.get(p, set())) for p, got in per_file]
        if self._jobs and not self._mixed_py and not self._defaults:
            for pruner in (self.subtest, self.slowverify):
                if pruner is not None:
                    per_file = self._subtest(per_file, reasons, pruner)
        if self.genmem is not None:
            per_file = self._genmem(per_file, reasons)
        for _, got in per_file:
            chosen |= got
        if chosen:
            if self._untraced is None:
                self._untraced = self.maps.untraced()
            chosen |= self._untraced
        chosen &= self._all
        if chosen and self.runverify is not None:
            # Last: run what is left, if cheap, on the patched code; drop what passes.
            try:
                chosen, note = self.runverify.prune(self._files, chosen)
            except Exception as e:  # noqa: BLE001
                note = f"runverify: keep ({type(e).__name__})"
            if note:
                reasons.append(note)
        return chosen, "; ".join(reasons)

    def _subtest(self, per_file, reasons, pruner):
        """Drop slow tests a run-time sub-test verification shows to pass: the TPC executor
        tests whose touched queries re-run with the pinned outputs (subtest.py), test_max_nodes
        whose expensive phases run no touched statement (slowverify.py)."""
        from . import pack
        prunable = {p: nums for p, _, nums in self._jobs}
        cands = set(pruner.numbers)
        for p, got in per_file:
            cands -= got - prunable.get(p, set())
        cands &= set().union(*(got for _, got in per_file))
        if not cands:
            return per_file
        ms_of = {n: pack.duration_ms(s) for n, s in self._secs.items()}
        everything = set().union(*(got for _, got in per_file))
        base = pack.cost_ms(pack.pack(everything, ms_of), ms_of)

        def benefit(only):
            rest = everything - only
            return base - (pack.cost_ms(pack.pack(rest, ms_of), ms_of) if rest else 0)

        dropped, note = pruner.prune(self._jobs, self._spans, cands, benefit)
        if note:
            reasons.append(note)
        return [(p, got - dropped) for p, got in per_file]

    def _genmem(self, per_file, reasons):
        """Drop test_generator_memory when its body passes on the patched package
        (selector/genmem.py)."""
        from . import pack
        n = self.genmem.number
        everything = set().union(*(got for _, got in per_file))
        if n not in everything:
            return per_file
        ms_of = {k: pack.duration_ms(s) for k, s in self._secs.items()}
        base = pack.cost_ms(pack.pack(everything, ms_of), ms_of)

        def benefit(only):
            rest = everything - only
            return base - (pack.cost_ms(pack.pack(rest, ms_of), ms_of) if rest else 0)

        try:
            drop, note = self.genmem.prune(self._files, per_file, benefit)
        except Exception as e:  # noqa: BLE001
            drop, note = False, f"genmem: keep ({type(e).__name__})"
        if note:
            reasons.append(note)
        return [(p, got - {n}) for p, got in per_file] if drop else per_file

    def _benefit(self, per_file, only: set[int] | None = None) -> int:
        """Cost saved (ms) if diffexec dropped every prunable test (or every prunable test in
        `only`): 0 → do not run it."""
        from . import pack
        ms_of = self.diffexec.ms_of
        everything = set().union(*(got for _, got in per_file))
        prunable = {p: (nums if only is None else nums & only) for p, _, nums in self._jobs}
        rest = set().union(*(got - prunable.get(p, set()) for p, got in per_file))
        return (pack.cost_ms(pack.pack(everything, ms_of), ms_of)
                - pack.cost_ms(pack.pack(rest, ms_of), ms_of))

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
            if isinstance(exc, GlobalEffect) or str(exc).startswith(f"{path}: hunk"):
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
        gone: set[int] | None = None
        if lost:
            # A removed/renamed module- or class-level name breaks its importers and callers
            # (ImportError at collection, AttributeError at call): `_removed_bindings`.
            gone, gone_why = self._removed_bindings(path, old_tree, new_tree, lost)
        removed, added = diff.changed_lines(fp)
        found = classify.regions(old_tree, new_tree, removed, added, same,
                                 self._old_scopes(path))
        if gone is not None:
            # Class / module bodies: every value they now build differently is in the
            # import-state diff `_removed_bindings` took the readers of. The removed defs:
            # their callers ran their bodies (taken there too). Other defs: as usual.
            self._mixed_py = True  # diffexec would replay a renamed, unchanged body
            lost_defs = {tuple(q.split(".")) for q in lost}
            found = [r for r in found
                     if (r.kind == "function"
                         and not any(r.chain[:len(d)] == d for d in lost_defs))
                     or (r.kind == "class" and r.via_def)]
            if not found:
                return gone, gone_why
            got, why = self._library(path, old_tree, new_tree, found, "")
            return got | gone, f"{gone_why}; {why}"
        if not found:
            return set(), "no behaviour-changing region"
        if path.startswith("tests/"):
            self._mixed_py = True
            return self._test_code(path, found, old_tree, new_tree)
        if path.startswith("sqlglot/"):
            return self._library(path, old_tree, new_tree, found, new_text)
        raise AllTests(f"{path}: .py outside sqlglot/ and tests/")

    def _removed_bindings(self, path: str, old_tree: ast.Module, new_tree: ast.Module,
                          lost: set[str]) -> tuple[set[int], str]:
        """Module- or class-level defs / assignments the patch removes or renames (idea I26).

        Who can notice that `name` is gone:
          * code that reads it (`x.name`, bare `name`, "name", getattr / dir with a computed
            name that can be it): the name sites (names.py) – run-time lines the map knows,
            or import-time code, whose products are in the import-state diff;
          * callers of a removed def: they ran its body (lines map over the old body; also
            covers references the name scan cannot see, e.g. a stored callable);
          * importers: `from module import name` inside a def → tests that ran that line; at
            module level in sqlglot/ → the patched package fails to import (the state diff
            is then unavailable → fallback); in a test file → collection error → ALL;
          * test code reading it while test modules are collected → ALL;
          * everything the patched package builds differently at import (class-creation
            consumers, tables holding the old function, the added name of a rename): the
            readers of each changed value in the import-state diff (`_state_readers`).
        Raises AllTests (→ import bound) for removed classes and imports (registries and
        re-exports the name scan cannot follow), special names, names the state snapshot
        does not see, reads in code cached across tests, an unavailable diff, and other
        edits of the file's class / module bodies the diff cannot see (a class head: the
        snapshot does not compare bases; a statement other than def / class / assignment:
        its effects may lie outside sqlglot's state)."""
        why = f"{path}: binding removed ({', '.join(sorted(lost)[:3])})"
        if not path.startswith("sqlglot/"):
            raise AllTests(why)
        unseen = _unseen_body_change(old_tree, new_tree)
        if unseen:
            raise AllTests(f"{why}: {unseen}")
        kinds = _binding_kinds(old_tree)
        module = _module_of(path)
        index = self._index()
        got: set[int] = set(self.maps.reads.get(path))
        scopes = self._old_scopes(path)
        callers: set[int] = set()  # tests that ran a removed def's body
        for q in sorted(lost):
            *chain, name = q.split(".")
            kind = kinds.get(q)
            if kind not in ("def", "assign"):
                raise AllTests(f"{why}: {q} is bound by {kind}")
            if name.startswith("__") and name.endswith("__"):
                raise AllTests(f"{why}: {q} is a special name")
            try:
                sites = index.sites(name, path, tuple(chain), True)
            except names.Fallback as exc:
                raise AllTests(f"{why}: {exc}") from None
            if not chain:
                got |= self._from_imports(name, why, module)
            for site in sites:
                if site.path.startswith("tests/") and site.when == "def" and not site.dynamic:
                    raise AllTests(f"{why}: {site.path}:{site.lines[0]} reads {name} at collection")
                if site.when == "def" and site.memo:  # (as `_site_reads`, before the diff)
                    raise AllTests(f"{why}: {name} read in code cached across tests ({site.what})")
            if kind == "def":
                s = scopes.by_chain.get(tuple(q.split(".")))
                if s is None or s.kind != "def":
                    raise AllTests(f"{why}: {q} not found")
                callers |= self.maps.span(path, s.body_start, s.end)
        found = self._state_diff()
        if found is None:
            raise AllTests(f"{why}: import-state diff unavailable")
        changed = set(found["changed"])
        for q in lost:
            *chain, name = q.split(".")
            key = f"C {module}:{'.'.join(chain)} {name}" if chain else f"M {module} {name}"
            if key not in changed:
                raise AllTests(f"{why}: {q} not in the import state")
        try:
            # no reader ran: fine if the removed defs' callers are known (as for a value
            # change, an unseen value with no seen reader is a map blind spot → fallback)
            readers, how = self._state_readers(found, None, allow_empty=bool(callers))
        except names.Fallback as exc:
            raise AllTests(f"{why}: {exc}") from None
        return got | callers | readers, f"removed {', '.join(sorted(lost)[:3])} → callers, {how}"

    def _from_imports(self, name: str, why: str, module: str) -> set[int]:
        """`from … import name` statements: inside a def → the tests that ran that line;
        at module level in a test file → AllTests (collection error); at module level in an
        unpatched sqlglot/ file, from this module → AllTests (the patched package does not
        import: the state diff would be unavailable); other module-level imports → the diff
        tells (it fails if one does import the removed name)."""
        got: set[int] = set()
        patched = {fp.path for fp in self._files}
        for other in sorted({p for p, _ in self._index().data["imports"].get(name, ())}):
            source = self._source(other)
            if source is None:
                continue
            for node, in_def in _import_froms(ast.parse(source), name):
                if in_def:
                    got |= self.maps.span(other, node.lineno, node.end_lineno or node.lineno)
                elif other.startswith("tests/"):
                    raise AllTests(f"{why}: {other} imports {name} at collection")
                elif other not in patched and _from_module(other, node) == module:
                    raise AllTests(f"{why}: {other} imports {name}")
        return got

    # ── tests/*.py ────────────────────────────────────────────────────────────

    def _test_code(self, path: str, found: list[classify.Region], old_tree: ast.Module,
                   new_tree: ast.Module) -> tuple[set[int], str]:
        if name_of(path) == "__init__.py":
            raise AllTests(f"{path}: test package init")
        importers = set(self.maps.test_imports.get(path))
        own = self._by_file.get(path, set())
        if not name_of(path).startswith("test_"):  # a helper module
            if not importers:
                raise AllTests(f"{path}: helper with no known importers")
            ran = [self._test_helper_function(path, r)
                   if r.kind == "function" else self._test_names(path, old_tree, new_tree, r)
                   for r in found]
            if all(got is not None for got in ran):
                return set().union(*ran), "test helper functions / names → tests that ran them"
            return importers | own, "test helper → importing test files"
        chosen: set[int] = set()
        whole_file = False
        helpers = False
        for r in found:
            test_name = r.chain[-1] if r.kind == "function" and r.chain else ""
            if r.kind == "function" and test_name.startswith("test") and len(r.chain) <= 2:
                suffix = "::" + "::".join(r.chain)
                chosen |= {n for n in own if self._id_of[n].endswith(suffix)}
                method = "::" + test_name  # copies inherited by test classes in importers
                chosen |= {n for n in importers if self._id_of[n].endswith(method)}
            elif (got := (self._test_helper_function(path, r) if r.kind == "function"
                          else self._test_names(path, old_tree, new_tree, r))) is not None:
                chosen |= got
                helpers = True
            else:
                whole_file = True
        if whole_file:
            return own | importers | chosen, "test-file setup / helper code → whole file"
        return chosen, "test functions" + (" + helper functions / names → tests that ran them"
                                           if helpers else "")

    def _test_helper_function(self, path: str, r: classify.Region) -> set[int] | None:
        """A function in test code that is not a test (helper, `Validator.validate_all`,
        `setUp`, `setUpClass`, a nested def): the tests that ran its body (lines map). Each
        test was traced alone from the start of its setup, so `setUpClass` work is credited
        to every test of the class. None (→ whole file / importers) when the map cannot see
        every use: the def / decorator line changed, no test ran the body, or the body runs
        while some module is imported (a module-level or class-body call: collection)."""
        if r.kind != "function" or r.signature:
            return None
        first, last = r.span
        if self.maps.import_time.modules(path, first + r.is_lambda, last):
            return None
        return self.maps.span(path, first, last) or None

    def _test_names(self, path: str, old_tree: ast.Module, new_tree: ast.Module,
                    r: classify.Region) -> set[int] | None:
        """A module-level assignment in test code (`TPCDS_SCHEMA = {...}` in tests/helpers.py):
        the tests that ran a line reading a changed name, as for sqlglot/ (`_by_names`, no
        conservative bound to intersect with: every reader is a traced line). Class bodies
        (unittest / pytest read their attributes) and anything `_by_names` cannot prove
        → None (whole file / importers)."""
        if r.kind != "module" or r.via_def:
            return None
        try:
            got, _ = self._by_names(path, old_tree, new_tree, r, None)
        except names.Fallback:
            return None
        return got or None

    # ── sqlglot/*.py ──────────────────────────────────────────────────────────

    def _library(self, path: str, old_tree: ast.Module, new_tree: ast.Module,
                 found: list[classify.Region], new_text: str = "") -> tuple[set[int], str]:
        module = _module_of(path)
        chosen: set[int] = set(self.maps.reads.get(path))
        fixed = set(chosen)  # tests diffexec may never drop (readers, definition-time users ...)
        prunable: set[int] = set()
        kinds = []
        # Every change in the file is a literal default value of a def (`depth: int = 0` →
        # `= 1`): diffexec replays the old code with the new default where a recorded call
        # may have used it (diffexec/replay.py "defaults").
        defaults = (_default_changes(old_tree, new_tree)
                    if new_text and any(r.kind == "function" and r.signature for r in found)
                    and self._defaults_unread() else None)
        for r in found:
            if r.kind == "function":
                self._spans.setdefault(path, []).extend(
                    r.statements if r.statements is not None else [r.span])
                ran = self.maps.span(path, *r.span)
                got = self._slow_tests_off_statements(path, ran, r)
                if got and r.signature and defaults:
                    prunable |= got
                    self._chains.setdefault(path, []).append(r.chain)
                    self._defaults[path] = defaults
                elif got and not r.signature:
                    prunable |= got
                    self._chains.setdefault(path, []).append(r.chain)
                else:
                    self._mixed_py = True
                if r.signature and path in self._defaults:
                    # literal defaults only: calls are what can observe them (prunable);
                    # the class users stay as for any signature edit
                    users = self._class_users(module, r.classes) - got
                    got |= users
                    fixed |= users
                elif r.signature:
                    got |= self._class_users(module, r.classes)
                    fixed |= got
                defs = _edited_defs(old_tree, new_tree, r)
                shared = _class_state_writes(defs)
                if got and any(classify._writes_shared_state(f) for f in defs):
                    # state that outlives the call (module / class tables, caches): replaying
                    # the recorded calls (diffexec) compares only arguments and results
                    fixed |= got
                    self._mixed_py = True
                if shared and got:
                    # The def mutates a class-level table at run time
                    # (`self.NO_PAREN_FUNCTION_PARSERS["PRIOR"] = …` … `.pop("PRIOR")`): an edit
                    # can leave it changed for every later test in the process, which replaying
                    # one call (diffexec) cannot see either.
                    got |= self._mention_runners(shared)
                    fixed |= got
                    self._mixed_py = True
                    kinds.append(f"(writes class state {', '.join(sorted(shared))})")
                # Called while some module is imported (trie builders, decorator factories,
                # `X = build()` tables, module __getattr__): every importer depends on it,
                # which no per-test coverage can show.
                # A lambda's first line is where it is created (class-body dicts run it at
                # import); only its later lines are body that can run at import.
                first, last = r.span
                # slow callers a run-time pruner (diffexec / subtest / slowverify) may drop –
                # unless the import-time runs leave this commit `_mixed_py`: count them in what
                # the import-state diff can save
                slack = (got & self._slow & (prunable - fixed)
                         if not self._mixed_py and self.diffexec is not None else set())
                # its own state entry (a def with a closure, e.g. `super()`'s `__class__`
                # cell, is digested with its code): the edit itself, seen by its callers
                # (a plain def only: a decorated one's entry is what the decorator returned)
                plain = bool(defs) and all(
                    isinstance(d, ast.Name) and d.id in ("staticmethod", "classmethod")
                    for f in defs for d in f.decorator_list)
                own = (None if r.signature or r.is_lambda or not r.chain or not plain
                       else f"M {module} {r.chain[0]}" if len(r.chain) == 1
                       else f"C {module}:{'.'.join(r.chain[:-1])} {r.chain[-1]}")
                at_import, same_state = self._function_at_import(path, first + r.is_lambda, last,
                                                                 chosen | got, slack, own)
                if not got and same_state in ("readers", "readers-unseen"):
                    # Never ran under a test; what its runs at import built differently is
                    # read only by the tests in at_import.
                    kinds.append(f"unrun def {'.'.join(r.chain) or '<lambda>'}, runs at import")
                elif not got and same_state:
                    # Never ran under a test, and its runs at import built the same state.
                    kinds.append(f"unrun def {'.'.join(r.chain) or '<lambda>'}, runs at import:"
                                 " import state unchanged")
                elif not got:
                    # Never ran under a test: may run only at import or class creation.
                    # Treat it as a change to the scope that defines it.
                    got = self._definition_time(path, module, old_tree, new_tree, r)
                    fixed |= got
                    kinds.append(f"unrun def {'.'.join(r.chain) or '<lambda>'} → definition-time")
                else:
                    dropped = len(ran - got)
                    kinds.append(f"def {'.'.join(r.chain) or '<lambda>'}"
                                 + (f" (-{dropped} slow, off touched statements)" if dropped else ""))
                if at_import:
                    kinds.append(f"(runs at import: +{len(at_import - got)}"
                                 + (", readers of changed import state)" if same_state else ")"))
                    if same_state != "readers":  # (also "readers-unseen")
                        # the import-time runs may have built anything the run-time pruners
                        # cannot see
                        self._mixed_py = True
                    # "readers": every value the import-time runs built differently is in the
                    # import-state diff, and every test that can read one is in at_import
                    # (kept, never pruned); what the other tests can still see of the edit is
                    # its run-time calls, which the run-time pruners verify as usual
                    fixed |= at_import
                elif same_state is True and got:
                    kinds.append("(runs at import: import state unchanged)")
                got |= at_import
            else:
                self._mixed_py = True
                self.notes.clear()
                got = self._definition_time(path, module, old_tree, new_tree, r)
                note = f" [{'; '.join(self.notes)}]" if self.notes else ""
                kinds.append(f"{r.kind} {'.'.join(r.chain) or module}{note}")
                fixed |= got
            chosen |= got
        if prunable - fixed and new_text:
            # default-only file: the child compiles the old source and swaps defaults
            self._jobs.append((path, self._source(path) if path in self._defaults else new_text,
                               prunable - fixed))
        return chosen, ", ".join(kinds)

    def _defaults_unread(self) -> bool:
        """No code in sqlglot/ or tests/ reads a function's defaults (`__defaults__`,
        `inspect.signature(f).parameters[p].default`, getcallargs, …): then a call is the only
        way to observe one."""
        if self._no_default_readers is None:
            index = self._index()
            anywhere = ("__defaults__", "__kwdefaults__", "getcallargs", "kwonlydefaults",
                        "getargvalues", "bind_partial", "apply_defaults", "__signature__")
            attrs = ("default", "defaults")  # Parameter.default, FullArgSpec.defaults
            self._no_default_readers = index is not None and not any(
                index.mentions(n) for n in anywhere) and not any(
                o[1] == "attr" for n in attrs for o in index._occurrences(n))
        return self._no_default_readers

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

        Base files (BASE_FILES): nearly every test uses their classes, so the conservative
        set is ~ALL and is not used as a bound (wide = every test); the name / state
        narrowing runs unbounded, and if neither applies → ALL.
        """
        base = path in BASE_FILES
        if r.kind == "class" and r.via_def:
            got = self._class_hook(path, module, old_tree, new_tree, r, base)
            if got is not None:
                return got
        if base and (r.kind not in ("class", "module") or r.via_def):
            raise AllTests(f"{path}: base class / module body")
        if r.kind in ("class", "module") and _may_share(old_tree, new_tree, r):
            self._foreign_state(path)
        try:
            wide: set[int] | None = (self.all_numbers() if base else
                                     self._conservative(path, module, old_tree, new_tree, r))
            wide_error = None
        except AllTests as exc:
            wide, wide_error = None, exc
        if (wide_error is not None and str(wide_error).endswith("with no known users")
                and r.kind in ("class", "module") and not r.via_def):
            # No test is known to use the class / module (typing-only TypedDicts): the
            # import-state diff, unbounded as for base files, shows what the edit changes;
            # the readers of those values are all that can observe it.
            try:
                got, why = self._by_state(path, r, self.all_numbers())
                self.notes.append(f"{'.'.join(r.chain) or module}: no known users, {why}")
                return got
            except names.Fallback as exc:
                self.notes.append(f"{'.'.join(r.chain) or module}: no state narrowing ({exc})")
        if wide_error is not None:
            raise wide_error  # v1 could not bound it (star import, re-export, …): never narrow
        if r.kind in ("class", "module") and not r.via_def:
            self._direct = self._inert = None
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
                    inert, self._inert = self._inert, None
                    if wide is not None:
                        # A selection is never cheaper than this floor (2 s if unknown).
                        floor = (self._cost(self._state_floor(path, r, direct, wide))
                                 if direct else 0.0)
                        if self._cost(wide) - max(floor, 2.0) < STATE_MIN_GAIN:
                            raise names.Fallback(f"at most {self._cost(wide) - max(floor, 2.0):.1f}"
                                                 " s to win: not worth an import-state diff")
                    got, why = self._by_state(path, r, wide, inert)
                    if wide is not None and self._seconds(got) >= self._seconds(wide):
                        raise names.Fallback(f"{why}, but no cheaper")
                    self.notes.append(f"{'.'.join(r.chain) or module}: {why}")
                    return got
                except names.Fallback as exc2:
                    self.notes.append(f"{'.'.join(r.chain) or module}: no state narrowing "
                                      f"({exc2})")
        if base:
            raise AllTests(f"{path}: base class / module body [{'; '.join(self.notes)}]")
        if wide_error is not None:
            raise wide_error
        return wide

    def _class_hook(self, path: str, module: str, old_tree: ast.Module, new_tree: ast.Module,
                    r: classify.Region, base: bool) -> set[int] | None:
        """An edit inside a class-creation hook (`__init_subclass__`, a metaclass method,
        classify.DEFINITION_TIME_METHODS): the def runs while classes are created, at import
        (sqlglot modules, test modules) or at run time (a test that defines a subclass).
        Its import-time runs are seen through the import-state diff
        (`_import_state_users`: readers of every value it builds differently), its run-time
        runs through the lines map (tests that ran a line of the def), and, as for any def,
        a class-level table it mutates in place adds the tests that ran a line naming it.
        None: fall back to the class-body rules (base files: ALL)."""
        hook = classify.Region("function", r.via_def)
        defs = _edited_defs(old_tree, new_tree, hook)
        olds = _edited_defs(old_tree, ast.Module(body=[], type_ignores=[]), hook)
        if len(olds) != 1:
            return None
        old_def = olds[0]
        first = min([old_def.lineno] + [d.lineno for d in old_def.decorator_list])
        last = old_def.end_lineno or old_def.lineno
        wide: set[int] | None = None
        if not base:
            try:
                wide = self._conservative(path, module, old_tree, new_tree, r)
            except AllTests:
                wide = None
            if wide is not None and self._cost(wide) - 2.0 < STATE_MIN_GAIN:
                return None
        try:
            hook = f"C {module}:{'.'.join(r.via_def[:-1])} {r.via_def[-1]}"
            got, why = self._import_state_users(
                path, first, last, self.maps.import_time.modules(path, first, last), hook)
        except names.Fallback as exc:
            self.notes.append(f"{'.'.join(r.via_def)}: no state narrowing ({exc})")
            return None
        got |= self.maps.span(path, first, last)
        # a table the hook first binds to a fresh object of the new class, then fills, is
        # that class's own (its import-time contents are in the diff, run-time creators ran
        # the def); one inherited from a base would be shared with every other class
        shared = _class_state_writes(defs) - _fresh_tables(defs)
        if shared:
            got |= self._mention_runners(shared)
        if wide is not None and self._seconds(got) >= self._seconds(wide):
            return None
        self.notes.append(f"{'.'.join(r.via_def)}: class-creation hook, {why}")
        return got

    def _conservative(self, path: str, module: str, old_tree: ast.Module,
                      new_tree: ast.Module, r: classify.Region) -> set[int]:
        got = set(self.maps.whole_file(path))
        users = self._class_users(module, r.classes) if r.classes else set()
        if not users:
            if r.kind == "module" or not r.classes:
                got |= self._importer_users(path, old_tree, new_tree)
            elif r.kind != "function":
                # A class the classes map does not record (enums, namespaces): other modules
                # read it through the class object (`ErrorLevel.RAISE`, `DType.BPCHAR.value`)
                # without running a line of this file or making one.
                # Not for a method body (an unrun def): it changes only what a call computes,
                # and a call runs the def's own lines (coverage) or happens at import
                # (_function_at_import), whoever names the class. Matching by bare name would
                # also pull in every unrelated class of that name (executor `Table` → every
                # test naming exp.Table: ~ALL).
                got |= self._mention_runners({name for chain in r.classes for name in chain})
            users = self.maps.module_users(module, classify.top_level_classes(old_tree))
        got |= users
        if not got:
            raise AllTests(f"{path}: definition-time change with no known users")
        return got

    def _mention_runners(self, names: set[str]) -> set[int]:
        """Tests that ran a line naming one of these classes (attribute, bare name, string or
        import alias), plus every test of a test file that names one at import."""
        index = self._index()
        got: set[int] = set()
        for name in names:
            for site in index.mentions(name):
                if site.when == "run":
                    got |= self.maps.span(site.path, *site.lines)
                elif site.path.startswith("tests/"):
                    got |= self._by_file.get(site.path, set()) | set(
                        self.maps.test_imports.get(site.path))
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
        self._inert = change  # for `_by_state`: plain assignments of inert values
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
            # an in-place literal edit: reads of only its truth / keys may see no change
            kept = (change.same.get(name, frozenset())
                    if (owner_path, owner_chain) == (path, r.chain if r.kind == "class" else ())
                    else frozenset())
            for site in index.sites(name, owner_path, owner_chain, name in change.new_names):
                n_sites += 1
                if site.when == "run" and site.use and site.use in kept:
                    continue
                if (site.when == "run" and not site.dynamic and not site.path.startswith("tests/")
                        and self.maps.import_time.modules(site.path, *site.lines)):
                    # a def body that also runs at import (`sql_names()` building the
                    # function tables): what it builds there outlives the read
                    raise names.Fallback(f"{name} read at import ({site.path}:{site.lines[0]})")
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

    def _foreign_state(self, path: str) -> None:
        """Every bound below (users of the class, tests that ran or load the module) assumes
        the edit changes only state the patched modules own. A body that mutates objects, or
        that now aliases one, can break that: `SINGLE_TOKENS = Tokenizer.SINGLE_TOKENS` followed
        by `SINGLE_TOKENS.pop("%")` removes % from every tokenizer once the dialect is imported,
        lazily, by some earlier test in the same worker. Check it on the import-state diff:
        an entry that changed outside the patched modules, not a subclass of one of their
        classes and not a copy of one of their changed values (equal to it both before and
        after the edit: a re-export, `X = other.X.copy()`) → GlobalEffect (ALL). The patched
        package does not import: ALL if the failing file is not a patched one (the edit broke
        another module), else the module's own bound still holds."""
        found = self._state_diff()
        if found is None:
            broken = self._state_error
            if broken and broken not in {fp.path for fp in self._files}:
                # the edit makes another module fail to import (it copied a key we removed)
                raise GlobalEffect(f"{path}: breaks the import of {broken}")
            return
        if "digests" not in found:
            return
        mods = {_module_of(fp.path) for fp in self._files
                if fp.path.startswith("sqlglot/") and fp.path.endswith(".py")}
        digests, info = found["digests"], found["info"]
        ours = {tuple(digests[k]) for k in found["changed"]
                if k in digests and _in_modules(k.split(" ", 2)[1], mods)}
        for key in found["changed"]:
            kind, owner, attr = key.split(" ", 2)
            pair = tuple(digests.get(key) or (None, None))
            if _in_modules(owner, mods) or (None not in pair and pair in ours):
                continue
            if kind == "C" and any(m and _in_modules(m, mods)
                                   for m in (info.get(owner) or {}).get("mro", [])):
                continue  # a subclass of a patched class: its users are the class's users
            raise GlobalEffect(f"{path}: changes import-time state outside the patched modules "
                               f"({owner.rsplit(':', 1)[-1]}.{attr})")

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
            found = state.diff(self.sqlglot, snapshot, patched)
            if found is not None and "import_error" in found:
                self._state_error, found = found["import_error"], None
            self._state = found
        return self._state  # type: ignore[return-value]

    def _by_state(self, path: str, r: classify.Region, wide: set[int] | None,
                  inert: names.BodyChange | None = None) -> tuple[set[int], str]:
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
        if (not changed and inert is not None and not inert.lambda_spans and not inert.sets
                and not (inert.names.keys() & DEFINITION_TIME_NAMES)):
            # The edit only assigns inert values (names.body_change), so all it can do is
            # change the namespace the class / module is built from, and the patched import
            # builds exactly the same state, package-wide: whatever read the edited values
            # at creation (a metaclass overwriting the attribute, `SUPPORTS_COLUMN_JOIN_MARKS
            # = "(+)" in KEYWORDS`) produced the same result. Nothing can observe it.
            return set(), "state: the patched import builds identical state"
        if not any(kind == ("C" if r.kind == "class" else "M") and owner == own
                   for kind, owner, _ in changed):
            # the edit must show up: else the snapshot is blind to it
            raise names.Fallback(f"no import-time change seen in {own}")
        return self._state_readers(found, wide)

    def _state_readers(self, found: dict, wide: set[int] | None,
                       allow_empty: bool = False) -> tuple[set[int], str]:
        """`_by_state` for an import-state diff `found`: the readers of every changed value.
        Fallback when no test ran a reader, unless `allow_empty`."""
        changed = [k.split(" ", 2) for k in found["changed"]]
        index = self._index()
        info, with_instances = found["info"], set(found["instances"])
        touch = set(wide) if wide is not None else set()
        free: set[int] = set()  # readers of module globals
        loose: set[int] = set()  # class-attribute readers restricted only by `touch`
        typed: set[int] = set()  # tests that name a TypedDict whose key sets changed
        untyped = False  # some changed value is not a TypedDict key set
        held: set[int] = set()  # class-attribute readers among tests that can hold the class
        in_tests: set[int] = set()
        n_sites = 0
        owners: dict[str, tuple] = {}
        per_key: list[tuple[str, set[int], set[int]]] = []  # (key, reads, reads ∩ touch)
        for kind, owner, attr in changed:
            if kind == "C" and attr in TYPEDDICT_KEYS:
                # Set by the TypedDict metaclass; Python reads them only when it builds a
                # TypedDict subclass (at import: in the diff; at run time: a line naming the
                # class, as does handing the class to anything). Any other read must spell
                # the name (`X.__required_keys__`) or compute it (getattr, vars, __dict__).
                # The class object reaches other code only through a line naming it: at run
                # time those tests are taken; at import only as a base of another class in
                # its own module (whose key sets are in the diff), else it may be stored.
                cname = owner.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
                opath = self._module_path(owner.split(":", 1)[0])
                if index.mentions(attr) or opath is None or any(
                        site.when != "run" and not (
                            site.path == opath and self._old_text(opath).splitlines()[
                                site.lines[0] - 1].lstrip().startswith("class "))
                        for site in index.mentions(cname)):
                    raise names.Fallback(f"{owner}.{attr}: TypedDict read or held at import")
                key_typed = self._mention_runners({cname})
                typed |= key_typed
                per_key.append((f"{kind} {owner} {attr}", key_typed, set()))
                continue
            if attr.startswith("__") and attr.endswith("__"):
                raise names.Fallback(f"{owner}.{attr} is a special name")
            untyped = True
            if kind == "M":
                owner_path = self._module_path(owner)
                if owner_path is None:
                    raise names.Fallback(f"module {owner} not found")
                chain: tuple[str, ...] = ()
                users = None
            else:
                if owner not in owners:
                    owners[owner] = self._class_owner(owner, info, with_instances, index,
                                                      found.get("holders"))
                owner_path, chain, users = owners[owner]
                touch |= self.maps.whole_file(owner_path) | self._class_users(
                    _module_of(owner_path), [chain])
            same = frozenset(found.get("same", {}).get(f"{kind} {owner} {attr}", ()))
            key_free: set[int] = set()
            key_touched: set[int] = set()
            per_key.append((f"{kind} {owner} {attr}", key_free, key_touched))
            for site in index.sites(attr, owner_path, chain, True):
                if (kind == "C" and site.recv is not None
                        and not self._related(index, (site.path, site.recv), owner_path, chain)):
                    continue  # `self.attr` in a method of a class unrelated to the owner
                for bucket, got in self._site_reads(index, site, attr, same=same):
                    n_sites += 1
                    if bucket == "tests":
                        in_tests |= got
                        key_free |= got
                    elif kind == "M":
                        free |= got
                        key_free |= got
                    elif users is None or bucket == "loose":
                        loose |= got
                        key_touched |= got
                    else:
                        held |= got & users
                        key_touched |= got & users
        # Class-attribute reads stay within the users of the owner classes and their files
        # (the bound `_by_names` relies on); `held` narrows that further.
        got = free | ((loose | held) & touch) | in_tests
        got |= typed
        # changed values no test is seen reading (a reader the index is blind to, or none):
        # callers of `_state_readers(…, allow_empty)` let the lines' run-time callers stand in
        self._state_unread = [k for k, f, t in per_key if not f and not t & touch]
        if not got and not allow_empty and untyped:
            raise names.Fallback("no test ran a line reading a changed value")
        shown = sorted({f"{o.rsplit(':', 1)[-1]}.{a}" for _, o, a in changed})
        more = f" +{len(shown) - 6}" if len(shown) > 6 else ""
        return got, (f"state: {', '.join(shown[:6])}{more} → {n_sites} reader sites "
                     f"(globals {len(free)}, unrestricted {len(loose & touch)}, "
                     f"held {len(held)}, tests {len(in_tests)})")

    def _site_reads(self, index: nameindex.NameIndex, site: names.Site, attr: str,
                    depth: int = 0, same: frozenset[str] = frozenset()
                    ) -> list[tuple[str, set[int]]]:
        """Tests that can observe the value read at `site`, as (bucket, tests): "tests" (test
        code reads it), "loose" (a dynamic read: any object), "held" (a read through an
        object of the owner). Follows `site.flow`: a value bound to a local that is bound
        once is observed where the local is loaded; a value handed to a constructor that only
        stores it is observed where that attribute is read.

        `same`: what the edit leaves unchanged in the value ("truth": bool(value), "keys": the
        set of keys / elements; state.compare). A run-time read that only uses that part
        (Site.use: `if x.NAME:`, `k in x.NAME`) cannot observe the edit: no tests."""
        if site.when == "run" and site.use and site.use in same:
            return []
        if site.path.startswith("tests/") and not site.dynamic:
            if site.when == "run":
                return [("tests", self.maps.span(site.path, *site.lines))]
            # test-module import code: every test of that file depends on it
            return [("tests", self._by_file.get(site.path, set())
                     | set(self.maps.test_imports.get(site.path)))]
        if site.when == "def" and site.memo:
            raise names.Fallback(f"{attr} read in code cached across tests ({site.what})")
        # A computed-name lookup (getattr(x, v)) reads a class attribute through an object of
        # the class or through the class object itself: the same two ways a static `x.NAME`
        # read does, so it is restricted like one. Namespace listings (vars, __dict__, dir)
        # and test code stay unrestricted.
        bucket = "loose" if site.dynamic and not (
            site.lookup and not site.path.startswith("tests/")) else "held"
        flow = site.flow if site.when == "run" and not site.dynamic and depth < 3 else None
        if flow is not None and flow[0] == "local":
            got: set[int] = set()
            uses = flow[2] if len(flow) > 2 else ("",) * len(flow[1])
            for (first, last), use in zip(flow[1], uses):
                if use and use in same:
                    continue  # this load only tests what the edit keeps
                got |= self.maps.span(site.path, first, last)
            return [(bucket, got)]
        if flow is not None and flow[0] == "attr":
            _, path, chain, stored = flow
            out = []
            for inner in index.sites(stored, path, tuple(chain), False):
                # the stored attribute holds the same object: same projections
                out += self._site_reads(index, inner, stored, depth + 1,
                                        same if not inner.dynamic else frozenset())
            return out
        # "def" sites outside cached code run at import: their products are in the diff; the
        # line map still shows any test that ran them later.
        return [(bucket, self.maps.span(site.path, *site.lines))]

    def _related(self, index: nameindex.NameIndex, cls: tuple[str, tuple[str, ...]],
                 owner_path: str, owner_chain: tuple[str, ...]) -> bool:
        """Can an object of class `cls` (path, chain) or of a subclass read an attribute as
        the owner class defines it? Only if some class of the owner's family (the owner and
        its subclasses) is `cls` or derives from it. True when unsure (a metaclass, a base
        outside the repo)."""
        key = (owner_path, owner_chain)
        family = self._families.get(key)
        if family is None:
            family = self._families[key] = index.check_class(owner_path, owner_chain)
        for member in family:
            ancestors = self._ancestors(index, member)
            if ancestors is None or cls in ancestors:
                return True
        return False

    def _ancestors(self, index: nameindex.NameIndex,
                   cls: tuple[str, tuple[str, ...]]) -> set | None:
        """`cls` and every repo class it may derive from (bases matched by name, so a
        superset); None for a metaclass or a base outside the repo."""
        if cls in self._ancestor_memo:
            return self._ancestor_memo[cls]
        classes = index.data["classes"]
        out: set | None = set()
        todo = [cls]
        while todo:
            c = todo.pop()
            if c in out:
                continue
            out.add(c)
            entries = [e for e in classes.get(c[1][-1], ()) if (e[0], tuple(e[1])) == c]
            if not entries:
                out = None
                break
            # the bases as spelled (a `metaclass=` keyword is not an ancestor of instances)
            for dotted in entries[0][3]:
                base = dotted.rsplit(".", 1)[-1]
                if base == "type" or (base not in classes and base not in names.HARMLESS_BASES):
                    out = None
                    break
                todo.extend((p, tuple(ch)) for p, ch, *_ in classes.get(base, ()))
            if out is None:
                break
        self._ancestor_memo[cls] = out
        return out

    def _module_path(self, module: str) -> str | None:
        base = module.replace(".", "/")
        for rel in (f"{base}.py", f"{base}/__init__.py"):
            if (self.sqlglot / rel).is_file():
                return rel
        return None

    def _class_owner(self, key: str, info: dict, with_instances: set[str],
                     index: nameindex.NameIndex, holders: dict | None = None
                     ) -> tuple[str, tuple[str, ...], set[int] | None]:
        """(source path, class chain, tests that can hold the class; None: do not restrict)
        for a changed class. A class made by `type(...)` at import (a dialect's default
        Tokenizer) stands for its nearest ancestor defined in the source.

        Objects of the class built at import (a template parsed into a module constant) are
        reached through the state entries that hold them (`holders`, build-time snapshot):
        the tests that ran a line reading one of those entries join the holders."""
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
        if not ({key, real, *mro} & TRACKED_ROOTS):
            return path, chain, None  # the classes map does not record this hierarchy
        # Expression classes: who made one at run time (classes_run), plus who read an entry
        # holding one made at import. Other roots are also credited by dialect lookups, which
        # the holder table does not follow: those keep the classes map, unrestricted if made
        # at import.
        expr = EXPR_ROOT in {key, real, *mro} and self.maps.classes_run is not None
        users = self._holders(family, index, run=expr)
        if {key, real} & with_instances:  # objects built at import: a test need not make one
            if not expr:
                return path, chain, None
            via = self._entry_readers({key, real}, holders, index)
            if via is None:
                return path, chain, None
            users |= via
        return path, chain, users

    def _entry_readers(self, keys: set[str], holders: dict | None,
                       index: nameindex.NameIndex) -> set[int] | None:
        """Tests that ran a line reading a state entry that holds an import-time object of
        one of the classes `keys` (or a subclass). None: cannot tell (no holder table, an
        entry without a source name, a read in code cached across tests, too many)."""
        if holders is None or self.maps.classes_run is None:
            return None
        entries = sorted({e for k in keys for e in holders.get(k, ())})
        if not entries or len(entries) > MAX_HOLDER_ENTRIES:
            return None
        got: set[int] = set()
        for entry in entries:
            kind, owner, name = entry.split(" ", 2)
            if kind == "M":
                owner_path, chain = self._module_path(owner), ()
            else:
                module, _, qual = owner.partition(":")
                if "#" in owner or "<" in qual or not qual:
                    return None
                owner_path, chain = self._module_path(module), tuple(qual.split("."))
            if owner_path is None or (name.startswith("__") and name.endswith("__")):
                return None
            for site in index.sites(name, owner_path, chain, False):
                if site.path.startswith("tests/"):
                    got |= (self.maps.span(site.path, *site.lines) if site.when == "run"
                            else self._by_file.get(site.path, set())
                            | set(self.maps.test_imports.get(site.path)))
                elif site.when == "run":
                    got |= self.maps.span(site.path, *site.lines)
                elif site.memo:
                    return None  # a cached result may hand the object to any later test
                # other import-time reads: what they build is itself a holder entry
        return got

    def _holders(self, family: list[tuple[str, tuple[str, ...]]],
                 index: nameindex.NameIndex, run: bool = False) -> set[int]:
        """Tests that can hold one of these classes (or an object of one). `run`: count
        objects made at run time only (classes_run; the caller adds import-time holders)."""
        users: set[int] = set()
        made = self._made_users if run else self._class_users
        for p, ch in family:  # the class, its subclasses and their outer classes
            users |= made(_module_of(p), [ch])
            if len(ch) == 1:  # class attributes that hold it (`Parser = MySQLParser`)
                for hp, hch in index.holders(ch[0]):
                    users |= made(_module_of(hp), [hch])
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
            # a lower bound: run-time makers only (the holder entries only add tests)
            users = self._holders(index.check_class(path, chain), index,
                                  run=path.startswith("sqlglot/expressions/"))
        got: set[int] = set()
        for name in direct:
            for site in index.sites(name, path, chain, True):
                for bucket, tests in self._site_reads(index, site, name, same=PARTIAL_USES):
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

    def _function_at_import(self, path: str, first: int, last: int,
                            have: set[int] | None = None,
                            slack: set[int] = frozenset(),
                            own: str | None = None) -> tuple[set[int], bool]:
        """Tests that depend on function lines path:first..last having run while modules
        were imported (`_import_time`: ALL for `import sqlglot`, else the dependents of each
        such module) – unless the import-state diff (selector/state.py) shows that importing
        the patched package builds exactly the same state: every module global and class
        attribute, digested in full (containers, objects, closures and lambdas with their
        code, defaults). Then what the function computed at import is unchanged, and only
        tests that run it later can see the edit (the lines map has them). Only for modules
        under sqlglot/ (the diff imports those) and test modules whose own import-time code
        runs no sqlglot code (`_explained_root`); the diff costs ~1.2 s CPU, so only when the
        dependents add at least STATE_MIN_GAIN to `have` (what the commit selects already)
        less `slack`: slow tests in `have` the run-time pruners may drop, which they may
        only try when the import-time runs are shown not to matter (`_mixed_py`, r19).
        `own`: the state key of the edited def itself (a body edit). A def with a closure
        (`super()`'s `__class__` cell) is digested with its code, so the edit shows there;
        that entry is the edit, which its callers see (lines map), not an import-time
        product (whatever holds the function at import is digested as its own entry).

        Otherwise, when the patched package imports, the tests that can observe the changed
        import-time state (`_import_state_users`) if cheaper than the dependents.

        Returns (tests, True if the state diff showed no change, "readers" if the tests are
        the readers of the changed state, "readers-unseen" if so but some changed value had
        no seen reader (the lines' run-time callers stand in for it), else False)."""
        modules = self.maps.import_time.modules(path, first, last)
        if not modules:
            return set(), False
        try:
            wide, error = self._import_time(path, first, last), None
        except AllTests as exc:
            wide, error = None, exc
        have = have or set()
        base = self._cost(have - slack)
        if ((wide is None or self._cost(wide | have) - base >= STATE_MIN_GAIN)
                and all((m.split(".")[0] == "sqlglot" and m != "sqlglot.__main__")  # state.SKIP_MODULES
                        or self._explained_root(m, path, first, last) for m in modules)):
            found = self._state_diff()
            if found is not None and not [k for k in found["changed"] if k != own]:
                return set(), True
        if wide is None or self._cost(wide | have) - base >= STATE_MIN_GAIN:
            try:
                got, _ = self._import_state_users(path, first, last, modules, own)
                if self._state_unread:
                    # some changed value has no seen reader: the run-time callers stand in
                    # for it (`allow_empty`), so they must not be pruned at run time
                    if wide is None or self._seconds(got) < self._seconds(wide):
                        return got, "readers-unseen"
                elif (wide is None or self._seconds(got) < self._seconds(wide)
                        or (slack and not got & slack
                            and self._cost((got | have) - slack) < self._cost(wide | have))):
                    return got, "readers"
            except names.Fallback:
                pass
        if error is not None:
            raise error
        return wide, False

    def _import_state_users(self, path: str, first: int, last: int, modules: set[str],
                            hook: str | None = None) -> tuple[set[int], str]:
        """Lines path:first..last run while modules are imported (`modules`, import-time map;
        `import sqlglot` stands for every importer), and the patched import may build
        different state: the tests that can observe what that import-time run produced.

        Everything an import of sqlglot modules leaves behind is module globals and class
        attributes, which the import-state diff (selector/state.py) compares in full
        (containers, objects, functions with code / defaults / closures); every value it
        shows changed, and every value derived from one at import, is in the diff. A test can
        only see the difference by reading one of them: `_state_readers` (run-time readers,
        restricted to tests that can hold the owner class; cached readers → Fallback).
        Test modules are not in the diff: one whose own import-time code runs sqlglot code
        (testimport: not explained) may run these lines itself, so its dependents join
        (all test modules when the map collapsed the lines to `import sqlglot`).
        The tests that run the lines later, at run time, come from the lines map (callers).
        `hook`: the state key of the edited class-creation hook itself (`C mod:Cls
        __init_subclass__`): Python reads it only while creating classes, which is what
        this rule follows; its own entry is not a value to find readers of.
        Fallback: the patched package does not import, a changed special name, no class in
        the source for a changed class, a read in cached code."""
        found = self._state_diff()
        if found is None:
            raise names.Fallback("import-state diff unavailable")
        roots = (self._test_modules() if ImportTimeMap.ROOT in modules
                 else {m for m in modules if m.split(".")[0] == "tests"})
        extra: set[int] = set()
        for m in sorted(roots):
            if not self._explained_root(m, path, first, last):
                extra |= self._module_dependents(m)
        for m in modules:
            if m.split(".")[0] not in ("sqlglot", "tests") or m == "sqlglot.__main__":
                raise names.Fallback(f"imported by {m}, which the diff does not cover")
        if hook is not None and hook in found["changed"]:
            found = dict(found, changed=[k for k in found["changed"] if k != hook])
        self._state_unread = []
        if not found["changed"]:
            return extra, "state: the patched import builds identical state"
        # a changed value nobody is seen reading usually means a reader the index is blind
        # to: only accept that when the lines also run at test time (their callers stand in)
        got, why = self._state_readers(found, None,
                                       allow_empty=bool(self.maps.span(path, first, last)))
        return got | extra, why

    def _test_modules(self) -> set[str]:
        """Every module under tests/ (dotted names)."""
        if self._all_test_modules is None:
            out = set()
            for p in (self.sqlglot / "tests").rglob("*.py"):
                rel = p.relative_to(self.sqlglot).with_suffix("")
                parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
                out.add(".".join(parts))
            self._all_test_modules = out
        return self._all_test_modules

    def _explained_root(self, module: str, path: str, first: int, last: int) -> bool:
        """A test module whose own import-time code runs no sqlglot code of its own
        (selector/testimport; a package `__getattr__` only if the edit is not in it): what
        its import runs in sqlglot/ is what importing sqlglot modules runs, which the
        import-state diff covers."""
        if module.split(".")[0] != "tests":
            return False
        if self._test_imports is None:
            from .testimport import Checker
            self._test_imports = Checker(self.sqlglot)
        return self._test_imports.explained_for(module, path, first, last)

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

    def _made_users(self, module: str, chains) -> set[int]:
        """`_class_users` from the classes_run map: tests that made an object of the class
        outside an import of a sqlglot module (objects made at import are reached through
        their holder entries, `_entry_readers`). Without that map, `_class_users`."""
        run = self.maps.classes_run
        if run is None:
            return self._class_users(module, chains)
        out: set[int] = set()
        for chain in chains:
            for cut in range(1, len(chain) + 1):
                out |= run.get(f"{module}.{'.'.join(chain[:cut])}")
        return out

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
        for name in classify.NESTED_STMTS:
            for child in getattr(stmt, name, None) or ():
                yield from _module_scope([child] if isinstance(child, ast.stmt) else child.body)


def _is_future(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__"


def _has_docstring(body: list[ast.stmt]) -> bool:
    return bool(body) and classify.is_docstring(body[0])


def _leading_futures(body: list[ast.stmt]) -> int:
    """Length of the leading docstring + `from __future__` imports."""
    n = int(_has_docstring(body))
    while n < len(body) and _is_future(body[n]):
        n += 1
    return n


def _module_of(path: str) -> str:
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def _in_modules(owner: str, modules: set[str]) -> bool:
    """Is a state-diff owner (`module`, `module:Qual`, or an anonymous class key
    `<anon>.module.Qual…`) defined in one of `modules`?"""
    owner = owner.removeprefix("<anon>.").split(":", 1)[0].rstrip("#")
    return any(owner == m or owner.startswith(m + ".") for m in modules)


_MUTATORS = {"pop", "popitem", "update", "append", "add", "extend", "setdefault", "clear",
             "remove", "discard", "insert"}


def _literal(node: ast.AST | None):
    """(True, value) for a literal default of an immutable scalar type (bool, int, finite
    float, str, None) that JSON round-trips exactly; (False, None) otherwise."""
    if node is None:
        return False, None
    try:
        v = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False, None
    if v is None or type(v) in (bool, int, str) or (type(v) is float and v == v
                                                       and v not in (float("inf"), float("-inf"))):
        if type(v) is int and abs(v) > 2 ** 53:
            return False, None
        return True, v
    return False, None


def _default_changes(old_tree: ast.Module, new_tree: ast.Module) -> list | None:
    """The edits of a file if all of them change literal default values of defs (not
    lambdas): [(def name, first line of its code object, {param: (old, new)})]; else None.

    With nothing else different, the old and the new module build the same code objects;
    each def only gets other `__defaults__` / `__kwdefaults__`, which a call reads only for
    a parameter it did not pass."""
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)

    def blank(tree: ast.Module) -> tuple[str, list]:
        import copy
        t = copy.deepcopy(tree)
        found = []
        for node in ast.walk(t):
            if isinstance(node, funcs + (ast.Lambda,)):
                a = node.args
                found.append((node, list(a.defaults), list(a.kw_defaults)))
                a.defaults = [ast.Constant(None) for _ in a.defaults]
                a.kw_defaults = [None if d is None else ast.Constant(None) for d in a.kw_defaults]
        return ast.dump(t), found

    old_dump, old_defs = blank(old_tree)
    new_dump, new_defs = blank(new_tree)
    if old_dump != new_dump or len(old_defs) != len(new_defs):
        return None
    out = []
    for (o, od, okw), (n, nd, nkw) in zip(old_defs, new_defs):
        if ast.dump(ast.Tuple(od + [d or ast.Constant(...) for d in okw])) == \
                ast.dump(ast.Tuple(nd + [d or ast.Constant(...) for d in nkw])):
            continue
        if not isinstance(o, funcs):
            return None  # a lambda's defaults: no named code object to find
        a = o.args
        positional = [x.arg for x in a.posonlyargs + a.args]
        names = positional[len(positional) - len(od):] + [x.arg for x in a.kwonlyargs]
        params = {}
        for name, x, y in zip(names, od + okw, nd + nkw):
            if (x is None and y is None) or (x is not None and y is not None
                                             and ast.dump(x) == ast.dump(y)):
                continue
            ok_x, vx = _literal(x)
            ok_y, vy = _literal(y)
            if not (ok_x and ok_y):
                return None
            if ast.dump(x) != ast.dump(y):
                params[name] = (vx, vy)
        if not params:
            return None
        first = min([o.lineno] + [d.lineno for d in o.decorator_list])
        out.append((o.name, first, params))
    return out or None


def _edited_defs(old_tree: ast.Module, new_tree: ast.Module, r: classify.Region) -> list:
    """The def a function region edits, in the old and the new tree (a lambda: none)."""
    if not r.chain or r.is_lambda:
        return []
    name = r.chain[-1]
    out = []
    for tree in (old_tree, new_tree):
        scopes: list[ast.AST] = [tree]
        for part in r.chain:  # follow the chain through class / def bodies (no full walk)
            scopes = [c for s in scopes for c in getattr(s, "body", ())
                      if isinstance(c, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      and c.name == part]
        if not scopes:  # defined under if / try: search the whole tree
            scopes = [f for f in ast.walk(tree) if isinstance(
                f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == name]
        out += [f for f in scopes if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return out


def _class_state_writes(defs: list) -> set[str]:
    """Class-level tables (UPPER_CASE attributes reached through `self` / `cls` /
    `type(self)`) that the edited def, in either version, mutates in place: item stores and
    deletes, augmented assignments, mutating method calls."""
    out: set[str] = set()

    def table(node: ast.AST) -> str | None:
        if (isinstance(node, ast.Attribute) and node.attr.lstrip("_").isupper()
                and (isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")
                     or isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                     and node.value.func.id == "type")):
            return node.attr
        return None

    for fn in defs:
        for node in ast.walk(fn):
            hit = None
            if isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Load):
                hit = table(node.value)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
                hit = table(node.target)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in _MUTATORS):
                hit = table(node.func.value)
            if hit:
                out.add(hit)
    return out



def _fresh_tables(defs: list) -> set[str]:
    """Class-level tables that every def in `defs` binds, as a plain top-level statement of
    its body, to a new object (`cls.X = {...}` / a comprehension / `set(...)` / `dict(...)` /
    `list(...)` / `x.copy()`) before any other statement mentions `cls.X`, with the receiver
    (`self` / `cls`) never rebound: the def's later in-place writes to X reach only that
    object, not a table another class (a base) also holds."""
    out: set[str] | None = None
    fresh_calls = {"set", "dict", "list"}
    for fn in defs:
        here: set[str] = set()
        rebound = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)
                   and n.id in ("self", "cls") and not isinstance(n.ctx, ast.Load)}
        seen: set[str] = set()  # tables mentioned so far
        for stmt in fn.body:
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Attribute)
                    and isinstance(stmt.targets[0].value, ast.Name)
                    and stmt.targets[0].value.id in ("self", "cls")
                    and stmt.targets[0].value.id not in rebound):
                name, v = stmt.targets[0].attr, stmt.value
                new = (isinstance(v, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp,
                                      ast.SetComp))
                       or isinstance(v, ast.Call) and (
                           isinstance(v.func, ast.Name) and v.func.id in fresh_calls
                           or isinstance(v.func, ast.Attribute) and v.func.attr == "copy"
                           and not v.args and not v.keywords))
                inside = {n.attr for n in ast.walk(v) if isinstance(n, ast.Attribute)}
                if new and name not in seen and name not in inside:
                    here.add(name)
            seen |= {n.attr for n in ast.walk(stmt) if isinstance(n, ast.Attribute)}
        out = here if out is None else out & here
    return out or set()

_INERT_STMTS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom,
                ast.Pass, ast.Global, ast.Nonlocal)


def _side_effects(body: list[ast.stmt]) -> bool:
    """Does a class / module body run statements beyond binding names: item / attribute
    stores, augmented assignments, calls as statements (`KEYWORDS.pop(...)`), loops?"""
    for stmt in body:
        if isinstance(stmt, _INERT_STMTS) or (isinstance(stmt, ast.Expr)
                                              and isinstance(stmt.value, ast.Constant)):
            continue
        if isinstance(stmt, ast.Assign):
            if all(isinstance(n, (ast.Name, ast.Tuple, ast.List, ast.Starred, ast.Load, ast.Store))
                   for t in stmt.targets for n in ast.walk(t)):
                continue
            return True
        if isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name):
                continue
            return True
        if isinstance(stmt, ast.If):
            if "TYPE_CHECKING" in ast.dump(stmt.test):
                continue
            if _side_effects(stmt.body) or _side_effects(stmt.orelse):
                return True
            continue
        if isinstance(stmt, ast.Try):
            if any(_side_effects(b) for b in (stmt.body, stmt.orelse, stmt.finalbody,
                                               *[h.body for h in stmt.handlers])):
                return True
            continue
        return True
    return False


def _may_share(old_tree: ast.Module, new_tree: ast.Module, r: classify.Region) -> bool:
    """Can a class / module body edit change objects other modules own? Only if a version of
    the body mutates objects (`_side_effects`) or a changed assignment now binds an existing
    object by reference (`X = Base.X`: an alias, where the old value was a copy)."""
    bodies = []
    for tree in (old_tree, new_tree):
        if r.kind == "class":
            node = names._find_class(tree, r.chain)
            if node is None:
                return False  # class added / removed: handled as a removed binding / new name
            bodies.append(node.body)
        else:
            bodies.append(tree.body)
    if _side_effects(bodies[0]) or _side_effects(bodies[1]):
        return True
    aliases = [s for s in bodies[1] if isinstance(s, (ast.Assign, ast.AnnAssign))
               and isinstance(s.value, (ast.Name, ast.Attribute))]
    if not aliases:
        return False
    old = {ast.dump(s) for s in bodies[0] if isinstance(s, (ast.Assign, ast.AnnAssign))
           and isinstance(s.value, (ast.Name, ast.Attribute))}
    return any(ast.dump(s) not in old for s in aliases)


def name_of(path: str) -> str:
    return PurePosixPath(path).name


def _bindings(tree: ast.Module) -> set[str]:
    """Names bound at module level and in class bodies (nested classes included), as
    dotted paths: `func`, `Class`, `Class.attr`, `Class.method`. Function bodies are skipped."""
    return set(_binding_kinds(tree))


def _unseen_body_change(old_tree: ast.Module, new_tree: ast.Module) -> str | None:
    """A module- or class-body edit the import-state diff cannot show: a changed class head
    (bases, keywords, decorators), or a changed statement that is not a def, a class or an
    assignment. None if there is none."""
    plain = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign,
             ast.AugAssign)

    def classes(tree: ast.Module) -> dict[tuple[str, ...], ast.ClassDef]:
        out: dict[tuple[str, ...], ast.ClassDef] = {}
        todo: list[tuple[list[ast.stmt], tuple[str, ...]]] = [(tree.body, ())]
        while todo:
            body, chain = todo.pop()
            for node in body:
                if isinstance(node, ast.ClassDef):
                    out[chain + (node.name,)] = node
                    todo.append((node.body, chain + (node.name,)))
        return out

    def others(body: list[ast.stmt]) -> list[str]:
        return [ast.dump(s) for s in body if not isinstance(s, plain)]

    old, new = classes(old_tree), classes(new_tree)
    for chain in old.keys() & new.keys():
        if classify._class_head(old[chain]) != classify._class_head(new[chain]):
            return f"class {'.'.join(chain)}: bases / keywords / decorators changed"
        if others(old[chain].body) != others(new[chain].body):
            return f"class {'.'.join(chain)}: a statement other than def / class / assignment changed"
    if others(old_tree.body) != others(new_tree.body):
        return "a module-level statement other than def / class / assignment changed"
    return None


def _from_module(path: str, node: ast.ImportFrom) -> str:
    """The absolute module a `from … import` in file `path` names."""
    if not node.level:
        return node.module or ""
    package = _module_of(path) if path.endswith("__init__.py") else _module_of(path).rpartition(".")[0]
    parts = package.split(".")
    base = ".".join(parts[:len(parts) - (node.level - 1)])
    return f"{base}.{node.module}" if node.module else base


def _import_froms(tree: ast.Module, name: str):
    """(`from … import name` node, inside a def or lambda) for every such import."""
    stack: list[tuple[ast.AST, bool]] = [(tree, False)]
    while stack:
        node, in_def = stack.pop()
        if isinstance(node, ast.ImportFrom) and any(a.name == name for a in node.names):
            yield node, in_def
        inner = in_def or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        stack.extend((c, inner) for c in ast.iter_child_nodes(node))


def _binding_kinds(tree: ast.Module) -> dict[str, str]:
    """`_bindings` → how each is bound: "def", "class", "assign", "import", or "mixed"."""
    out: dict[str, str] = {}

    def add(name: str, kind: str) -> None:
        out[name] = kind if out.get(name, kind) == kind else "mixed"

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add(prefix + node.name, "def")
            elif isinstance(node, ast.ClassDef):
                add(prefix + node.name, "class")
                walk(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            add(prefix + n.id, "assign")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    add(prefix + (a.asname or a.name).split(".")[0], "import")
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
