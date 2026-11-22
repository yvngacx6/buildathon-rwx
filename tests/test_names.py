"""Narrowing class-attribute / module-constant edits by name (selector/names.py, idea I08).

A tiny fake sqlglot; the line map is built from markers in the source, so the tests do not
depend on exact line numbers. Run: python -m pytest tests/
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

DIALECT = '''\
from sqlglot.helper import cached, register

_CACHE = {}
LIMIT = 3


class _Meta(type):
    def __new__(cls, name, bases, attrs):
        klass = super().__new__(cls, name, bases, attrs)
        klass.DERIVED = klass.META_READ  # metaclass derives a setting
        return klass


class Dialect(metaclass=_Meta):
    WEEK = 0
    META_READ = 1
    CACHED = 2
    UNREAD = 3
    CALLED = 4
    TABLE = {
        "A": lambda args: make(
            args,
        ),
        "B": 1,
    }

    def week(self):
        return self.WEEK  # @week

    def limit(self):
        return LIMIT  # @limit

    def table(self, key):
        return self.TABLE[key]  # @table

    def dyn(self, name):
        return getattr(self, f"{name}_sql")  # @dyn


class TSQL(Dialect):
    WEEK = -1
    COPY = {**Dialect.TABLE}
    SUM = Dialect.WEEK + 1
    ABS = abs(Dialect.CALLED)


def make(args):
    return args


def build_cache(cls):
    return cls.CACHED  # @cached


def get(cls):
    if cls not in _CACHE:
        _CACHE[cls] = build_cache(cls)
    return _CACHE[cls]
'''
HELPER = "def cached(f):\n    return f\n\n\ndef register(c):\n    c.X = 1\n    return c\n"
USER = "from sqlglot.dialect import LIMIT as CAP\n\n\ndef cap():\n    return CAP  # @cap\n"
TEST = '''\
from sqlglot.dialect import TSQL


class TestTSQL:
    def test_week(self):
        assert TSQL.WEEK == -1  # @test_week

    def test_other(self):
        assert True
'''

FILES = {
    "sqlglot/dialect.py": DIALECT,
    "sqlglot/helper.py": HELPER,
    "sqlglot/user.py": USER,
    "tests/test_tsql.py": TEST,
    "sqlglot/consts.py": "K = 1\n",
    "sqlglot/stars.py": "from sqlglot.consts import *\n\n\ndef f():\n    return K\n",
}
T = ["tests/test_a.py::TestA::test_week", "tests/test_a.py::TestA::test_limit",
     "tests/test_a.py::TestA::test_table", "tests/test_a.py::TestA::test_cached",
     "tests/test_b.py::TestB::test_slow", "tests/test_tsql.py::TestTSQL::test_week",
     "tests/test_tsql.py::TestTSQL::test_other", "tests/test_a.py::TestA::test_cap",
     "tests/test_a.py::TestA::test_lambda"]
WEEK, LIMIT_T, TABLE, CACHED, SLOW, TW, TO, CAP, LAM = range(9)
EVERYONE = set(range(9))


def line_of(text: str, marker: str) -> int:
    return next(i for i, row in enumerate(text.splitlines(), 1) if marker in row)


def build_maps() -> dict:
    d = "sqlglot/dialect.py"
    lines = {
        f"{d}:{line_of(DIALECT, '@week')}": [WEEK],
        f"{d}:{line_of(DIALECT, '@limit')}": [LIMIT_T],
        f"{d}:{line_of(DIALECT, '@table')}": [TABLE],
        f"{d}:{line_of(DIALECT, '@cached')}": [CACHED],
        f"{d}:{line_of(DIALECT, '@dyn')}": [SLOW],
        f"{d}:{line_of(DIALECT, 'lambda args: make(')}": [SLOW],  # created at import
        f"{d}:{line_of(DIALECT, '            args,')}": [LAM],  # the call itself
        f"{d}:{line_of(DIALECT, 'class Dialect(')}": [SLOW, WEEK, LIMIT_T, TABLE, CACHED, LAM],
        f"sqlglot/user.py:{line_of(USER, '@cap')}": [CAP],
        f"tests/test_tsql.py:{line_of(TEST, '@test_week')}": [TW],
    }
    return {"lines": lines,
            "classes": {"sqlglot.dialect.TSQL": [WEEK, TABLE], "sqlglot.dialect.Dialect":
                        [WEEK, LIMIT_T, TABLE, CACHED, SLOW, LAM]},
            "reads": {}, "test_imports": {}, "import_time": {}}


@pytest.fixture
def selector(tmp_path: Path) -> Selector:
    repo, maps = tmp_path / "sqlglot", tmp_path / "maps"
    for path, text in FILES.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    maps.mkdir()
    for name, mapping in build_maps().items():
        body = {"tests": T, "map": mapping}
        (maps / f"{name}.json.gz").write_bytes(gzip.compress(json.dumps(body).encode()))
    tests = [{"number": i, "id": t, "file": t.split("::")[0],
              "seconds": 9.0 if i == SLOW else 1.0} for i, t in enumerate(T)]
    return Selector(tests, repo, maps)


def run(selector: Selector, path: str, before: str, after: str) -> set[int]:
    old = FILES[path]
    assert before in old
    new = old.replace(before, after, 1)
    text = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"a/{path}", f"b/{path}"))
    return selector.select(text)[0]


D = "sqlglot/dialect.py"
CONSERVATIVE_TSQL = {WEEK, LIMIT_T, TABLE, CACHED, SLOW, LAM}  # class users ∪ ran the file


def test_class_attribute_selects_tests_that_read_it(selector):
    # self.WEEK in Dialect.week (test WEEK) and TSQL.WEEK in test_tsql (test TW).
    # The getattr with an f"{x}_sql" name cannot produce "WEEK", so test SLOW stays out.
    assert run(selector, D, "    WEEK = -1", "    WEEK = -2") == {WEEK, TW}


def test_test_file_reads_join_without_intersection(selector):
    # TW is not a user of TSQL in the class map, but its own code reads TSQL.WEEK.
    assert TW in run(selector, D, "    WEEK = -1", "    WEEK = -2")


def test_value_that_runs_code_at_import_falls_back(selector):
    got = run(selector, D, "    WEEK = -1", "    WEEK = make(-2)")
    assert got == CONSERVATIVE_TSQL


def test_metaclass_read_falls_back(selector):
    assert run(selector, D, "META_READ = 1", "META_READ = 2") == CONSERVATIVE_TSQL


def test_memoized_read_falls_back(selector):
    # build_cache's result is stored in the module-level _CACHE: later tests never run it.
    assert run(selector, D, "CACHED = 2", "CACHED = 3") == CONSERVATIVE_TSQL


def test_copy_into_subclass_is_followed(selector):
    # TSQL.COPY = {**Dialect.TABLE}: a container copy; readers of TABLE are self.TABLE[...].
    assert run(selector, D, '"B": 1,', '"B": 2,') == {TABLE}


def test_same_kind_value_flows_through_operator_copy(selector):
    # TSQL.SUM = Dialect.WEEK + 1: int stays int, so import cannot fail; SUM has no readers.
    assert run(selector, D, "    WEEK = 0", "    WEEK = 5") == {WEEK, TW}


def test_kind_change_through_operator_falls_back(selector):
    # "5" + 1 would raise while TSQL's body runs: every importer breaks.
    assert run(selector, D, "    WEEK = 0", '    WEEK = "5"') == CONSERVATIVE_TSQL


def test_derived_by_call_at_import_falls_back(selector):
    # TSQL.ABS = abs(Dialect.CALLED): code runs on the new value at import.
    assert run(selector, D, "CALLED = 4", "CALLED = 5") == CONSERVATIVE_TSQL


def test_lambda_in_table_uses_the_call_lines(selector):
    # The lambda's first line also ran at import (credited to SLOW): only the call lines count.
    assert run(selector, D, "            args,", "            args[1:],") == {LAM}


def test_lambda_in_table_called_at_import_falls_back(selector):
    # moon-18: a table lambda called while modules import (Parser lambdas parsing the duckdb
    # templates) built state the lines map never credits to a test: no lambda narrowing.
    line = FILES[D].splitlines().index('        "A": lambda args: make(') + 1
    real = selector.maps.import_time.modules
    selector.maps.import_time.modules = lambda path, first, last, start=None: (
        {"sqlglot.other"} if path == D and start == line else real(path, first, last, start))
    got = run(selector, D, "            args,", "            args[1:],")
    assert LAM in got and got != {LAM}


def test_unread_name_falls_back(selector):
    assert run(selector, D, "UNREAD = 3", "UNREAD = 4") == CONSERVATIVE_TSQL


def test_module_constant_follows_import_alias(selector):
    # LIMIT is read in Dialect.limit and, as CAP, in sqlglot/user.py.
    assert run(selector, D, "LIMIT = 3", "LIMIT = 4") == {LIMIT_T, CAP}


def test_non_assignment_in_class_body_falls_back(selector):
    # A call in the body may mutate objects other modules own: with no import-state diff
    # (this fake repo has no snapshot) nothing rules that out → ALL.
    edit = ("    WEEK = -1\n", "    WEEK = -1\n    print(WEEK)\n")
    assert run(selector, D, *edit) == EVERYONE
    # Once the global-effect check passes, the class's conservative set.
    selector._foreign_state = lambda path: None
    assert run(selector, D, *edit) == CONSERVATIVE_TSQL


def test_decorated_class_with_side_effects_falls_back(selector):
    got = run(selector, D, "class TSQL(Dialect):", "@register\nclass TSQL(Dialect):")
    assert got == CONSERVATIVE_TSQL


def test_when_v1_cannot_bound_a_module_edit_never_narrow(selector):
    # consts.K is star-imported by stars.py: v1 answers ALL, and so must we.
    assert run(selector, "sqlglot/consts.py", "K = 1", "K = 2") == EVERYONE


def test_prebuilt_index_is_used_only_for_the_source_it_describes(tmp_path):
    import pickle
    from selector import nameindex
    repo = tmp_path / "src"
    for path, text in FILES.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    out = tmp_path / "names.pkl"
    data = nameindex.build(nameindex.read_sources(repo), nameindex.file_sizes(repo))
    out.write_bytes(pickle.dumps(data))
    loaded = nameindex.load(out, repo)
    assert loaded is not None
    paths = {s.path for s in loaded.sites("WEEK", "sqlglot/dialect.py", ("TSQL",), False)}
    assert {"sqlglot/dialect.py", "tests/test_tsql.py"} <= paths
    (repo / "sqlglot/user.py").write_text(USER + "# edited\n")
    assert nameindex.load(out, repo) is None


def test_dynamic_lookup_on_a_module_object_cannot_read_a_class_attribute():
    """getattr(<module>, name): a module alias of a repo module, or a local bound only to
    module objects, reads module attributes, never class attributes. Any other local, or an
    alias the file binds again (here: a parameter), may be any object."""
    from selector import names

    load = (
        "import importlib\n"
        "from sqlglot import expressions as exp\n\n\n"
        "def by_alias(name):\n"
        "    return getattr(exp, name)\n\n\n"
        "def by_local(name, path):\n"
        "    module = exp\n"
        "    if path:\n"
        "        module = importlib.import_module(path)\n"
        "    return getattr(module, name)\n\n\n"
        "def by_anything(name, obj):\n"
        "    return getattr(obj, name)\n")
    shadow = ("from sqlglot import expressions as exp\n\n\n"
              "def f(name, exp):\n"
              "    return getattr(exp, name)\n")
    sources = {"sqlglot/__init__.py": "", "sqlglot/expressions.py": "class Expr:\n    W = 1\n",
               "sqlglot/load.py": load, "sqlglot/shadow.py": shadow}
    found = names.Index(sources)._dynamic_sites()
    on_module = {(path, site.lines[0]): pattern for path, _, site, _, _, pattern in found}
    assert on_module[("sqlglot/load.py", 6)] is not None
    assert on_module[("sqlglot/load.py", 13)] is not None
    assert on_module[("sqlglot/load.py", 17)] is None
    assert on_module[("sqlglot/shadow.py", 5)] is None


def test_self_read_records_its_receiver_class():
    """`self.NAME` in a plain method: the object is of that class or a subclass. Not for a
    static method, a rebound receiver, or a method read off the class object."""
    from selector import names

    source = ("class A:\n"
              "    def f(self):\n"
              "        return self.WEEK\n\n"
              "    @staticmethod\n"
              "    def g(x):\n"
              "        return x.WEEK\n\n"
              "    def h(self):\n"
              "        self = make()\n"
              "        return self.WEEK\n\n\n"
              "class B:\n"
              "    def k(self):\n"
              "        return self.WEEK\n\n\n"
              "STORED = B.k\n")
    index = names.Index({"sqlglot/__init__.py": "", "sqlglot/a.py": source})
    recv = {site.lines[0]: site.recv for node, ctx, stmt in index.walk("sqlglot/a.py")
            if getattr(node, "attr", None) == "WEEK"
            for site in [index._site("sqlglot/a.py", node, ctx, stmt)]}
    assert recv[3] == ("A",)
    assert recv[7] is None  # static method: any object
    assert recv[11] is None  # receiver rebound
    assert recv[16] is None  # B.k is read off the class: may be called with any object


SERDE = '''\
from sqlglot import expressions as exp


def load(name, path):
    module: object
    if path:
        module = __import__(path, fromlist=[name])
    else:
        module = exp
    return getattr(module, name)  # @module_lookup


def maybe(name, obj):
    module = exp
    module = obj  # rebound to any object
    return getattr(module, name)  # @any_lookup
'''


def test_computed_lookup_on_a_module_cannot_read_a_class_attribute():
    from selector.names import Index
    index = Index({"sqlglot/serde.py": SERDE, "sqlglot/expressions.py": "",
                   "sqlglot/__init__.py": ""})
    found = {site.lines[0]: on_module for _, _, site, _, _, on_module in index._dynamic_sites()}
    rows = SERDE.splitlines()
    line = next(i for i, row in enumerate(rows, 1) if "@module_lookup" in row)
    other = next(i for i, row in enumerate(rows, 1) if "@any_lookup" in row)
    assert found[line] is not None  # module globals only: never a class attribute
    assert found[other] is None


@pytest.mark.parametrize("old, new, kept", [
    ('{"x": 1, "y": 2}', '{"x": 1, "y": 3}', {"truth", "keys"}),
    ('{"x": 1}', '{"z": 1}', {"truth"}),
    ('{"x": 1}', '{}', set()),
    ('{"x": 1, **A}', '{"x": 2, **A}', set()),  # unpacking: unknown
    ("True", "False", set()),
    ("3", "4", {"truth"}),
    ('["a", "b"]', '["b", "a"]', {"truth", "keys"}),
    ("[]", "()", set()),  # another type
])
def test_what_a_literal_edit_keeps(old, new, kept):
    import ast
    from selector.names import _kept
    assert _kept(ast.parse(old, mode="eval").body, ast.parse(new, mode="eval").body) == kept


@pytest.mark.parametrize("code, use", [
    ("if x.A: pass", "truth"),
    ("y = 1 if not x.A else 2", "truth"),
    ("if a and x.A: pass", "truth"),
    ("y = a and x.A", ""),  # returns x.A itself
    ("y = bool(x.A or b)", "truth"),
    ("y = 'k' in x.A", "keys"),
    ("y = x.A in z", ""),
    ("y = x.A.get('k')", ""),
])
def test_how_a_read_uses_the_value(code, use):
    import ast
    from selector.names import _use
    tree = ast.parse(code)
    parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "A")
    assert _use(node, parents) == use


def test_run_time_read_spans_are_the_node_lines():
    """r36: a run-time name / attribute read is observed on its own lines (sys.monitoring LINE
    events, as the lines map records them), not on its whole statement's: a read in a branch
    not taken runs no line of its own; a read that runs fires a LINE event on one of them."""
    from selector import names

    source = ("class C:\n"
              "    A = 1\n"
              "    B = 2\n\n"
              "    def f(self, c):\n"
              "        x = g(\n"
              "            1,\n"
              "            self.A\n"
              "            if c\n"
              "            else self.B,\n"
              "            'A',\n"
              "        )\n"
              "        return x\n\n\n"
              "def g(*a):\n"
              "    return a\n")
    index = names.Index({"sqlglot/__init__.py": "", "sqlglot/a.py": source})
    spans = {(type(node).__name__, getattr(node, "attr", getattr(node, "value", None))):
             index._site("sqlglot/a.py", node, ctx, stmt).lines
             for node, ctx, stmt in index.walk("sqlglot/a.py")
             if getattr(node, "attr", None) in ("A", "B")
             or getattr(node, "value", None) == "A"}
    assert spans[("Attribute", "A")] == (8, 8)
    assert spans[("Attribute", "B")] == (10, 10)
    assert spans[("Constant", "A")] == (6, 12)  # a constant may be folded: statement lines
    space: dict = {}
    exec(compile(source, "a.py", "exec"), space)
    mon, tool = sys.monitoring, 4
    for c, ran, not_ran in ((True, 8, 10), (False, 10, 8)):
        seen: set[int] = set()
        mon.use_tool_id(tool, "r36-test")
        try:
            mon.register_callback(
                tool, mon.events.LINE,
                lambda code, line: seen.add(line) if code.co_filename == "a.py" else None)
            mon.set_events(tool, mon.events.LINE)
            space["C"]().f(c)
        finally:
            mon.set_events(tool, 0)
            mon.free_tool_id(tool)
        assert ran in seen and not_ran not in seen, (c, sorted(seen))
