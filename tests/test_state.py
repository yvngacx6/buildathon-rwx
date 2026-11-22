"""Narrowing class-creation edits by what they change in import-time state (selector/state.py,
idea I22), on a tiny fake sqlglot. Run: python -m pytest tests/
"""

from __future__ import annotations

import difflib
import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selector import select, state  # noqa: E402
from selector.select import Selector  # noqa: E402

DIALECT = '''\
_CACHE = {}


class _Meta(type):
    def __new__(cls, name, bases, attrs):
        klass = super().__new__(cls, name, bases, attrs)
        klass.TRIE = {k: len(k) for k in klass.KEYWORDS}  # derived at class creation
        klass.MARK = "A" in klass.KEYWORDS  # overwrites whatever the body set
        return klass


class Dialect(metaclass=_Meta):
    KEYWORDS = {"A": 1}
    PARTS = (1, 2)
    ESC = "x"
    CACHED = 1
    OPTS = {"x": 1}

    def trie(self):
        return self.TRIE  # @trie

    def keywords(self):
        return self.KEYWORDS  # @keywords

    def parts(self):
        parts = self.PARTS  # @parts
        size = 0  # @between
        return [p for p in parts]  # @parts_use

    def core(self):
        return Core(esc=self.ESC)  # @core

    def has_x(self):
        return "x" in self.OPTS  # @opts_keys

    def any_opts(self):
        if self.OPTS:  # @opts_truth
            return 1

    def opt_x(self):
        return self.OPTS["x"]  # @opts_full

    def cached(self):
        if type(self) not in _CACHE:
            _CACHE[type(self)] = build(type(self))
        return _CACHE[type(self)]


class TSQL(Dialect):
    KEYWORDS = {"A": 1, "BB": 2}
    PARTS = (3, 4)
    ESC = "y"
    CACHED = 2
    OPTS = {"x": 2}
    MARK = 5


class Core:
    def __init__(self, esc):
        self.esc = esc

    def scan(self):
        return self.esc  # @esc


def build(cls):
    return cls.CACHED  # @build


class Other:
    ESC = "o"

    def esc(self):
        return self.ESC  # @other_esc
'''
TEST_A = '''\
from sqlglot.dialect import build


def test_build():
    assert build
'''
BASE = '''\
class Base:
    TABLE = {"a": 1, "b": 2}

    def get(self, key):
        return self.TABLE.get(key)  # @table
'''
CHILD = '''\
from sqlglot.base import Base


class Child(Base):
    TABLE = {**Base.TABLE}
    TABLE.pop("a")
'''
CHILD2 = '''\
from sqlglot.base import Base


class Child2(Base):
    TABLE = {**Base.TABLE, "c": 3}
    TABLE.pop("a")
'''
FILES = {"sqlglot/__init__.py": "", "sqlglot/dialect.py": DIALECT, "sqlglot/base.py": BASE,
         "sqlglot/child.py": CHILD, "sqlglot/child2.py": CHILD2, "tests/test_a.py": TEST_A}
T = [f"tests/test_a.py::TestA::test_{i}" for i in range(11)]
TRIE, TRIE_OTHER, PARTS, PARTS_USE, CORE, ESC, KW, BUILD, OPT_KEYS, OPT_TRUTH, OPT_FULL = range(11)
D = "sqlglot/dialect.py"
TSQL_USERS = [TRIE, PARTS, PARTS_USE, CORE, ESC, KW, BUILD, OPT_KEYS, OPT_TRUTH, OPT_FULL]


def line_of(marker: str) -> int:
    return next(i for i, row in enumerate(DIALECT.splitlines(), 1) if marker in row)


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("fake")
    repo, maps = root / "sqlglot", root / "maps"
    for path, text in FILES.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    lines = {
        f"{D}:{line_of('@trie')}": [TRIE, TRIE_OTHER],
        f"{D}:{line_of('@keywords')}": [KW],
        f"{D}:{line_of('@parts')}": [PARTS, PARTS_USE],
        f"{D}:{line_of('@between')}": [PARTS, PARTS_USE],
        f"{D}:{line_of('@parts_use')}": [PARTS_USE],
        f"{D}:{line_of('@core')}": [CORE, ESC],
        f"{D}:{line_of('@esc')}": [ESC],
        f"{D}:{line_of('@build')}": [BUILD],
        f"{D}:{line_of('@other_esc')}": [KW],  # a TSQL holder that reads Other.ESC
        f"{D}:{line_of('@opts_keys')}": [OPT_KEYS],
        f"{D}:{line_of('@opts_truth')}": [OPT_TRUTH],
        f"{D}:{line_of('@opts_full')}": [OPT_FULL],
    }
    classes = {"sqlglot.dialect.TSQL": TSQL_USERS,
               "sqlglot.dialect.Dialect": list(range(len(T)))}
    maps.mkdir()
    for name, mapping in {"lines": lines, "classes": classes, "reads": {},
                          "test_imports": {}}.items():
        body = {"tests": T, "map": mapping}
        (maps / f"{name}.json.gz").write_bytes(gzip.compress(json.dumps(body).encode()))
    body = {"tests": [], "map": {}}  # no line runs at import
    (maps / "import_time.json.gz").write_bytes(gzip.compress(json.dumps(body).encode()))
    snapshot = root / "state.pkl"
    assert state.build(repo, snapshot, samples=2)
    return repo, snapshot


