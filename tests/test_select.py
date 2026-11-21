"""One test per row of the change-kind table (research/PLAN.md §3), on a tiny fake repo.

Run: python -m pytest tests/
"""

from __future__ import annotations

import difflib
import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selector.select import Selector  # noqa: E402

# A miniature sqlglot checkout. Line numbers matter: the maps below refer to them.
LIB = '''\
"""Module docstring."""
LIMIT = 10


class Dialect:
    WEEK_OFFSET = 0

    def parse(self, sql):
        """Parse it."""
        if sql and (
            len(sql) > LIMIT
        ):
            return sql
        return None

    def never_run(self):
        return 1


class _Meta(type):
    def __new__(cls, name, bases, attrs):
        return super().__new__(cls, name, bases, attrs)
'''
OTHER = "from sqlglot.lib import Dialect\n\ndef use():\n    return Dialect\nSHARED = 1\n"
BASE = "class Parser:\n    STRICT = False\n\n    def run(self):\n        return self.STRICT\n"
TEST = '''\
from tests.helpers import load


class TestLib:
    def setup_method(self):
        self.x = 1

    def test_a(self):
        assert load() == 1

    def test_b(self):
        assert True
'''
HELPERS = ("TABLE = {\"a\": 1}\n\n\ndef load():\n    return TABLE[\"a\"]\n\n\n"
           "def unused():\n    return 2\n")
FIXTURE = "# meta: x\nSELECT 1;\n"

FILES = {
    "sqlglot/lib.py": LIB,
    "sqlglot/other.py": OTHER,
    "sqlglot/parser.py": BASE,
    "sqlglot/third.py": "from sqlglot.other import SHARED\n",
    "sqlglot/fourth.py": "VALUE = 1\n",
    "sqlglot/fifth.py": "from sqlglot.fourth import VALUE\n\n\ndef f():\n    return VALUE\n",
    "sqlglot/sixth.py": "def helper():\n    return 1\n",
    "sqlglot/seventh.py": "from sqlglot.sixth import helper\n\n\ndef g():\n    return helper()\n",
    "sqlglot/eighth.py": "BASE_NAMES = [1]\nDERIVED = list(BASE_NAMES)\n",
    "sqlglot/ninth.py": "from sqlglot.eighth import DERIVED\n\n\ndef h():\n    return DERIVED\n",
    "sqlglot/tenth.py": "def boot():\n    return 1\n",
    "sqlglot/slow_cache.py": ("_CACHE = {}\n\n\ndef get(key):\n    if key not in _CACHE:\n"
                           "        _CACHE[key] = key * 2\n    return _CACHE[key]\n"),
    "sqlglot/slow_lambda.py": "class P:\n    F = {\n        \"a\": lambda x: (\n            x + 1\n        ),\n    }\n",
    "tests/conftest.py": "import pytest\n",
    "tests/test_lib.py": TEST,
    "tests/helpers.py": HELPERS,
    "tests/fixtures/q.sql": FIXTURE,
    "tests/fixtures/unread.sql": FIXTURE,
    "pyproject.toml": '[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n',
    "README.md": "hello\n",
    "setup.py": "from setuptools import setup\n\nsetup(name=\"x\")\n",
    "MANIFEST.in": "include LICENSE\n",
    ".gitmodules": "[submodule \"x\"]\n",
    ".gitpod.yml": "tasks: []\n",
}

T = ["tests/test_lib.py::TestLib::test_a", "tests/test_lib.py::TestLib::test_b",
     "tests/test_other.py::TestOther::test_c", "tests/test_other.py::TestOther::test_d",
     "tests/test_new.py::TestNew::test_untraced"]
A, B, C, D, U = range(5)
ALL = set(range(5))

