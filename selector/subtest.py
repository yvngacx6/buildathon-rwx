"""Sub-test verification of the TPC executor tests (r17).

tests/test_executor.py::TestExecutor::test_execute_tpcds (21.5 s), test_execute_tpch (3.9 s)
and test_optimized_tpch (3.5 s) loop over ~120 TPC queries. A function-body edit usually runs
in a few of those queries only, yet the per-test lines map keeps the whole test.

Build (pinned source, not charged): for every query, the sqlglot lines each step of the test
runs (fresh fork per query, sys.monitoring):
  * parse:  parse_one(sql) + pickle.dumps (the pool's submit)
  * exec:   pickle.loads, find_tables, execute(...), pickle round trip of the result Table
            (only queries with `# execute: true`, as mp_execute)
  * duck:   transpile(sql, write="duckdb") (cached_execute; the expected frame comes from it)
  * opt:    transpile(optimized, write="duckdb") (test_optimized_tpch, TPC-H only)
plus the lines the class set-up runs (Table construction, pickling the tables for the pool:
"global"), the pinned outputs (digest of the result columns / rows with types, duckdb SQL) and
each query's CPU.

Run: for a commit whose .py edits are all clean function-body edits (the diffexec jobs; no
definition-time / import-time effect, else the test is "fixed" and never a candidate), a query
that runs none of the touched statements behaves exactly as at the pinned commit. The queries
that do run one are re-run in a forked child with the changed functions' code swapped in
(as diffexec/replay.py), doing what the test does, and must give exactly the pinned outputs
(type-strict, bit-exact floats, same duckdb SQL → same duckdb frame). Then the test's outcome is
the pinned one (pass) and it is dropped. Global lines touched, any difference, exception,
timeout, unknown state → keep.
"""

from __future__ import annotations

import ast
import csv
import gc
import hashlib
import io
import marshal
import os
import pickle
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_STORE = REPO / "index" / "subtest.zip"

TESTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": ("tpcds", "execute"),
    "tests/test_executor.py::TestExecutor::test_execute_tpch": ("tpch", "execute"),
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": ("tpch", "optimized"),
}
SUITES = {"tpch": "optimizer/tpc-h/", "tpcds": "optimizer/tpc-ds/"}
SCORER_SLOWDOWN = 2.0
BUDGET_S = float(os.environ.get("BR_SUBTEST_S", "12"))
SHARE = 0.6  # spend at most this share of what dropping saves


# ── shared: what the test does for one query ─────────────────────────────────

def digest_table(table) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(repr(tuple(table.columns)).encode())
    for row in table.rows:
        h.update(repr(tuple((type(v).__name__, v) for v in row)).encode())
        h.update(b"\n")
    h.update(repr(len(table.rows)).encode())
    return h.hexdigest()


def run_query(q: dict, schema: dict, tables: dict, opt: bool):
    """Outputs of one query: (exec digest | None, duck sql | None, opt duck sql | None)."""
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute

    out_exec = out_duck = out_opt = None
    if not opt:
        # the pool pickles every submitted expression and the worker unpickles it (even when
        # mp_execute then returns None)
        expr = pickle.loads(pickle.dumps(parse_one(q["sql"])))
    if q["execute"] and not opt:
        used = {t.name: tables[t.name] for t in find_tables(expr)}
        res = execute(expr, schema=schema, tables=used)
        res = pickle.loads(pickle.dumps(res))
        out_exec = digest_table(res)
        out_duck = transpile(q["sql"], write="duckdb")[0]
    if opt:
        out_duck = transpile(q["sql"], write="duckdb")[0]
        out_opt = transpile(q["optimized"], write="duckdb")[0]
    return out_exec, out_duck, out_opt


# ── build ─────────────────────────────────────────────────────────────────────

def _load_fixture_tables(root: Path, suite: str, schema: dict) -> dict:
    """setUpClass's CSV parsing (test code, not sqlglot): {table: (columns, rows)}."""
    import gzip
    out = {}
    for table, columns in schema.items():
        fn = root / "tests" / "fixtures" / SUITES[suite] / f"{table}.csv.gz"
        with open(fn, "rb") as f:
            gz = f.read(2) == b"\x1f\x8b"
        fh = gzip.open(fn, "rt", newline="") if gz else open(fn, encoding="utf-8", newline="")
        reader = csv.reader(fh, delimiter="|")
        rows, ctypes = [], []
        next(reader)
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v)
                              for t, v in zip(ctypes, row)))
        fh.close()
        out[table] = (dict(columns), rows)
    return out