@pytest.fixture
def selector(repo, monkeypatch) -> Selector:
    root, snapshot = repo
    monkeypatch.setenv("BR_STATE", str(snapshot))
    monkeypatch.setattr(select, "STATE_MIN_GAIN", -10.0)
    # the fake's hierarchy stands in for one the classes map records
    monkeypatch.setattr(select, "TRACKED_ROOTS", {"sqlglot.dialect:Dialect"})
    tests = [{"number": i, "id": t, "file": t.split("::")[0], "seconds": 1.0}
             for i, t in enumerate(T)]
    return Selector(tests, root, root.parent / "maps")


def run(selector: Selector, before: str, after: str) -> tuple[set[int], str]:
    old = DIALECT
    assert before in old
    new = old.replace(before, after, 1)
    text = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"a/{D}", f"b/{D}"))
    return selector.select(text)


def test_metaclass_derived_value_selects_its_readers_among_class_users(selector):
    # KEYWORDS → TRIE (metaclass): TRIE's reader ran in TRIE and TRIE_OTHER, but only TRIE
    # holds a TSQL; KEYWORDS' own reader (KW) also stays.
    got, why = run(selector, '"A": 1, "BB": 2}', '"A": 1, "BB": 3, "CC": 4}')
    assert got == {TRIE, KW}, why
    assert "state:" in why


def test_value_bound_to_a_local_is_observed_where_the_local_is_loaded(selector):
    # PARTS is read into a local; only PARTS_USE ran the line that loads it.
    got, why = run(selector, "PARTS = (3, 4)", "PARTS = (3, int(5))")
    assert got == {PARTS_USE}, why


def test_value_stored_by_a_constructor_is_observed_where_the_attribute_is_read(selector):
    got, why = run(selector, 'ESC = "y"', 'ESC = str("z")')
    assert got == {ESC}, why


def test_cached_reader_falls_back(selector):
    got, why = run(selector, "CACHED = 2", "CACHED = int(3)")
    assert "cached across tests" in why
    assert got >= set(TSQL_USERS)


def test_import_error_falls_back(selector):
    got, why = run(selector, "CACHED = 2", "CACHED = int('x')")
    assert "import-state diff unavailable" in why
    assert got >= set(TSQL_USERS)


def test_import_error_is_no_diff_for_every_consumer(repo, selector):
    # The subprocess reports which file broke the import; every consumer (`_by_state`, the
    # import-time "state unchanged" rule, `_foreign_state`) sees no diff, never "no change".
    root, snapshot = repo
    new = DIALECT.replace("CACHED = 2", "CACHED = int('x')")
    assert state.diff(root, snapshot, {D: new}) == {"import_error": D}
    selector._files = select.diff.parse("".join(difflib.unified_diff(
        DIALECT.splitlines(True), new.splitlines(True), f"a/{D}", f"b/{D}")))
    selector._state, selector._state_error = False, None
    assert selector._state_diff() is None
    assert selector._state_error == D


def test_snapshot_of_other_source_is_not_used(repo, tmp_path, monkeypatch):
    root, snapshot = repo
    other = tmp_path / "sqlglot"
    (other / "sqlglot").mkdir(parents=True)
    (other / D).write_text(DIALECT + "\n# changed size\n")
    (other / "sqlglot/__init__.py").write_text("")
    assert state.diff(other, snapshot, {}) is None
    assert state.diff(root, snapshot, {}) == {"changed": [], "same": {}, "digests": {},
                                              "info": {},
                                              "instances": [], "holders": {}, "keys": {}}


@pytest.mark.parametrize("after, expected", [
    ('OPTS = dict(x=3)', {OPT_FULL}),  # same keys, same truth (a call: state diff)
    ('OPTS = dict(y=2)', {OPT_FULL, OPT_KEYS}),  # same truth only
    ("OPTS = dict()", {OPT_FULL, OPT_KEYS, OPT_TRUTH}),
])
def test_reads_of_truth_or_keys_only_see_what_changed(selector, after, expected):
    got, why = run(selector, 'OPTS = {"x": 2}', after)
    assert got == expected, why