MAPS = {
    "lines": {"sqlglot/lib.py:10": [C], "sqlglot/lib.py:13": [C], "sqlglot/lib.py:14": [D],
              "sqlglot/other.py:4": [A], "sqlglot/parser.py:5": [B],
              "sqlglot/fifth.py:5": [B], "sqlglot/sixth.py:2": [C], "tests/helpers.py:5": [A], "sqlglot/seventh.py:5": [D],
              "sqlglot/ninth.py:5": [A],
              "sqlglot/slow_lambda.py:3": [D], "sqlglot/slow_lambda.py:4": [C],
              "sqlglot/slow_cache.py:5": [C, D], "sqlglot/slow_cache.py:6": [C], "sqlglot/slow_cache.py:7": [C, D]},
    "classes": {"sqlglot.lib.Dialect": [A]},
    "reads": {"tests/fixtures/q.sql": [B], ".gitpod.yml": [C]},
    "test_imports": {"tests/helpers.py": [A, B]},
}


@pytest.fixture
def selector(tmp_path: Path) -> Selector:
    return make_selector(tmp_path)


def make_selector(tmp_path: Path, slow: tuple[int, ...] = ()) -> Selector:
    """The fake repo; tests in `slow` take 30 s (every other test takes 1 s)."""
    repo, maps = tmp_path / "sqlglot", tmp_path / "maps"
    for path, text in FILES.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    maps.mkdir()
    for name, mapping in MAPS.items():
        traced = T[:4]  # test U appears in no map
        body = {"tests": traced, "map": mapping}
        (maps / f"{name}.json.gz").write_bytes(gzip.compress(json.dumps(body).encode()))
    # Import-time map: line → modules whose import runs it (root 0 = `import sqlglot`).
    roots = ["sqlglot", "sqlglot.other", "tests.test_lib"]
    body = {"tests": roots, "map": {"sqlglot/tenth.py:2": [0], "sqlglot/fifth.py:5": [2],
                                    "sqlglot/seventh.py:1": [1], "sqlglot/ninth.py:1": [2]}}
    (maps / "import_time.json.gz").write_bytes(gzip.compress(json.dumps(body).encode()))
    tests = [{"number": i, "id": t, "file": t.split("::")[0], "seconds": 30.0 if i in slow else 1.0}
             for i, t in enumerate(T)]
    return Selector(tests, repo, maps)


def patch(path: str, old: str | None, new: str | None) -> str:
    a = (old or "").splitlines(keepends=True)
    b = (new or "").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, "/dev/null" if old is None else f"a/{path}",
                                        "/dev/null" if new is None else f"b/{path}"))


def edit(path: str, before: str, after: str) -> str:
    old = FILES[path]
    assert before in old
    return patch(path, old, old.replace(before, after, 1))


def run(selector: Selector, text: str) -> set[int]:
    return selector.select(text)[0]


# ── .py: no behaviour change ─────────────────────────────────────────────────

def test_comment_only_selects_nothing(selector):
    assert run(selector, edit("sqlglot/lib.py", "LIMIT = 10\n", "LIMIT = 10  # cap\n")) == set()


def test_docstring_only_selects_nothing(selector):
    assert run(selector, edit("sqlglot/lib.py", '"""Parse it."""', '"""Parse SQL."""')) == set()


def test_syntax_error_selects_all(selector):
    assert run(selector, edit("sqlglot/lib.py", "return sql\n", "return sql +\n")) == ALL


def test_hunk_that_does_not_apply_selects_all(selector):
    bad = patch("sqlglot/lib.py", "nothing like the file\n", "x\n")
    assert run(selector, bad) == ALL


# ── sqlglot/: functions ──────────────────────────────────────────────────────

def test_function_body_uses_whole_function_span(selector):
    # Line 11 never ran (short-circuit), but tests that ran the function are selected,
    # plus the untraced test U, which joins every non-empty selection.
    got = run(selector, edit("sqlglot/lib.py", "len(sql) > LIMIT", "len(sql) >= LIMIT"))
    assert got == {C, D, U}