class _Cov:
    """sys.monitoring LINE events in files under `prefix`: {relpath: set(lines)}."""

    TOOL = 4

    def __init__(self, prefix: str):
        self.prefix = prefix
        self.lines: dict[str, set[int]] = {}
        self.on = False

    def start(self):
        mon = sys.monitoring
        mon.use_tool_id(self.TOOL, "subtest")
        prefix, lines, plen = self.prefix, self.lines, len(self.prefix)
        DISABLE = mon.DISABLE

        def line(code, lineno):
            fn = code.co_filename
            if fn.startswith(prefix):
                lines.setdefault(fn[plen:], set()).add(lineno)
            return DISABLE

        mon.register_callback(self.TOOL, mon.events.LINE, line)
        mon.set_events(self.TOOL, mon.events.LINE)
        mon.restart_events()  # locations DISABLEd by an earlier phase report again

    def stop(self):
        mon = sys.monitoring
        mon.set_events(self.TOOL, 0)
        mon.register_callback(self.TOOL, mon.events.LINE, None)
        mon.free_tool_id(self.TOOL)
        return {k: sorted(v) for k, v in self.lines.items()}


def _in_fork(fn):
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.close(rfd)
            data = pickle.dumps(fn())
            with os.fdopen(wfd, "wb") as fh:
                fh.write(data)
            code = 0
        except BaseException:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        finally:
            os._exit(code)
    os.close(wfd)
    with os.fdopen(rfd, "rb") as fh:
        data = fh.read()
    _, status = os.waitpid(pid, 0)
    if status != 0:
        raise RuntimeError("child failed")
    return pickle.loads(data)


def build(root: Path, out: Path) -> None:
    root = root.resolve()
    sys.path.insert(0, str(root))
    sys.setrecursionlimit(1000)  # the pool worker's default
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs
    import sqlglot  # noqa: F401
    from sqlglot import parse_one, transpile  # noqa: F401
    from sqlglot.executor import execute  # noqa: F401
    from sqlglot.executor.table import Table
    import sqlglot.optimizer  # noqa: F401
    import sqlglot.dialects.duckdb  # noqa: F401
    prefix = str(root) + "/"
    schemas = {"tpch": TPCH_SCHEMA, "tpcds": TPCDS_SCHEMA}
    raw = {s: _load_fixture_tables(root, s, schemas[s]) for s in SUITES}
    meta = {"fingerprint": fingerprint(root), "suites": {}, "schemas": schemas}

    def make_tables(suite):
        return {name: Table(columns=cols, rows=rows) for name, (cols, rows) in raw[suite].items()}

    def global_phase():
        cov = _Cov(prefix)
        cov.start()
        tabs = {s: make_tables(s) for s in SUITES}
        for s in SUITES:
            pickle.loads(pickle.dumps((schemas[s], tabs[s])))
        return cov.stop()

    meta["global"] = _in_fork(global_phase)
    tables = {s: make_tables(s) for s in SUITES}
    for suite in SUITES:
        queries = []
        fixture = SUITES[suite] + ("tpc-h.sql" if suite == "tpch" else "tpc-ds.sql")
        for qmeta, sql, optimized in load_sql_fixture_pairs(fixture):
            q = {"sql": sql, "optimized": optimized, "execute": bool(qmeta.get("execute"))}

            def traced(q=q):
                from sqlglot import find_tables, parse_one, transpile
                from sqlglot.executor import execute
                phases = {}
                cov = _Cov(prefix)
                cov.start()
                expr = parse_one(q["sql"])
                e2 = pickle.loads(pickle.dumps(expr))
                phases["parse"] = cov.stop()
                outs = {}
                if q["execute"]:
                    cov = _Cov(prefix)
                    cov.start()
                    used = {t.name: tables[suite][t.name] for t in find_tables(e2)}
                    res = execute(e2, schema=schemas[suite], tables=used)
                    res = pickle.loads(pickle.dumps(res))
                    phases["exec"] = cov.stop()
                    outs["exec"] = digest_table(res)
                cov = _Cov(prefix)
                cov.start()
                outs["duck"] = transpile(q["sql"], write="duckdb")[0]
                phases["duck"] = cov.stop()
                return phases, outs

            def traced_opt(q=q):
                # test_optimized_tpch alone: both transpiles in one fresh process
                from sqlglot import transpile
                cov = _Cov(prefix)
                cov.start()
                duck = transpile(q["sql"], write="duckdb")[0]
                opt = transpile(q["optimized"], write="duckdb")[0]
                return cov.stop(), duck, opt

            def timed(q=q):
                t0 = time.process_time()
                a = run_query(q, schemas[suite], tables[suite], False)
                t1 = time.process_time()
                b = run_query(q, schemas[suite], tables[suite], True) if suite == "tpch" else None
                t2 = time.process_time()
                return a, b, t1 - t0, t2 - t1

            try:
                phases, outs = _in_fork(traced)
                if suite == "tpch":
                    phases["opt"], duck, outs["opt"] = _in_fork(traced_opt)
                    if duck != outs["duck"]:
                        raise RuntimeError("duckdb sql differs between runs")
                a, b, cpu_exec, cpu_opt = _in_fork(timed)
                # the untraced run must give the same outputs (else: not deterministic → never used)
                stable = (a[0] == outs.get("exec")) and (
                    a[1] == (outs["duck"] if q["execute"] else None))
                if b is not None:
                    stable = stable and b[1] == outs["duck"] and b[2] == outs["opt"]
            except RuntimeError:  # e.g. RecursionError at the worker's limit: never used
                phases, outs, cpu_exec, cpu_opt, stable = {}, {}, 0.0, 0.0, False
            q.update(phases=phases, outs=outs, cpu_exec=cpu_exec, cpu_opt=cpu_opt, stable=stable)
            queries.append(q)
            print(f"{suite} {len(queries)} exec={q['execute']} cpu={cpu_exec:.2f}/{cpu_opt:.2f}"
                  f" stable={stable}", file=sys.stderr)
        meta["suites"][suite] = queries
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.pkl", pickle.dumps(meta))
        for suite in SUITES:
            for name, (cols, rows) in raw[suite].items():
                zf.writestr(f"{suite}/{name}", marshal.dumps((tuple(cols), rows)))
    os.replace(tmp, out)