def test_edit_overwritten_at_class_creation_selects_nothing(selector):
    # the metaclass overwrites MARK: the patched import builds the same state
    got, why = run(selector, "MARK = 5", "MARK = 6")
    assert got == set(), why
    assert "identical state" in why


def test_diff_reports_derived_values(repo):
    root, snapshot = repo
    new = DIALECT.replace('"A": 1, "BB": 2}', '"A": 1, "BB": 3, "CC": 4}')
    got = state.diff(root, snapshot, {D: new})
    assert got["changed"] == ["C sqlglot.dialect:TSQL KEYWORDS", "C sqlglot.dialect:TSQL TRIE"]


# ── removed / renamed bindings (idea I26) ────────────────────────────────────────────────


def test_renamed_method_selects_the_tests_that_ran_it(selector):
    got, why = run(selector, "def keywords(self):", "def keywords2(self):")
    assert got == {KW}, why
    assert "removed Dialect.keywords" in why


def test_removed_class_attribute_selects_readers_of_the_inherited_value(selector):
    # TSQL.ESC removed: TSQL objects now read Dialect.ESC through Core(esc=self.ESC).
    # `self.ESC` in the unrelated class Other (a line KW ran) cannot reach it.
    got, why = run(selector, '    ESC = "y"\n    CACHED = 2', "    CACHED = 2")
    assert got == {ESC}, why
    assert "removed TSQL.ESC" in why


def test_removed_name_imported_by_a_test_module_selects_all(selector):
    got, why = run(selector, "def build(cls):", "def build2(cls):")
    assert got == selector.all_numbers(), why
    assert "imports build at collection" in why


def test_removed_name_read_in_cached_code_falls_back(selector):
    got, why = run(selector, "    CACHED = 2\n", "")
    assert "cached across tests" in why
    assert got >= set(TSQL_USERS)


def test_removal_with_a_changed_class_head_is_not_narrowed(selector):
    # The snapshot does not compare bases: Other's attributes now shadow for TSQL objects.
    old = DIALECT
    new = old.replace("def keywords(self):", "def keywords2(self):", 1).replace(
        "class TSQL(Dialect):", "class TSQL(Dialect, Other):", 1)
    text = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"a/{D}", f"b/{D}"))
    got, why = selector.select(text)
    assert "bases / keywords / decorators changed" in why, why
    assert got == selector.all_numbers()


def run_file(selector: Selector, path: str, old: str, before: str, after: str):
    assert old.count(before) == 1
    new = old.replace(before, after, 1)
    return selector.select("".join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}")))


def test_alias_mutating_another_modules_dict_is_all(selector):
    # Child.TABLE now IS Base.TABLE: the pop in Child's body removes "a" from Base's dict, so
    # every test that runs after sqlglot.child is imported sees it, users of Child or not.
    # Here Child2 (imported after Child) then copies Base.TABLE without "a" and its own
    # pop("a") fails: the edit breaks the import of another module, also ALL.
    got, why = run_file(selector, "sqlglot/child.py", CHILD, "TABLE = {**Base.TABLE}",
                        "TABLE = Base.TABLE")
    assert got == set(range(len(T))), why
    assert "breaks the import of sqlglot/child2.py" in why


def test_alias_that_also_changes_the_own_value_is_all(selector):
    # Child2.TABLE changes too, and its new value equals Base.TABLE's new value; Base.TABLE
    # is still not a copy of it (they differed before the edit).
    got, why = run_file(selector, "sqlglot/child2.py", CHILD2, 'TABLE = {**Base.TABLE, "c": 3}',
                        "TABLE = Base.TABLE")
    assert got == set(range(len(T))), why
    assert "outside the patched modules" in why


def test_mutation_of_own_copy_is_not_all(selector):
    got, why = run_file(selector, "sqlglot/child.py", CHILD, 'TABLE.pop("a")', 'TABLE.pop("b")')
    assert "outside the patched modules" not in why


@pytest.mark.parametrize("out", [None, {"import_error": None}])
def test_foreign_state_without_a_diff_is_all(selector, monkeypatch, out):
    # The alias edit, but the import-state diff fails (timeout, crash, missing snapshot, an
    # import error in no known package file): nothing rules the global effect out → ALL,
    # never a silent pass.
    monkeypatch.setattr(state, "diff", lambda *a, **k: out)
    got, why = run_file(selector, "sqlglot/child2.py", CHILD2, 'TABLE = {**Base.TABLE, "c": 3}',
                        "TABLE = Base.TABLE")
    assert got == set(range(len(T))), why
    assert "import-state diff unavailable" in why