def test_slow_test_off_the_touched_statement_is_dropped(tmp_path):
    # D (slow) ran the function (line 14) but not the edited `return sql` (line 13).
    selector = make_selector(tmp_path, slow=(D,))
    assert run(selector, edit("sqlglot/lib.py", "return sql\n", "return sql[1:]\n")) == {C, U}


def test_slow_test_on_another_line_of_the_touched_header_is_kept(tmp_path):
    # C (slow) ran line 10 of the 3-line `if` header, not the edited line 11.
    selector = make_selector(tmp_path, slow=(C,))
    got = run(selector, edit("sqlglot/lib.py", "len(sql) > LIMIT", "len(sql) >= LIMIT"))
    assert got == {C, D, U}


def test_slow_test_kept_when_statements_are_inserted(tmp_path):
    # A new statement has no old statement to anchor it: whole-function rule.
    selector = make_selector(tmp_path, slow=(D,))
    got = run(selector, edit("sqlglot/lib.py", "return sql\n", "sql = sql.strip()\n            return sql\n"))
    assert got == {C, D, U}


def test_slow_test_kept_when_the_edit_changes_name_bindings(tmp_path):
    # `LIMIT :=` makes LIMIT local to parse(): line 11, which every caller runs, now fails.
    selector = make_selector(tmp_path, slow=(D,))
    got = run(selector, edit("sqlglot/lib.py", "return sql\n", "return (LIMIT := sql)\n"))
    assert got == {C, D, U}


def test_slow_test_kept_for_a_lambda_region(tmp_path):
    # A lambda in a class-level dict has no enclosing def body: whole-lambda rule.
    selector = make_selector(tmp_path, slow=(D,))
    assert run(selector, edit("sqlglot/slow_lambda.py", "x + 1", "x + 2")) == {C, D, U}


def test_slow_test_kept_when_the_function_fills_a_module_cache(tmp_path):
    # The map credits the cache fill (line 6) to whichever test ran first in the process;
    # D, which only read the cache in the mapping run, may be the first one in the real run.
    selector = make_selector(tmp_path, slow=(D,))
    assert run(selector, edit("sqlglot/slow_cache.py", "key * 2", "key * 3")) == {C, D, U}


def test_signature_change_adds_class_users(selector):
    got = run(selector, edit("sqlglot/lib.py", "def parse(self, sql):", "def parse(self, sql=None):"))
    assert got == {A, C, D, U}


def test_function_no_test_ran_is_treated_as_definition_time(selector):
    got = run(selector, edit("sqlglot/lib.py", "return 1\n", "return 2\n"))
    assert got == {A, C, D, U}  # class users ∪ tests that ran the file


def test_metaclass_method_is_class_body(selector):
    got = run(selector, edit("sqlglot/lib.py", "super().__new__(cls, name, bases, attrs)",
                             "super().__new__(cls, name, bases, dict(attrs))"))
    # _Meta has no users in the class map → module users ∪ tests that ran the file.
    assert got == {A, C, D, U}


# ── sqlglot/: class and module bodies ────────────────────────────────────────

def test_class_body_selects_class_users_and_file(selector):
    got = run(selector, edit("sqlglot/lib.py", "WEEK_OFFSET = 0", "WEEK_OFFSET = 1"))
    assert got == {A, C, D, U}


def test_new_method_is_a_class_body_change(selector):
    got = run(selector, edit("sqlglot/lib.py", "    def never_run", "    def extra(self):\n        return 3\n\n    def never_run"))
    assert got == {A, C, D, U}


def test_private_module_constant_selects_tests_that_read_it(selector):
    # LIMIT is read only in the `if` of Dialect.parse (lines 10-12), which test C ran
    # (selector/names.py; tests/test_names.py covers the narrowing in detail).
    assert run(selector, edit("sqlglot/lib.py", "LIMIT = 10", "LIMIT = 11")) == {C, U}


def test_module_constant_used_elsewhere_selects_all(selector):
    assert run(selector, edit("sqlglot/other.py", "SHARED = 1", "SHARED = 2")) == ALL