def fingerprint(root: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    for p in sorted((root / "sqlglot").rglob("*.py")):
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    for s, d in SUITES.items():
        for p in sorted((root / "tests" / "fixtures" / d).iterdir()):
            h.update(p.name.encode())
            h.update(p.read_bytes())
    h.update((root / "tests" / "helpers.py").read_bytes())
    h.update((root / "tests" / "test_executor.py").read_bytes())
    return h.hexdigest()


# ── run time ──────────────────────────────────────────────────────────────────

def _hit(lines_by_file: dict, spans: dict) -> bool:
    for path, ss in spans.items():
        lines = lines_by_file.get(path)
        if not lines:
            continue
        for first, last in ss:
            for ln in lines:  # sorted
                if ln > last:
                    break
                if ln >= first:
                    return True
    return False


class SubTest:
    def __init__(self, selector, store: Path):
        self.sel = selector
        self.store = store
        self._meta = None
        self.numbers = {t["number"]: t["id"] for t in selector.tests if t["id"] in TESTS}
        self.spent = 0.0

    @classmethod
    def create(cls, selector):
        if os.environ.get("BR_SUBTEST", "1") == "0":
            return None
        store = Path(os.environ.get("BR_SUBTEST_STORE", DEFAULT_STORE))
        if not store.is_file():
            return None
        self = cls(selector, store)
        return self if self.numbers else None

    def meta(self):
        if self._meta is None:
            with zipfile.ZipFile(self.store) as zf:
                self._meta = pickle.loads(zf.read("meta.pkl"))
            if self._meta["fingerprint"] != fingerprint(Path(self.sel.sqlglot)):
                self._meta = {}
        return self._meta

    def plan(self, spans: dict, numbers: set[int]):
        """→ ({number: [(suite, query index, opt)]}, cpu estimate s) or None (cannot)."""
        meta = self.meta()
        if not meta:
            return None
        if _hit(meta["global"], spans):
            return None
        out, cpu = {}, 0.0
        for n in numbers:
            suite, kind = TESTS[self.numbers[n]]
            todo = []
            for i, q in enumerate(meta["suites"][suite]):
                if not q["stable"]:
                    return None  # not reproducible at the pinned commit: never verify
                ph = q["phases"]
                if kind == "execute":
                    hit = _hit(ph["parse"], spans) or (
                        q["execute"] and (_hit(ph["exec"], spans) or _hit(ph["duck"], spans)))
                else:
                    hit = _hit(ph["opt"], spans)
                if hit:
                    if not q["stable"]:
                        return None
                    todo.append((suite, i, kind == "optimized"))
                    cpu += q["cpu_opt"] if kind == "optimized" else q["cpu_exec"]
            out[n] = todo
        return out, cpu

    def verify(self, jobs, todo: list, budget: float) -> bool:
        """Run the queries in a forked child with the changed code swapped in."""
        meta = self.meta()
        root = str(self.sel.sqlglot)
        _warm(root)
        _warm_code(root, jobs)
        rfd, wfd = os.pipe()
        t0 = os.times()
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                os.close(rfd)
                ok = _child(root, jobs, todo, meta, self.store, budget)
                with os.fdopen(wfd, "wb") as fh:
                    fh.write(b"OK" if ok else b"NO")
                code = 0
            except BaseException:  # noqa: BLE001
                if os.environ.get("SUBTEST_DEBUG"):
                    import traceback
                    traceback.print_exc()
            finally:
                os._exit(code)
        os.close(wfd)
        import select as _select
        from .diffexec import wall_guard
        deadline = time.monotonic() + wall_guard(budget)
        chunks = []
        with os.fdopen(rfd, "rb") as fh:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                ready, _, _ = _select.select([fh], [], [], left)
                if not ready:
                    break
                chunk = os.read(fh.fileno(), 1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
        timed_out = time.monotonic() >= deadline
        if timed_out:
            os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
        t1 = os.times()
        self.last_child = round((t1.children_user - t0.children_user)
                                + (t1.children_system - t0.children_system), 2)
        self.spent += self.last_child
        return not timed_out and status == 0 and b"".join(chunks) == b"OK"

    def prune(self, jobs, spans, candidates: set[int], benefit_of) -> tuple[set[int], str]:
        """jobs: diffexec jobs [(path, new text, prunable)], spans {path: [(first, last)]} of
        the touched statements, candidates: the TPC tests that only these jobs keep."""
        cands = candidates & set(self.numbers)
        if not cands or not spans:
            return set(), ""
        try:
            got = self.plan(spans, cands)
        except Exception as e:  # noqa: BLE001
            return set(), f"subtest: keep ({type(e).__name__})"
        if got is None:
            return set(), "subtest: keep (set-up touched / unknown / unstable)"
        per, cpu = got
        # cheapest first: drop the tests whose verification pays off
        chosen, todo, est = set(), [], 0.0
        for n in sorted(per, key=lambda n: sum(self._cpu(x) for x in per[n])):
            qs = [x for x in per[n] if x not in todo]
            c = sum(self._cpu(x) for x in qs) + (0.3 if qs and not todo else 0.0)  # fork, swap, tables
            trial = chosen | {n}
            spend = SCORER_SLOWDOWN * (est + c)
            gain = benefit_of(trial)
            if gain > 0 and spend <= min(BUDGET_S, SHARE * gain / 1000):
                chosen, est = trial, est + c
                todo.extend(qs)
        if not chosen:
            return set(), f"subtest: keep (est {cpu:.1f}s CPU for {sum(map(len, per.values()))} queries)"
        if not todo:
            return chosen, f"subtest: {sorted(chosen)} dropped (no query runs a touched statement)"
        try:
            cap = min(BUDGET_S / SCORER_SLOWDOWN, 2.5 * est + 1.0)
            ok = self.verify(jobs, todo, cap)
        except Exception as e:  # noqa: BLE001
            return set(), f"subtest: keep ({type(e).__name__})"
        if not ok:
            return set(), f"subtest: keep (verification of {len(todo)} queries failed, {self.last_child:.2f}s, est {est:.2f}, cap {cap:.2f})"
        return chosen, (f"subtest: {sorted(chosen)} dropped ({len(todo)} queries re-run,"
                        f" {self.last_child:.2f}s, est {est:.2f}, cap {cap:.2f})")

    def _cpu(self, x):
        suite, i, opt = x
        q = self.meta()["suites"][suite][i]
        return q["cpu_opt"] if opt else q["cpu_exec"]


def _warm_code(root: str, jobs) -> None:
    """Compile the jobs' pinned and patched files in the parent, once per commit for all the
    forked verifiers (diffexec replay, subtest, slowverify): see diffexec/replay.py."""
    try:
        from diffexec.replay import warm_files
        warm_files(root, [(path, new_text) for path, new_text, _ in jobs])
    except Exception:  # noqa: BLE001  (the child compiles them itself)
        pass


def _swap(root: str, jobs) -> str | None:
    """Swap the changed functions' code (as diffexec/replay.py). → bail reason or None."""
    import functools
    import importlib
    import types
    from diffexec import serial
    from diffexec.replay import _new_code, _old_code, _walk
    swaps = {}
    for path, new_text, _ in jobs:
        filename = os.path.join(root, path)
        old_top = _old_code(filename)  # compiled in the parent before the fork (warm_files)
        new_top = _new_code(filename, new_text)
        old_codes, new_codes = _walk(old_top), _walk(new_top)
        if old_codes.keys() != new_codes.keys():
            return "functions added / removed"
        for k, o in old_codes.items():
            n = new_codes[k]
            if serial.code_digest(o) == serial.code_digest(n):
                continue
            if o.co_freevars != n.co_freevars or o.co_cellvars != n.co_cellvars:
                return "closure layout"
            if serial.code_digest(o, shallow=True) != serial.code_digest(n, shallow=True) and (
                    o is old_top or not (o.co_flags & 0x01)):
                return "definition-time body"
            swaps[(filename, o.co_qualname, o.co_firstlineno)] = n
        importlib.import_module(path[:-3].replace("/", ".").removesuffix(".__init__"))
    if not swaps:
        return "nothing changed"
    seen = set()
    for obj in gc.get_objects():
        if type(obj) is types.FunctionType:
            c = obj.__code__
            k = (c.co_filename, c.co_qualname, c.co_firstlineno)
            n = swaps.get(k)
            if n is not None:
                obj.__code__ = n
                seen.add(k)
        elif type(obj) is functools._lru_cache_wrapper:
            obj.cache_clear()
    # Every changed code object must belong to a live function or sit inside one that does
    # (nested defs / lambdas / comprehensions are made from the swapped parent's constants).
    def inside(k):
        return any(k != s and k[0] == s[0] for s in seen)
    for k in swaps:
        if k not in seen and not inside(k):
            return f"no live function for {k[1]}"
    return None


def _warm(root: str) -> None:
    """Import what the queries use (the selector does it once, before the first fork)."""
    if root not in sys.path:
        sys.path.insert(0, root)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import sqlglot  # noqa: F401
    import sqlglot.executor  # noqa: F401
    import sqlglot.executor.python  # noqa: F401
    import sqlglot.optimizer  # noqa: F401
    import sqlglot.planner  # noqa: F401
    import sqlglot.dialects.duckdb  # noqa: F401
    from diffexec import replay, serial  # noqa: F401


def _child(root, jobs, todo, meta, store, budget) -> bool:
    import signal
    sys.setrecursionlimit(1000)  # the pool worker's default (this stack is deeper: stricter)
    # CPU cap: SIGPROF's default action ends the child (→ keep the tests)
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_PROF, budget)
    _warm(root)
    from sqlglot.executor.table import Table
    why = _swap(root, jobs)
    if why:
        if os.environ.get("SUBTEST_DEBUG"):
            print("subtest bail:", why, file=sys.stderr)
        return False
    import contextlib
    tables: dict = {}
    with zipfile.ZipFile(store) as zf:
        for suite, i, opt in todo:
            q = meta["suites"][suite][i]
            # each query (with its table loading): at most 3x its pinned CPU + 0.8 s, else the
            # child ends (→ keep)
            signal.setitimer(signal.ITIMER_PROF, 3 * (q["cpu_opt"] if opt else q["cpu_exec"]) + 0.8)
            tabs = {}
            if q["execute"] and not opt:
                from sqlglot import find_tables, parse_one
                for t in find_tables(parse_one(q["sql"])):
                    key = (suite, t.name)
                    if key not in tables:
                        cols, rows = marshal.loads(zf.read(f"{suite}/{t.name}"))
                        tables[key] = Table(columns=cols, rows=rows)
                    tabs[t.name] = tables[key]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    got = run_query(q, meta["schemas"][suite], tabs, opt)
                except Exception:  # noqa: BLE001
                    return False
            want = (q["outs"].get("exec") if not opt else None,
                    (q["outs"]["duck"] if (q["execute"] or opt) else None),
                    q["outs"].get("opt") if opt else None)
            if got != want:
                if os.environ.get("SUBTEST_DEBUG"):
                    print("subtest diff:", suite, i, opt, file=sys.stderr)
                return False
    return True


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "build":
        build(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        sys.exit("usage: python -m selector.subtest build SQLGLOT_ROOT OUT.zip")
