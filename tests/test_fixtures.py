"""r27: fixture readers compared through the loader view each one consumes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selector.fixtures import FixtureRule  # noqa: E402

# The pinned loaders (tests/helpers.py at eb11aec), verbatim.
HELPERS = '''\
import os

FILE_DIR = os.path.dirname(__file__)
FIXTURES_DIR = os.path.join(FILE_DIR, "fixtures")


def _filter_comments(s):
    return "\\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _extract_meta(sql):
    meta = {}
    sql_lines = sql.split("\\n")
    i = 0
    while sql_lines[i].startswith("#"):
        key, val = sql_lines[i].split(":", maxsplit=1)
        meta[key.lstrip("#").strip()] = val.strip()
        i += 1
    sql = "\\n".join(sql_lines[i:])
    return sql, meta


def load_sql_fixtures(filename):
    with open(os.path.join(FIXTURES_DIR, filename), encoding="utf-8") as f:
        lines = []
        for line in _filter_comments(f.read()).splitlines():
            lines.append(line)
            if not line.startswith("#"):
                yield "\\n".join(lines)
                lines.clear()


def load_sql_fixture_pairs(filename):
    with open(os.path.join(FIXTURES_DIR, filename), encoding="utf-8") as f:
        statements = _filter_comments(f.read()).split(";")

        size = len(statements)

        for i in range(0, size, 2):
            if i + 1 < size:
                sql = statements[i].strip()
                sql, meta = _extract_meta(sql)
                expected = statements[i + 1].strip()
                yield meta, sql, expected
'''
OPT = '''\
class TestOptimizer:
    def test_simplify(self):
        self.check_file("simplify", simplify, schema=self.schema)

    def test_invalid(self):
        for sql in load_sql_fixtures("optimizer/invalid.sql"):
            pass

    def test_both(self):
        self.check_file("simplify", f)
        for x in load_sql_fixtures("optimizer/simplify.sql"):
            pass
'''
SIMPLIFY = "-- comment\n# title: one\nx AND x;\nx;\n\n# title: two\ny OR y;\ny;\n"
TPCH = "-- TPC-H 1\nselect a from t;\nSELECT \"t\".\"a\" FROM \"t\";\n" \
       "# execute: true\nselect b from t;\nSELECT \"t\".\"b\" FROM \"t\";\n"
S = "tests/fixtures/optimizer/simplify.sql"
H = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
O = "tests/test_optimizer.py::TestOptimizer::"
E = "tests/test_executor.py::TestExecutor::"
EXEC = [E + "test_execute_tpch", E + "test_optimized_tpch", E + "test_tables", E + "test_execute_tpcds",
        O + "test_tpch"]


@pytest.fixture
def rule(tmp_path: Path) -> FixtureRule:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "helpers.py").write_text(HELPERS)
    (tmp_path / "tests" / "test_optimizer.py").write_text(OPT + '''
    def test_tpch(self):
        self.check_file("tpc-h/tpc-h", optimize)
''')
    r = FixtureRule(tmp_path)
    r._parses = lambda sqls: all("((" not in s for s in sqls)  # no sqlglot here
    return r


def narrow(rule, path, old, new, readers, pinned=True):
    return rule.narrow(path, old.encode(), new.encode(), readers, pinned)[0]


def test_comment_and_blank_lines_drop_every_reader(rule):
    new = SIMPLIFY.replace("-- comment", "-- other comment") + "\n\n-- tail\n"
    assert narrow(rule, S, SIMPLIFY, new, [O + "test_simplify"]) == set()


def test_title_only_drops_check_file_reader(rule):
    new = SIMPLIFY.replace("# title: one", "# title: uno")
    assert narrow(rule, S, SIMPLIFY, new, [O + "test_simplify"]) == set()


def test_expected_or_meta_edit_keeps_reader(rule):
    assert narrow(rule, S, SIMPLIFY, SIMPLIFY.replace("\nx;", "\nx 1;"), [O + "test_simplify"]) \
        == {O + "test_simplify"}
    assert narrow(rule, S, SIMPLIFY, SIMPLIFY.replace("# title: two", "# dialect: duckdb"),
                  [O + "test_simplify"]) == {O + "test_simplify"}


def test_leading_space_comment_is_not_filtered(rule):
    new = SIMPLIFY.replace("# title: one", "  -- x\n# title: one")
    assert narrow(rule, S, SIMPLIFY, new, [O + "test_simplify"]) == {O + "test_simplify"}


def test_unknown_or_ambiguous_reader_kept(rule):
    new = SIMPLIFY.replace("-- comment", "-- c2")
    assert narrow(rule, S, SIMPLIFY, new, [O + "test_both", "tests/test_x.py::T::test_y"]) == \
        {O + "test_both", "tests/test_x.py::T::test_y"}


def test_load_error_keeps_every_reader(rule):
    new = TPCH.replace("select a from t;", "# no colon\nselect a from t;")
    assert narrow(rule, H, TPCH, new, EXEC) == set(EXEC)


def test_tpch_expected_edit_keeps_only_its_consumers(rule):
    new = TPCH.replace('SELECT "t"."a" FROM "t"', 'SELECT "t"."a" AS "z" FROM "t"')
    assert narrow(rule, H, TPCH, new, EXEC) == {E + "test_optimized_tpch", O + "test_tpch"}


def test_tpch_input_edit_and_parse_check(rule):
    parses = TPCH.replace("select a from t", "select a + 1 from t")
    assert narrow(rule, H, TPCH, parses, EXEC) == {E + "test_optimized_tpch", O + "test_tpch"}
    broken = TPCH.replace("select a from t", "select ((a from t")
    assert narrow(rule, H, TPCH, broken, EXEC) == {E + "test_execute_tpch", E + "test_optimized_tpch",
                                                     O + "test_tpch"}
    # code edits in the commit: no parse check
    assert E + "test_execute_tpch" in narrow(rule, H, TPCH, parses, EXEC, pinned=False)
    # executed query: never cleared by parsing
    executed = TPCH.replace("select b from t", "select b + 1 from t")
    assert E + "test_execute_tpch" in narrow(rule, H, TPCH, executed, EXEC)


def test_pair_shift_keeps_consumers_drops_setupclass_only(rule):
    new = TPCH.replace('FROM "t";\n#', 'FROM "t";\n;\n#')
    assert narrow(rule, H, TPCH, new, EXEC) == {E + "test_execute_tpch", E + "test_optimized_tpch",
                                                  O + "test_tpch"}


def test_crlf_and_bad_utf8(rule):
    assert narrow(rule, S, SIMPLIFY, SIMPLIFY.replace("\n", "\r\n"), [O + "test_simplify"]) == set()
    got = rule.narrow(S, SIMPLIFY.encode(), SIMPLIFY.encode() + b"\xff", [O + "test_simplify"], True)[0]
    assert got == {O + "test_simplify"}