def test_module_import_change_selects_all(selector):
    assert run(selector, edit("sqlglot/other.py", "from sqlglot.lib import Dialect",
                              "from sqlglot.lib import Dialect, LIMIT")) == ALL


def test_module_constant_follows_importers(selector):
    # VALUE is imported by fifth.py, whose function test B ran.
    assert run(selector, edit("sqlglot/fourth.py", "VALUE = 1", "VALUE = 2")) == {B, U}


def test_renamed_module_selects_all(selector):
    text = ("--- a/sqlglot/other.py\n+++ b/sqlglot/moved.py\n@@ -1 +1 @@\n"
            "-from sqlglot.lib import Dialect\n+from sqlglot.lib import Dialect\n")
    assert run(selector, text) == ALL


def test_base_class_body_narrows_to_name_readers(selector):
    # Base files: no conservative bound, but the name readers still narrow (line 5 reads it).
    assert run(selector, edit("sqlglot/parser.py", "STRICT = False", "STRICT = True")) == {B, U}


def test_base_class_body_without_narrowing_selects_all(selector):
    # A value computed at import cannot be narrowed by name, and there is no state
    # snapshot here: base files never fall back to the conservative set → ALL.
    assert run(selector, edit("sqlglot/parser.py", "STRICT = False", "STRICT = bool(0)")) == ALL


def test_base_file_function_body_uses_map(selector):
    assert run(selector, edit("sqlglot/parser.py", "return self.STRICT", "return not self.STRICT")) == {B, U}


def test_new_module_selects_nothing(selector):
    assert run(selector, patch("sqlglot/brand_new.py", None, "X = 1\n")) == set()


def test_deleted_module_selects_all(selector):
    assert run(selector, patch("sqlglot/other.py", OTHER, None)) == ALL


# ── tests/ ───────────────────────────────────────────────────────────────────

def test_test_function_edit_selects_that_test(selector):
    assert run(selector, edit("tests/test_lib.py", "assert True", "assert False")) == {B, U}


def test_test_setup_edit_selects_whole_file(selector):
    assert run(selector, edit("tests/test_lib.py", "self.x = 1", "self.x = 2")) == {A, B, U}


def test_test_helper_function_selects_tests_that_ran_it(selector):
    assert run(selector, edit("tests/helpers.py", 'return TABLE["a"]', 'return TABLE["a"] + 1')) == {A, U}


def test_test_helper_constant_selects_tests_that_read_it(selector):
    assert run(selector, edit("tests/helpers.py", '{"a": 1}', '{"a": 2}')) == {A, U}


def test_test_helper_function_no_test_ran_selects_importers(selector):
    assert run(selector, edit("tests/helpers.py", "return 2", "return 3")) == {A, B, U}


def test_test_helper_function_run_at_import_selects_importers(selector):
    # A body that runs while a test module is imported (collection) is invisible to the
    # lines map: keep the importers rule.
    selector.maps.import_time.modules = lambda path, first, last: (
        {"tests.test_lib"} if path == "tests/helpers.py" else set())
    assert run(selector, edit("tests/helpers.py", 'return TABLE["a"]', 'return TABLE["a"] + 1')) == {A, B, U}


def test_test_helper_signature_change_selects_importers(selector):
    assert run(selector, edit("tests/helpers.py", "def load():", "def load(x=1):")) == {A, B, U}


def test_fixture_selects_readers_without_ast_filter(selector):
    # A '#' line in a fixture is metadata, not a comment: it must still select readers.
    assert run(selector, edit("tests/fixtures/q.sql", "# meta: x", "# meta: y")) == {B, U}


def test_renamed_fixture_selects_old_readers(selector):
    text = ("--- a/tests/fixtures/q.sql\n+++ b/tests/fixtures/q2.sql\n@@ -1 +1 @@\n"
            "-# meta: x\n+# meta: x\n")
    assert run(selector, text) == {B, U}


def test_conftest_selects_all(selector):
    assert run(selector, edit("tests/conftest.py", "import pytest", "import os")) == ALL


