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
HELPERS = "def load():\n    return 1\n"
FIXTURE = "# meta: x\nSELECT 1;\n"

FILES = {
    "sqlglot/lib.py": LIB,
    "sqlglot/other.py": OTHER,
    "sqlglot/parser.py": BASE,
    "sqlglot/third.py": "from sqlglot.other import SHARED\n",
    "sqlglot/fourth.py": "VALUE = 1\n",
    "sqlglot/fifth.py": "from sqlglot.fourth import VALUE\n\n\ndef f():\n    return VALUE\n",
    "tests/conftest.py": "import pytest\n",
    "tests/test_lib.py": TEST,
    "tests/helpers.py": HELPERS,
    "tests/fixtures/q.sql": FIXTURE,
    "tests/fixtures/unread.sql": FIXTURE,
    "pyproject.toml": '[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n',
    "README.md": "hello\n",
}

T = ["tests/test_lib.py::TestLib::test_a", "tests/test_lib.py::TestLib::test_b",
     "tests/test_other.py::TestOther::test_c", "tests/test_other.py::TestOther::test_d",
     "tests/test_new.py::TestNew::test_untraced"]
A, B, C, D, U = range(5)
ALL = set(range(5))

MAPS = {
    "lines": {"sqlglot/lib.py:10": [C], "sqlglot/lib.py:13": [C], "sqlglot/lib.py:14": [D],
              "sqlglot/other.py:4": [A], "sqlglot/parser.py:5": [B],
              "sqlglot/fifth.py:5": [B]},
    "classes": {"sqlglot.lib.Dialect": [A]},
    "reads": {"tests/fixtures/q.sql": [B]},
    "test_imports": {"tests/helpers.py": [A, B]},
}


@pytest.fixture
def selector(tmp_path: Path) -> Selector:
    repo, maps = tmp_path / "sqlglot", tmp_path / "maps"
    for path, text in FILES.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    maps.mkdir()
    for name, mapping in MAPS.items():
        traced = T[:4]  # test U appears in no map
        body = {"tests": traced, "map": mapping}
        (maps / f"{name}.json.gz").write_bytes(gzip.compress(json.dumps(body).encode()))
    tests = [{"number": i, "id": t, "file": t.split("::")[0], "seconds": 1.0} for i, t in enumerate(T)]
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


def test_base_class_body_selects_all(selector):
    assert run(selector, edit("sqlglot/parser.py", "STRICT = False", "STRICT = True")) == ALL


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


def test_test_helper_selects_importers(selector):
    assert run(selector, edit("tests/helpers.py", "return 1", "return 2")) == {A, B, U}


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


def _with_imports(selector: Selector, tmp_path: Path, mapping: dict) -> Selector:
    body = {"tests": T[:4], "map": mapping}  # U is untraced: always added
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
    assert run(bounded, edit("sqlglot/parser.py", "STRICT = False", "STRICT = True")) == ALL


def test_hunk_that_does_not_apply_still_selects_all(bounded):
    assert run(bounded, patch("sqlglot/lib.py", "nothing like the file\n", "x\n")) == ALL


def test_removed_binding_selects_tests_that_load_the_module(bounded):
    # Dialect is imported by other.py → the removal is visible → import bound of lib.py
    assert run(bounded, edit("sqlglot/lib.py", "class Dialect:", "class Dialect2:")) == {A, C, D, U}


def test_unreferenced_removed_function_uses_normal_rules(bounded):
    old = FILES["sqlglot/fifth.py"]
    new = old.replace("def f():", "def g():")  # nothing else names f
    assert run(bounded, patch("sqlglot/fifth.py", old, new)) == {B, U}


def test_module_nothing_loads_selects_only_unknown_tests(bounded):
    # only the untraced test U, which the map cannot vouch for
    assert run(bounded, patch("sqlglot/third.py", FILES["sqlglot/third.py"], None)) == {U}


def test_isolation_failures_join_the_bound(selector, tmp_path):
    sel = _with_imports(selector, tmp_path, {**IMPORTS, "<isolation-failure>": [D]})
    assert run(sel, patch("sqlglot/other.py", OTHER, None)) == {A, D, U}


def test_test_file_syntax_error_selects_its_loaders(bounded):
    assert run(bounded, edit("tests/test_lib.py", "assert True", "assert True +")) == {A, B, U}
