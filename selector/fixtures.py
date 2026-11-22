"""Fixture edits compared through what each reader consumes (r27, moonshot 14).

The reads map says which tests open a fixture. Many of them consume only part of what the
pinned loaders (tests/helpers.py) yield: `TestExecutor.setUpClass` loads BOTH tpc-h.sql and
tpc-ds.sql for all ~60 executor tests, yet only test_execute_tpch / tpcds and
test_optimized_tpch use the pairs; a check_file title only names a subTest.

Run: apply the hunk, run the PINNED loader on the old and new bytes (in memory, through an
`open` shim with the same utf-8 / universal-newline semantics), and compare, per reader, only
the view it consumes (table below). Equal view → that reader executes identically → drop it.
The new load raises → keep every reader (setUpClass would error the class). Readers not in
the table → keep. Built from the pinned test sources only (which fields each test consumes),
never from edit kinds or failures.

  reader                                   loader  view
  TestOptimizer check_file / annotate_*    pairs   (meta minus title, sql, expected)
  load_sql_fixtures readers                list    every item
  TestTranspile.test_pretty                pairs   (sql, expected)
  TestExecutor.test_execute_tpch / tpcds   pairs   (bool(meta.execute), sql)  + parse check
  TestExecutor.test_optimized_tpch         pairs   (sql, expected)  (tpc-h only)
  other TestExecutor tests (tpc files)     pairs   nothing beyond "the load succeeds"

Parse check: `_mp_execute` runs parse_one(sql) and a pickle round trip on every query and
executes only those with a truthy `execute` meta. When every changed query is unexecuted
(old and new) and parse_one + pickle succeed under the pinned code (commit has no .py edit),
the test's outcome is unchanged.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

EXECUTOR = "tests/test_executor.py::TestExecutor::"
OPTIMIZER = "tests/test_optimizer.py::TestOptimizer::"
TPC = {"tests/fixtures/optimizer/tpc-h/tpc-h.sql": "tpch",
       "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": "tpcds"}
LIST_READERS = {
    ("tests/test_transpile.py::TestTranspile::test_identity", "identity.sql"),
    ("tests/test_serde.py::TestSerde::test_serde", "identity.sql"),
    ("tests/test_transpile.py::TestTranspile::test_partial", "partial.sql"),
}
PRETTY = ("tests/test_transpile.py::TestTranspile::test_pretty", "pretty.sql")
# The test sources the table is read from: a commit touching any of them (or other test code)
# keeps the plain rule.
CONSUMER_SOURCES = ("tests/helpers.py", "tests/test_optimizer.py", "tests/test_executor.py",
                    "tests/test_transpile.py", "tests/test_serde.py")
PARSE_TIMEOUT_S = 20

_PARSE_CHILD = r"""
import json, pickle, sys
sys.path.insert(0, sys.argv[1])
from sqlglot import parse_one
ok = True
for sql in json.load(sys.stdin):
    try:
        pickle.loads(pickle.dumps(parse_one(sql)))
    except Exception:
        ok = False
        break