def test_unread_fixture_selects_nothing(selector):
    assert run(selector, edit("tests/fixtures/unread.sql", "SELECT 1", "SELECT 2")) == set()


# ── other files ──────────────────────────────────────────────────────────────

def test_docs_select_nothing(selector):
    assert run(selector, edit("README.md", "hello", "bye")) == set()


def test_pyproject_outside_pytest_section_selects_nothing(selector):
    assert run(selector, edit("pyproject.toml", 'name = "x"', 'name = "y"')) == set()


def test_pyproject_pytest_section_selects_all(selector):
    assert run(selector, edit("pyproject.toml", 'addopts = "-q"', 'addopts = "-x"')) == ALL


def test_unknown_file_selects_all(selector):
    assert run(selector, patch("tools/run.sh", None, "echo hi\n")) == ALL


def test_multi_file_patch_is_the_union(selector):
    text = edit("tests/fixtures/q.sql", "SELECT 1", "SELECT 2") + edit(
        "sqlglot/lib.py", "LIMIT = 10\n", "LIMIT = 10  # cap\n")
    assert run(selector, text) == {B, U}


def test_bindings_detects_removed_names():
    """A renamed def or class attribute must be reported as a lost binding."""
    import ast as _ast
    from selector.select import _bindings
    old = _ast.parse("def record(): pass\nclass A:\n    X = 1\n    def f(self): pass\n")
    new = _ast.parse("def record2(): pass\nclass A:\n    X = 1\n    def g(self): pass\n")
    assert _bindings(old) - _bindings(new) == {"record", "A.f"}


# ── import bound (imports map) ───────────────────────────────────────────────

IMPORTS = {"sqlglot/other.py": [A], "sqlglot/lib.py": [A, C, D], "sqlglot/parser.py": [A, B, C, D],
           "sqlglot/fourth.py": [B], "tests/test_lib.py": [A, B], "<isolation-failure>": []}


COLLECTED = ["sqlglot/parser.py", "tests/helpers.py", "tests/test_lib.py"]
COLLECTED_BY_IMPORT = ["sqlglot/parser.py", "tests/helpers.py"]  # test_lib.py: pytest only


def _with_imports(selector: Selector, tmp_path: Path, mapping: dict, **extra) -> Selector:
    body = {"tests": T[:4], "map": mapping,  # U is untraced: always added
            "collection": COLLECTED, "collection_imported": COLLECTED_BY_IMPORT, **extra}
    body = {k: v for k, v in body.items() if v is not None}
    (tmp_path / "maps" / "imports.json.gz").write_bytes(gzip.compress(json.dumps(body).encode()))
    return Selector(selector.tests, selector.sqlglot, tmp_path / "maps")


@pytest.fixture
def bounded(selector: Selector, tmp_path: Path) -> Selector:
    return _with_imports(selector, tmp_path, IMPORTS)


def test_syntax_error_selects_tests_that_load_the_module(bounded):
    assert run(bounded, edit("sqlglot/lib.py", "return sql\n", "return sql +\n")) == {A, C, D, U}


def test_deleted_module_selects_tests_that_load_it(bounded):
    assert run(bounded, patch("sqlglot/other.py", OTHER, None)) == {A, U}


def test_renamed_module_selects_tests_that_load_the_old_path(bounded):
    text = ("--- a/sqlglot/other.py\n+++ b/sqlglot/moved.py\n@@ -1 +1 @@\n"
            "-from sqlglot.lib import Dialect\n+from sqlglot.lib import Dialect\n")
    assert run(bounded, text) == {A, U}


def test_rule_that_gives_up_falls_back_to_import_bound(bounded):
    # module-level import change and base-class body were ALL
    assert run(bounded, edit("sqlglot/other.py", "from sqlglot.lib import Dialect",
                             "from sqlglot.lib import Dialect, LIMIT")) == {A, U}
    assert run(bounded, edit("sqlglot/parser.py", "STRICT = False", "STRICT = bool(0)")) == ALL


