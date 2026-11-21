"""Test modules whose import runs no sqlglot code of their own (selector/testimport.py, I25).
Run: python -m pytest tests/
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selector.testimport import Checker  # noqa: E402

LIB = {
    "sqlglot/__init__.py": "from sqlglot import expressions as exp\n",
    "sqlglot/expressions.py": "class Expr:\n    pass\n",
    "sqlglot/dialects/__init__.py": (
        "import importlib\n\nMODULES = {'Duck': 'duck'}\n\n\n"
        "def __getattr__(name):\n"
        "    module = importlib.import_module(f'sqlglot.dialects.{MODULES.get(name)}')\n"
        "    return getattr(module, name)\n"),
    "sqlglot/dialects/duck.py": "class Duck:\n    pass\n",
    "tests/__init__.py": "",
    "tests/helpers.py": (
        "import os\n\n\n"
        "def string_to_bool(s):\n"
        "    return s.lower() in ('1', 'true')\n\n\n"
        "SKIP = string_to_bool(os.environ.get('SKIP', '0'))\n"
        "FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')\n"),
}

TESTS = {
    "plain": (
        "import unittest\n"
        "from unittest import mock\n"
        "from sqlglot import exp\n"
        "from sqlglot.dialects import Duck\n"
        "from tests.helpers import SKIP, FIXTURES\n\n"
        "DIR = FIXTURES + '/x'\n\n\n"
        "class Validator(unittest.TestCase):\n"
        "    dialect = 'duck'\n\n"
        "    @unittest.skipIf(SKIP, 'skip')\n"
        "    @mock.patch('sqlglot.dialects.duck.Duck')\n"
        "    def test_a(self, m, e: exp.Expr = None):\n"
        "        exp.Expr()\n\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n"),
    "calls_sqlglot": "from sqlglot import exp\n\nX = exp.Expr()\n",
    "subclasses_sqlglot": "from sqlglot import exp\n\n\nclass E(exp.Expr):\n    pass\n",
    "hashes_sqlglot": "from sqlglot import exp\n\nS = {exp.Expr}\n",
    "imports_bad": "from tests.test_calls_sqlglot import X\n",
    "loop": "import os\n\nfor x in os.environ:\n    pass\n",
}


def make(tmp_path: Path) -> Checker:
    for path, text in LIB.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    for name, text in TESTS.items():
        (tmp_path / f"tests/test_{name}.py").write_text(text)
    return Checker(tmp_path)


def test_imports_definitions_and_harmless_calls_are_explained(tmp_path):
    c = make(tmp_path)
    assert c.explained("tests.test_plain")
    assert c.explained_for("tests.test_plain", "sqlglot/expressions.py", 1, 2)


def test_sqlglot_code_at_import_is_not_explained(tmp_path):
    c = make(tmp_path)
    for name in ("calls_sqlglot", "subclasses_sqlglot", "hashes_sqlglot", "imports_bad", "loop"):
        assert not c.explained(f"tests.test_{name}"), name


def test_edit_in_a_package_getattr_is_not_explained(tmp_path):
    # `from sqlglot.dialects import Duck` calls the package's __getattr__, which the
    # import-state snapshot never runs: an edit there is seen only by importing tests.
    c = make(tmp_path)
    assert not c.explained_for("tests.test_plain", "sqlglot/dialects/__init__.py", 7, 7)