print("OK" if ok else "FAIL")
"""


class FixtureRule:
    def __init__(self, sqlglot: Path) -> None:
        self.sqlglot = sqlglot
        self._helpers: dict | None = None
        self._bodies: dict[str, str] | None = None

    # ── pinned sources ───────────────────────────────────────────────────────

    def _loader_ns(self, data: bytes) -> dict:
        """A fresh namespace of the pinned tests/helpers.py whose `open` serves `data`."""
        if self._helpers is None:
            src = (self.sqlglot / "tests" / "helpers.py").read_text(encoding="utf-8")
            self._helpers = {"code": compile(src, "tests/helpers.py", "exec")}
        ns: dict = {"__name__": "_pinned_helpers", "__file__": str(self.sqlglot / "tests" / "helpers.py")}
        exec(self._helpers["code"], ns)  # noqa: S102  (pinned test helper source)

        def shim(path, mode="r", encoding=None, **kw):  # helpers: open(path, encoding="utf-8")
            if mode != "r" or kw:
                raise RuntimeError("unexpected open")
            return io.TextIOWrapper(io.BytesIO(data), encoding=encoding)
        ns["open"] = shim
        return ns

    def load(self, data: bytes, rel: str, loader: str):
        """('ok', items) or ('err', why): the pinned loader run on these bytes."""
        try:
            ns = self._loader_ns(data)
            fn = ns["load_sql_fixture_pairs" if loader == "pairs" else "load_sql_fixtures"]
            return "ok", list(fn(rel))
        except Exception as e:  # noqa: BLE001
            return "err", f"{type(e).__name__}"

    def _optimizer_bodies(self) -> dict[str, str]:
        if self._bodies is None:
            self._bodies = {}
            src = (self.sqlglot / "tests" / "test_optimizer.py").read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                    self._bodies[node.name] = ast.get_source_segment(src, node) or ""
        return self._bodies

    def consumer(self, path: str, test: str) -> tuple[str, str] | None:
        """(loader, view) of `test` reading fixture `path`; None: unknown → keep."""
        rel = path[len("tests/fixtures/"):]
        meth = test.rsplit("::", 1)[-1]
        if test.startswith(OPTIMIZER) and rel.startswith("optimizer/") and rel.endswith(".sql"):
            body = self._optimizer_bodies().get(meth, "")
            stem = rel[len("optimizer/"):-len(".sql")]
            uses = [m.group(1) for m in re.finditer(r'check_file\(\s*"([^"]+)"', body)]
            pairs = re.findall(r'load_sql_fixture_pairs\(\s*"([^"]+)"', body)
            lists = re.findall(r'load_sql_fixtures\(\s*"([^"]+)"', body)
            # one way of reading this file only, else unknown
            ways = (stem in uses) + (rel in pairs) + (rel in lists)
            if ways != 1:
                return None
            return ("list", "all") if rel in lists else ("pairs", "notitle")
        if test.startswith(EXECUTOR) and path in TPC:
            suite = TPC[path]
            if meth == f"test_execute_{suite}":
                return "pairs", "exec"
            if meth == "test_optimized_tpch":
                return ("pairs", "sql_exp") if suite == "tpch" else ("pairs", "load")
            return "pairs", "load"
        if (test, rel) == PRETTY:
            return "pairs", "sql_exp"
        if (test, rel) in LIST_READERS:
            return "list", "all"
        return None

    # ── views ────────────────────────────────────────────────────────────────

    @staticmethod
    def view(items, loader: str, v: str):
        if v == "load":
            return None
        if loader == "list" or v == "all":
            return items
        if v == "notitle":
            return [({k: x for k, x in m.items() if k != "title"}, s, e) for m, s, e in items]
        if v == "sql_exp":
            return [(s, e) for _, s, e in items]
        if v == "exec":
            return [(bool(m.get("execute")), s) for m, s, _ in items]
        raise ValueError(v)

    def _parses(self, sqls: list[str]) -> bool:
        """parse_one + pickle round trip of each, under the pinned sqlglot (child process)."""
        try:
            r = subprocess.run([sys.executable, "-c", _PARSE_CHILD, str(self.sqlglot)],
                               input=json.dumps(sqls), capture_output=True, text=True,
                               timeout=PARSE_TIMEOUT_S, cwd=str(self.sqlglot),
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        except Exception:  # noqa: BLE001
            return False
        return r.returncode == 0 and r.stdout.strip() == "OK"

    # ── entry point ──────────────────────────────────────────────────────────

    def narrow(self, path: str, old: bytes, new: bytes, readers: list[str],
               pinned_code: bool) -> tuple[set[str], str]:
        """The readers whose consumed view changes (or that are unknown). pinned_code: the
        commit edits no .py file, so the parse check may run the pinned sqlglot."""
        rel = path[len("tests/fixtures/"):]
        kept: set[str] = set()
        loaded: dict[str, tuple] = {}
        parsed = 0
        for test in readers:
            c = self.consumer(path, test)
            if c is None:
                kept.add(test)
                continue
            loader, v = c
            if loader not in loaded:
                loaded[loader] = (self.load(old, rel, loader), self.load(new, rel, loader))
            (so, items_old), (sn, items_new) = loaded[loader]
            if so != "ok" or sn != "ok":
                return set(readers), f"new load raises ({items_new})" if sn != "ok" else "old load failed"
            vo, vn = self.view(items_old, loader, v), self.view(items_new, loader, v)
            if vo == vn:
                continue
            if v == "exec" and pinned_code and len(vo) == len(vn):
                changed = [i for i in range(len(vo)) if vo[i] != vn[i]]
                if all(not vo[i][0] and not vn[i][0] for i in changed) and \
                        self._parses([vn[i][1] for i in changed]):
                    parsed += 1
                    continue
            kept.add(test)
        note = f"consumer views: {len(readers) - len(kept)}/{len(readers)} readers unchanged"
        return kept, note + (f", {parsed} by parse check" if parsed else "")