def test_hunk_that_does_not_apply_still_selects_all(bounded):
    assert run(bounded, patch("sqlglot/lib.py", "nothing like the file\n", "x\n")) == ALL


def test_removed_binding_selects_tests_that_load_the_module(bounded):
    # Dialect is imported by other.py → the removal is visible → import bound of lib.py
    assert run(bounded, edit("sqlglot/lib.py", "class Dialect:", "class Dialect2:")) == {A, C, D, U}


def test_rename_onto_conftest_or_init_selects_all(bounded):
    for target in ("tests/conftest.py", "sqlglot/__init__.py", "setup.py", "sqlglot/x.txt"):
        text = (f"--- a/sqlglot/other.py\n+++ b/{target}\n@@ -1 +1 @@\n"
                "-from sqlglot.lib import Dialect\n+from sqlglot.lib import Dialect\n")
        assert run(bounded, text) == ALL, target


def test_module_nothing_loads_selects_only_unknown_tests(bounded):
    # only the untraced test U, which the map cannot vouch for
    assert run(bounded, patch("sqlglot/third.py", FILES["sqlglot/third.py"], None)) == {U}


def test_isolation_failures_join_the_bound(selector, tmp_path):
    sel = _with_imports(selector, tmp_path, {**IMPORTS, "<isolation-failure>": [D]})
    assert run(sel, patch("sqlglot/other.py", OTHER, None)) == {A, D, U}


def test_syntax_error_in_collected_file_selects_all(bounded):
    # a collection error: pytest's default interrupts the whole session
    assert run(bounded, edit("tests/test_lib.py", "assert True", "assert True +")) == ALL
    assert run(bounded, edit("tests/helpers.py", "return 2", "return 2 +")) == ALL


def test_deleted_test_file_selects_all(bounded):
    """A deleted test file may abort the labeler's whole pytest run ("not found"): ALL."""
    assert run(bounded, patch("tests/test_lib.py", TEST, None)) == ALL


def test_deleted_module_imported_at_collection_selects_all(bounded):
    assert run(bounded, patch("tests/helpers.py", HELPERS, None)) == ALL


def test_map_without_collection_lists_selects_all(selector, tmp_path):
    sel = _with_imports(selector, tmp_path, IMPORTS, collection=None, collection_imported=None)
    assert run(sel, patch("sqlglot/other.py", OTHER, None)) == ALL

# ── import-time code, removed names, derived names, hunkless entries ─────────

def test_function_run_by_import_sqlglot_selects_all(selector):
    assert run(selector, edit("sqlglot/tenth.py", "return 1", "return 2")) == ALL


def test_function_run_by_import_sqlglot_with_unchanged_import_state(selector):
    # The import-state diff shows the patched import builds the same state: what boot()
    # computed at import is unchanged, and no test runs it later.
    selector._state_diff = lambda: {"changed": []}
    assert run(selector, edit("sqlglot/tenth.py", "return 1", "return 2")) == set()


def test_function_run_by_import_sqlglot_with_changed_import_state_selects_all(selector):
    # A changed value no test is seen reading, and the function never runs under a test:
    # the name index may be blind to the reader → ALL.
    selector._state_diff = lambda: {"changed": ["M sqlglot.tenth NOBODY_READS"], "info": {},
                                    "instances": []}
    assert run(selector, edit("sqlglot/tenth.py", "return 1", "return 2")) == ALL


def test_function_run_by_import_sqlglot_selects_readers_of_changed_state(selector):
    # What boot() built at import differs only in fourth.VALUE: its run-time readers
    # (fifth.py:5, run by B) can see the edit. The fake test modules count as running
    # sqlglot code at import (testimport): their dependents (A, B) join.
    selector._state_diff = lambda: {"changed": ["M sqlglot.fourth VALUE"], "info": {},
                                    "instances": []}
    assert run(selector, edit("sqlglot/tenth.py", "return 1", "return 2")) == {A, B, U}
    selector._test_imports = type("Explained", (), {"explained_for": lambda *a: True})()
    selector._state = False
    assert run(selector, edit("sqlglot/tenth.py", "return 1", "return 2")) == {B, U}


