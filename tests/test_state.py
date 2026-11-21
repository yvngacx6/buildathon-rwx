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
        return klass


class Dialect(metaclass=_Meta):
    KEYWORDS = {"A": 1}
    PARTS = (1, 2)
    ESC = "x"
    CACHED = 1

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

    def cached(self):
        if type(self) not in _CACHE:
            _CACHE[type(self)] = build(type(self))
        return _CACHE[type(self)]


class TSQL(Dialect):
    KEYWORDS = {"A": 1, "BB": 2}
    PARTS = (3, 4)
    ESC = "y"
    CACHED = 2


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
FILES = {"sqlglot/__init__.py": "", "sqlglot/dialect.py": DIALECT, "tests/test_a.py": TEST_A}
T = [f"tests/test_a.py::TestA::test_{i}" for i in range(8)]
TRIE, TRIE_OTHER, PARTS, PARTS_USE, CORE, ESC, KW, BUILD = range(8)
D = "sqlglot/dialect.py"
TSQL_USERS = [TRIE, PARTS, PARTS_USE, CORE, ESC, KW, BUILD]


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
    }
    classes = {"sqlglot.dialect.TSQL": TSQL_USERS,
               "sqlglot.dialect.Dialect": list(range(8))}
    maps.mkdir()
    for name, mapping in {"lines": lines, "classes": classes, "reads": {},
                          "test_imports": {}}.items():
        body = {"tests": T, "map": mapping}
        (maps / f"{name}.json.gz").write_bytes(gzip.compress(json.dumps(body).encode()))
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


def test_snapshot_of_other_source_is_not_used(repo, tmp_path, monkeypatch):
    root, snapshot = repo
    other = tmp_path / "sqlglot"
    (other / "sqlglot").mkdir(parents=True)
    (other / D).write_text(DIALECT + "\n# changed size\n")
    (other / "sqlglot/__init__.py").write_text("")
    assert state.diff(other, snapshot, {}) is None
    assert state.diff(root, snapshot, {}) == {"changed": [], "info": {}, "instances": [],
                                              "holders": {}}


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
