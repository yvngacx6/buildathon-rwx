"""r34: a literal-default edit (R-DEF) of a def that also runs at import. When the
import-state diff shows only the def's own entry changed (its `__defaults__`), what the
import-time runs built is unchanged: no import-time dependents. When an import-time product
changed, its readers stay. Run: python -m pytest tests/
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

# the import-time call passes the parameter: its product does not depend on the default
MOD = '''\
def make(n=1):
    return n * 2  # @make


TABLE = {"k": make(n=1)}


def use():
    return make()  # @use


def other():
    return 0  # @other
'''
# the import-time call leaves the parameter out: TABLE2 changes with the default
MOD2 = '''\
def make2(n=1):
    return n * 2  # @make2


TABLE2 = {"k": make2()}


def read():
    return TABLE2["k"]  # @read


def use2():
    return make2(n=3)  # @use2


def other2():
    return 0  # @other2
'''
FILES = {"sqlglot/__init__.py": "", "sqlglot/mod.py": MOD, "sqlglot/mod2.py": MOD2}
T = [f"tests/test_m.py::TestM::test_{i}" for i in range(5)]
USE, OTHER, READ, USE2, OTHER2 = range(5)


def at(text: str, marker: str) -> int:
    return next(i for i, row in enumerate(text.splitlines(), 1) if marker in row)


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("rdef")
    repo, maps = root / "sqlglot", root / "maps"
    for path, text in FILES.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    m, m2 = "sqlglot/mod.py", "sqlglot/mod2.py"
    lines = {
        f"{m}:{at(MOD, '@make')}": [USE], f"{m}:{at(MOD, '@use')}": [USE],
        f"{m}:{at(MOD, '@other')}": [OTHER],
        f"{m2}:{at(MOD2, '@make2')}": [USE2], f"{m2}:{at(MOD2, '@use2')}": [USE2],
        f"{m2}:{at(MOD2, '@read')}": [READ], f"{m2}:{at(MOD2, '@other2')}": [OTHER2],
    }
    maps.mkdir()
    for name, mapping in {"lines": lines, "classes": {}, "reads": {},
                          "test_imports": {}}.items():
        body = {"tests": T, "map": mapping}
        (maps / f"{name}.json.gz").write_bytes(gzip.compress(json.dumps(body).encode()))
    body = {"tests": ["sqlglot.mod", "sqlglot.mod2"],
            "map": {f"{m}:{at(MOD, '@make')}": [0], f"{m2}:{at(MOD2, '@make2')}": [1]}}
    (maps / "import_time.json.gz").write_bytes(gzip.compress(json.dumps(body).encode()))
    snapshot = root / "state.pkl"
    assert state.build(repo, snapshot, samples=2)
    return repo, snapshot


@pytest.fixture
def selector(repo, monkeypatch, tmp_path) -> Selector:
    root, snapshot = repo
    monkeypatch.setenv("BR_STATE", str(snapshot))
    monkeypatch.setenv("BR_INDEX", str(tmp_path / "none.pkl"))  # build the index in memory
    monkeypatch.setenv("BR_MAPS_COMPILED", str(tmp_path / "none"))
    monkeypatch.setattr(select, "STATE_MIN_GAIN", -10.0)
    tests = [{"number": i, "id": t, "file": t.split("::")[0], "seconds": 1.0}
             for i, t in enumerate(T)]
    return Selector(tests, root, root.parent / "maps")


def run(selector: Selector, path: str, old: str, before: str, after: str):
    assert before in old
    new = old.replace(before, after, 1)
    text = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"a/{path}", f"b/{path}"))
    return selector.select(text)


def test_default_edit_with_unchanged_import_products_adds_no_dependents(selector):
    got, why = run(selector, "sqlglot/mod.py", MOD, "def make(n=1):", "def make(n=2):")
    assert got == {USE}, why  # not OTHER, a dependent of sqlglot.mod
    assert "import state unchanged" in why


def test_default_edit_that_changes_an_import_product_keeps_its_readers(selector):
    got, why = run(selector, "sqlglot/mod2.py", MOD2, "def make2(n=1):", "def make2(n=2):")
    assert READ in got, why