def test_function_run_by_import_sqlglot_unimportable_selects_all(selector):
    selector._state_diff = lambda: None
    assert run(selector, edit("sqlglot/tenth.py", "return 1", "return 2")) == ALL


def test_function_run_while_importing_a_module_selects_its_dependents(selector):
    # fifth.py:5 runs while tests/test_lib.py is imported → that file's tests join line users.
    got = run(selector, edit("sqlglot/fifth.py", "return VALUE\n", "return VALUE + 1\n"))
    assert got == {A, B, U}


def test_function_run_while_importing_a_test_module_ignores_the_state_diff(selector):
    # The state diff imports sqlglot/ only: a test module's import products are unseen.
    selector._state_diff = lambda: {"changed": []}
    got = run(selector, edit("sqlglot/fifth.py", "return VALUE\n", "return VALUE + 1\n"))
    assert got == {A, B, U}


def test_renamed_function_selects_all(selector):
    # Lost binding guard: importers of `helper` fail at import → ALL.
    assert run(selector, edit("sqlglot/sixth.py", "def helper():", "def helper2():")) == ALL


def test_removed_name_imported_during_collection_selects_all(selector):
    # ninth.py imports DERIVED and is imported by tests/test_lib.py: a collection error.
    assert run(selector, edit("sqlglot/eighth.py", "DERIVED = list(BASE_NAMES)\n", "")) == ALL


def test_derived_module_name_follows_importers(selector):
    got = run(selector, edit("sqlglot/eighth.py", "BASE_NAMES = [1]", "BASE_NAMES = [2]"))
    assert got == {A, U}


def test_pure_rename_without_hunks_is_seen(selector):
    text = ("diff --git a/tests/fixtures/q.sql b/tests/fixtures/q2.sql\nsimilarity index 100%\n"
            "rename from tests/fixtures/q.sql\nrename to tests/fixtures/q2.sql\n")
    text += edit("README.md", "hello", "hi")
    assert run(selector, text) == {B, U}


def test_deleted_empty_file_is_seen(selector):
    text = "diff --git a/tests/helpers.py b/tests/helpers.py\ndeleted file mode 100644\nindex e69de29..0000000\n"
    assert run(selector, text + edit("README.md", "hello", "hi")) == ALL


PARSER = '''\
_REGISTRY = []


class Parser:
    TABLE = {"A": 1}

    def connect(self):
        self.TABLE["PRIOR"] = 2
        out = self.parse()
        self.TABLE.pop("PRIOR")
        return out

    def note(self, x):
        _REGISTRY.append(x)
        self.errors.append(x)

    def local(self, x):
        seen = set()
        seen.add(x)
        return seen

    def through_alias(self, x):
        table = self.TABLE
        table[x] = 1
        registry = _REGISTRY
        registry.append(x)
'''


def test_run_time_writes_to_class_tables_are_shared_state():
    import ast

    from selector import classify, select

    tree = ast.parse(PARSER)
    fns = {f.name: f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
    region = lambda name: classify.Region("function", ("Parser", name))  # noqa: E731
    defs = lambda name: select._edited_defs(tree, tree, region(name))  # noqa: E731
    assert select._class_state_writes(defs("connect")) == {"TABLE"}
    assert select._class_state_writes(defs("note")) == set()  # self.errors: per object
    assert classify._writes_shared_state(fns["connect"])
    assert classify._writes_shared_state(fns["note"])  # _REGISTRY.append
    assert not classify._writes_shared_state(fns["local"])
    assert classify._writes_shared_state(fns["through_alias"])  # table = self.TABLE; table[x] = 1


def test_deleting_a_one_line_lambda_entry_changes_the_table():
    import ast

    from selector import classify

    old = 'class P:\n    T = {\n        "A": lambda self: 1,\n        "B": lambda self: 2,\n    }\n'
    new = old.replace('        "A": lambda self: 1,\n', "")
    got = classify.regions(ast.parse(old), ast.parse(new), [3], [])
    assert {r.kind for r in got} == {"function", "class"}, got


# ── R3: repository metadata, root setup.py ──────────────────────────────────

def test_repo_metadata_without_reader_selects_nothing(selector):
    assert run(selector, edit("MANIFEST.in", "LICENSE", "LICENSE README.md")) == set()
    assert run(selector, patch("MANIFEST.in", FILES["MANIFEST.in"], None)) == set()
    assert run(selector, patch(".gitmodules", FILES[".gitmodules"], None)) == set()


def test_repo_metadata_with_a_reader_selects_its_readers(selector):
    assert run(selector, edit(".gitpod.yml", "[]", "[1]")) == {C, U}  # U: untraced


def test_root_setup_py_comment_selects_nothing(selector):
    assert run(selector, edit("setup.py", "\n\nsetup(", "\n\n# build\nsetup(")) == set()


def test_root_setup_py_code_change_or_delete_selects_all(selector):
    assert run(selector, edit("setup.py", 'name="x"', 'name="y"')) == ALL
    assert run(selector, patch("setup.py", FILES["setup.py"], None)) == ALL
    assert run(selector, edit("setup.py", "setup(name", "setup((name")) == ALL  # syntax error


def test_pyproject_pytest_section_still_selects_all(selector):
    assert run(selector, edit("pyproject.toml", '"-q"', '"-x"')) == ALL


# ── R1: run-time verification off (time budget) ─────────────────────────────

def test_degrade_makes_commits_that_need_the_state_diff_all(selector):
    text = edit("sqlglot/tenth.py", "return 1", "return 2")
    selector._state = False
    selector.degrade()
    assert selector.diffexec is selector.subtest is selector.slowverify is selector.genmem is None
    assert run(selector, text) == ALL
    # edits that need no run-time verification are unchanged
    assert run(selector, edit("sqlglot/lib.py", "LIMIT = 10\n", "LIMIT = 10  # cap\n")) == set()


# ── R28 R-DEF: calls that can leave an edited default unpassed ──────────────────────────

def _sites(src, name="f", slots=None):
    import ast as _ast
    from selector.select import _default_call_sites
    return _default_call_sites(_ast.parse(src), name, slots if slots is not None else {"p": 1})


def test_rdef_call_that_passes_the_parameter_is_not_a_site():
    src = "def g(x):\n    f(x, 1)\n    f(x, p=2)\n    return f(\n        x)\n"
    assert _sites(src) == [(4, 5)]


def test_rdef_star_args_and_kwargs_count_as_omitting():
    assert _sites("def g(a, k):\n    f(*a)\n    f(1, **k)\n") == [(2, 2), (3, 3)]


def test_rdef_method_slot_counts_self_conservatively():
    # def f(self, x, p=True): a bound call passing 2 positionals may or may not reach p
    src = "def g(o):\n    o.f(1, 2)\n    o.f(1, 2, 3)\n"
    assert _sites(src, slots={"p": 2}) == [(2, 2)]


def test_rdef_keyword_only_parameter_needs_the_keyword():
    assert _sites("def g():\n    f(1, 2, 3)\n    f(p=1)\n", slots={"p": None}) == [(2, 2)]


def test_rdef_any_other_load_or_alias_falls_back():
    assert _sites("def g(o):\n    h(o.f)\n") is None
    assert _sites("X = {1: f}\n") is None
    assert _sites("from m import f as g2\n") is None
    assert _sites("from m import f\n") == []


def test_rdef_definition_time_call_falls_back_but_lambda_body_is_run_time():
    assert _sites("class C:\n    X = f(1)\n") is None
    assert _sites("class C:\n    X = {1: lambda self: self.f(2)}\n") == [(2, 2)]
    assert _sites("def g(x=f()):\n    pass\n") is None
