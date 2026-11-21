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
STAGE = os.environ.get("BR_SUBTEST_STAGE", "1") != "0"  # moon-05 checkpoint verification
FALLBACK = os.environ.get("BR_SUBTEST_FALLBACK", "0") != "0"  # checkpoint differs → whole query
FALLBACK_CAP = float(os.environ.get("BR_SUBTEST_FALLBACK_CAP", "1.5"))  # laptop s
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


# ── moon-05: stage split of one executed query (checkpoints) ──────────────────
#
# The pool worker of test_execute_tpcds receives pickle(parse_one(sql)) and runs
# sqlglot.executor.execute on it. execute's body is mirrored here window by window, calling the
# very objects execute() calls (looked up in sqlglot.executor's namespace):
#   prep:  ensure_tables, ensure_schema, the supported_table_args check
#   optb:  optimize(...) and the eager logger.debug argument expression.sql(pretty=True)
#   plan:  Plan(expression)
#   post:  PythonExecutor(tables).execute(plan) + the result's pickle round trip
# Checkpoints: the post-parse pickle (what the worker receives) and a type-strict digest of the
# optimized expression after the debug .sql() (what Plan receives). A change whose touched
# statements run only in parse / optb can be verified by re-running those windows and comparing
# the checkpoint digests; plan / post are then identical (same code, same input).

def strict_digest(root) -> str:
    """Digest of an expression tree: classes, every arg key (None-valued too) in order, value
    types, DType values, .type, comments, _meta, parent / arg_key / index links and sharing."""
    from sqlglot.expressions import Expr
    h = hashlib.blake2b(digest_size=16)
    seen: dict[int, int] = {}
    stack = [(root, None, None, None)]
    while stack:
        node, parent, key, index = stack.pop()
        if not isinstance(node, Expr):
            h.update(b"V")
            h.update(repr((type(node).__name__, node)).encode())
            continue
        nid = id(node)
        if nid in seen:
            h.update(b"R%d" % seen[nid])
            continue
        seen[nid] = len(seen)
        cls = type(node)
        h.update(f"N{cls.__module__}.{cls.__qualname__}".encode())
        if parent is not None:
            h.update(repr((node.parent is parent, node.arg_key == key, node.index == index)).encode())
        t = getattr(node, "_type", None)
        if t is not None and t is not node:
            h.update(b"T" + strict_digest(t).encode())
        if getattr(node, "comments", None):
            h.update(repr(("C", node.comments)).encode())
        m = getattr(node, "_meta", None)
        if m is not None:
            h.update(repr(("M", sorted((k, type(v).__name__, repr(v)) for k, v in m.items()))).encode())
        args = node.args
        h.update(repr(("K", tuple(args))).encode())
        items = list(args.items())
        for k, v in reversed(items):
            if type(v) is list:
                stack.append(("]", None, None, None))
                for i in range(len(v) - 1, -1, -1):
                    stack.append((v[i], node, k, i))
                stack.append(("[", None, None, None))
            else:
                stack.append((v, node, k, None))
    return h.hexdigest()


def exec_windows(expr, schema, used, cov_factory=None, swap_until=None):
    """execute(expr, schema=schema, tables=used) window by window.
    → ({window: coverage}, result digest)."""
    import sqlglot.executor as E
    covs = {}

    def window(name, fn):
        cov = cov_factory() if cov_factory else None
        if cov:
            cov.start()
        try:
            return fn()
        finally:
            if cov:
                covs[name] = cov.stop()

    def prep():
        tables_ = E.ensure_tables(used, dialect=None)
        schema_ = E.ensure_schema(schema, dialect=None)
        if tables_.supported_table_args and tables_.supported_table_args != schema_.supported_table_args:
            raise E.ExecuteError("Tables must support the same table args as schema")
        return tables_, schema_

    tables_, schema_ = window("prep", prep)

    def optb():
        e = E.optimize(expr, schema_, leave_tables_isolated=True, dialect=None)
        e.sql(pretty=True)
        return e

    oe = window("optb", optb)
    d_opt = strict_digest(oe)
    plan = window("plan", lambda: E.Plan(oe))
    res = window("post", lambda: pickle.loads(pickle.dumps(E.PythonExecutor(tables=tables_).execute(plan))))
    return covs, d_opt, digest_table(res)


# ── build-machine calibration ────────────────────────────────────────────────────
#
# The image build may run on a busy box: CPU seconds measured there are inflated (shared cores,
# hyper-threads, caches), which made the budget refuse verifications it can afford (a store
# built at load ~20 had every query 2.4x too expensive). Each timed run is bracketed by a fixed
# pure-Python workload measured the same way, and the stored CPU is expressed in reference
# seconds: cpu * CALIB_REF / calib. CALIB_REF is the workload's CPU on the machine the budget
# constants were tuned on (laptop, idle); BR_SUBTEST_NOCALIB=1 stores raw seconds.

CALIB_REF = 0.0  # set below from the reference measurement


def _calib_work() -> int:
    acc = 0
    d: dict = {}
    for i in range(20000):
        k = ("k", i % 97)
        d[k] = d.get(k, 0) + i
        s = f"col{i % 31}".upper()
        acc += len(s) + (i & 7)
        lst = [i, s, k]
        acc += len(lst[1:])
    return acc + len(d)


def _calib() -> float:
    best = float("inf")
    for _ in range(5):
        t = time.process_time()
        _calib_work()
        best = min(best, time.process_time() - t)
    return best


def _norm(cpu: float, c0: float, c1: float) -> float:
    if os.environ.get("BR_SUBTEST_NOCALIB") == "1" or not CALIB_REF:
        return cpu
    return cpu * CALIB_REF / max(1e-6, (c0 + c1) / 2)


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
                    pexpr = pickle.dumps(expr)
                    outs["pexpr"] = pexpr
                    outs["d_parse"] = strict_digest(pickle.loads(pexpr))
                    cov = _Cov(prefix)
                    cov.start()
                    used = {t.name: tables[suite][t.name] for t in find_tables(e2)}
                    res = execute(e2, schema=schemas[suite], tables=used)
                    res = pickle.loads(pickle.dumps(res))
                    phases["exec"] = cov.stop()
                    outs["exec"] = digest_table(res)
                    # moon-05: the same execute, window by window, in a fresh fork
                    def windows():
                        e3 = pickle.loads(pexpr)
                        cov = _Cov(prefix)
                        cov.start()
                        u = {t.name: tables[suite][t.name] for t in find_tables(e3)}
                        ft = cov.stop()
                        covs, d_opt, dres = exec_windows(e3, schemas[suite], u, lambda: _Cov(prefix))
                        covs["prep"] = {k: sorted(set(v) | set(ft.get(k, ()))) for k, v in
                                        list(covs["prep"].items()) + list(ft.items())}
                        return covs, d_opt, dres

                    def stage_timed():
                        c0 = _calib()
                        t0 = time.process_time()
                        e4 = pickle.loads(pexpr)
                        import sqlglot.executor as E
                        sch = E.ensure_schema(schemas[suite], dialect=None)
                        oe = E.optimize(e4, sch, leave_tables_isolated=True, dialect=None)
                        oe.sql(pretty=True)
                        d2 = strict_digest(oe)
                        cpu_stage = time.process_time() - t0
                        return d2, _norm(cpu_stage, c0, _calib())
                    covs, d_opt, dres = _in_fork(windows)
                    d2, cpu_stage = _in_fork(stage_timed)
                    phases["win"] = covs
                    outs["d_opt"] = d_opt if (dres == outs["exec"] and d2 == d_opt) else None
                    outs["cpu_stage"] = cpu_stage
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
                c0 = _calib()
                t0 = time.process_time()
                a = run_query(q, schemas[suite], tables[suite], False)
                t1 = time.process_time()
                b = run_query(q, schemas[suite], tables[suite], True) if suite == "tpch" else None
                t2 = time.process_time()
                c1 = _calib()
                return a, b, _norm(t1 - t0, c0, c1), _norm(t2 - t1, c0, c1), (c0 + c1) / 2

            try:
                phases, outs = _in_fork(traced)
                if suite == "tpch":
                    phases["opt"], duck, outs["opt"] = _in_fork(traced_opt)
                    if duck != outs["duck"]:
                        raise RuntimeError("duckdb sql differs between runs")
                a, b, cpu_exec, cpu_opt, calib = _in_fork(timed)
                # the untraced run must give the same outputs (else: not deterministic → never used)
                stable = (a[0] == outs.get("exec")) and (
                    a[1] == (outs["duck"] if q["execute"] else None))
                if b is not None:
                    stable = stable and b[1] == outs["duck"] and b[2] == outs["opt"]
            except RuntimeError:  # e.g. RecursionError at the worker's limit: never used
                phases, outs, cpu_exec, cpu_opt, stable, calib = {}, {}, 0.0, 0.0, False, 0.0
            q.update(phases=phases, outs=outs, cpu_exec=cpu_exec, cpu_opt=cpu_opt, stable=stable,
                     calib=calib)
            queries.append(q)
            print(f"{suite} {len(queries)} exec={q['execute']} cpu={cpu_exec:.2f}/{cpu_opt:.2f}"
                  f" stable={stable} calib={calib * 1000:.1f}ms", file=sys.stderr)
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


# ── stage guard: no checkpoint shortcut for code that may write shared state ─────────────
#
# The checkpoint compares the optimized tree, not global state. If a changed function (old or
# new version; for a nested def, its outermost enclosing def, so closures resolve) can write
# state that outlives its call, the query goes the whole way (full re-run, as r17):
#   * `global` / `nonlocal`, setattr / delattr / exec / eval / globals() / vars() / __import__
#   * a store, del, augmented assignment or mutating method call (append, update, set, ...)
#     whose base is a non-local object: a module-level name, builtin or import, `cls`, `self`
#     of a Dialect (instances are cached and shared; parser / generator / tokenizer / scope /
#     schema objects are made per call), or a local name bound to one of those by alias
#     (x = MOD.TABLE, from m import X, for x in GLOBAL..., with GLOBAL as x, x = M.get(...));
#     exp.* builders and .copy() make new objects
# Parameters and names bound from them / from calls of plain functions count as query-local:
# the objects the optimizer works on are the query's tree and scopes, compared by the digest.
# Not covered (documented gap, as r17's cross-query reuse): an unchanged callee that writes
# global state on the changed function's behalf.

_MUTATORS = {"append", "extend", "insert", "pop", "popitem", "remove", "clear", "update",
             "setdefault", "add", "discard", "sort", "reverse", "__setitem__", "__delitem__",
             "__setattr__", "__delattr__", "difference_update", "intersection_update",
             "symmetric_difference_update", "appendleft", "extendleft", "popleft", "rotate",
             "move_to_end", "cache_clear", "set", "replace", "append_comments", "add_comments",
             "pop_comments", "set_meta"}
_BAD_CALLS = {"setattr", "delattr", "exec", "eval", "globals", "vars", "__import__", "locals"}


def _defs(tree):
    """{(qualname, occurrence): (def node, outermost enclosing def node)}."""
    out, seen = {}, {}

    def visit(node, prefix, outer):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                name = prefix + (child.name if not isinstance(child, ast.Lambda) else "<lambda>")
                k = seen.get(name, 0)
                seen[name] = k + 1
                out[(name, k)] = (child, outer or child)
                visit(child, name + ".<locals>.", outer or child)
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".", outer)
            else:
                visit(child, prefix, outer)

    visit(tree, "", None)
    return out


def _root(node):
    """Base name of a.b[c].d(...).e chains (through method calls, not plain-function calls)."""
    while True:
        if isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred)):
            node = node.value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = node.func
            # sqlglot.expressions builders (exp.column(...), exp.Alias(...)) and .copy() make
            # new objects
            if f.attr in ("copy", "deepcopy") or (isinstance(f.value, ast.Name) and f.value.id == "exp"):
                return node
            node = f.value
        else:
            return node


def _writes_shared(fn, self_shared: bool) -> str | None:
    """Why `fn` (whole subtree) may write state that outlives its call, or None."""
    nodes = list(ast.walk(fn))
    params: set[str] = set()
    for n in nodes:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = n.args
            params.update(p.arg for p in a.posonlyargs + a.args + a.kwonlyargs)
            params.update(p.arg for p in (a.vararg, a.kwarg) if p is not None)
    shared_self = "self" if self_shared else None

    # local bindings: name → [value expressions] (None: an import, i.e. a shared module object)
    binds: dict[str, list] = {}

    def bind(target, value):
        for x in ast.walk(target):
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                binds.setdefault(x.id, []).append(value if isinstance(target, ast.Name) else value)

    for n in nodes:
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            return "global / nonlocal"
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                binds.setdefault((a.asname or a.name).split(".")[0], []).append(None)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                bind(t, n.value)
        elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and n.value is not None:
            bind(n.target, n.value)
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            bind(n.target, n.iter)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None:
                    bind(it.optional_vars, it.context_expr)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            binds.setdefault(n.name, []).append(ast.Constant(None))
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
            binds.setdefault(n.name, []).append(ast.Constant(None))

    shared: dict[str, bool] = {}

    def is_shared(name: str, depth: int = 0) -> bool:
        if name in ("cls",) or name == shared_self:
            return True
        if name in params and name not in binds:
            return False
        if name not in binds:
            return True  # module-level name, builtin or closure of the module
        if name in shared:
            return shared[name]
        shared[name] = False  # cycles: assume local until shown otherwise
        res = False
        for v in binds[name]:
            if v is None:
                res = True  # import
                break
            r = _root(v)
            if isinstance(r, ast.Name) and depth < 20 and r.id != name and is_shared(r.id, depth + 1):
                res = True
                break
        shared[name] = res
        return res

    def target_shared(t) -> str | None:
        if isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                why = target_shared(e)
                if why:
                    return why
            return None
        if isinstance(t, ast.Name):
            return None
        r = _root(t)
        if isinstance(r, ast.Name):
            return f"writes {r.id}" if is_shared(r.id) else None
        return "writes through an expression" if not isinstance(r, ast.Call) else None

    for n in nodes:
        targets = []
        if isinstance(n, ast.Assign):
            targets = n.targets
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)) and (n.value is not None or isinstance(n, ast.AugAssign)):
            targets = [n.target]
        elif isinstance(n, ast.Delete):
            targets = n.targets
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            targets = [n.target]
        elif isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id in _BAD_CALLS:
                return f"calls {f.id}"
            if isinstance(f, ast.Attribute) and f.attr in _MUTATORS:
                r = _root(f.value)
                if isinstance(r, ast.Name) and is_shared(r.id):
                    return f"{f.attr}() on {r.id}"
        for t in targets:
            why = target_shared(t)
            if why:
                return why
    return None


def stage_guard(root: str, jobs) -> str | None:
    """None when every changed function (old and new version) of the jobs passes
    _writes_shared, else the reason (→ no checkpoint shortcut for this commit)."""
    for path, new_text, _ in jobs:
        with open(os.path.join(root, path), encoding="utf-8") as fh:
            old_text = fh.read()
        old_defs, new_defs = _defs(ast.parse(old_text)), _defs(ast.parse(new_text))
        for k in set(old_defs) | set(new_defs):
            o, n = old_defs.get(k), new_defs.get(k)
            if o is not None and n is not None and ast.dump(o[0]) == ast.dump(n[0]):
                continue
            for d in (o, n):
                if d is None:
                    continue
                # Dialect instances are cached and shared; parser / generator / tokenizer /
                # scope / schema objects are made per call
                why = _writes_shared(d[1], path.startswith("sqlglot/dialects/") or "Dialect" in k[0])
                if why:
                    return f"{k[0]}: {why}"
    return None


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
                    x = (suite, i, kind == "optimized", "full", False, False, False)
                    win = ph.get("win")
                    if (STAGE and getattr(self, "stage_ok", False) and kind == "execute"
                            and q["execute"] and win and q["outs"].get("d_opt")):
                        wl = {}
                        for w in win.values():
                            for f, ls in w.items():
                                wl.setdefault(f, set()).update(ls)
                        glue = {f: sorted(set(ls) - wl.get(f, set())) for f, ls in ph["exec"].items()}
                        later = any(_hit(win[w], spans) for w in ("prep", "plan", "post"))
                        if not later and not _hit(glue, spans):
                            x = (suite, i, False, "stage", _hit(ph["parse"], spans),
                                 _hit(win["optb"], spans), _hit(ph["duck"], spans))
                    todo.append(x)
                    cpu += self._cpu(x)
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
        deadline = time.monotonic() + budget * 2 + 2
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
        self.stage_ok, self.stage_why = False, "off"
        if STAGE:
            try:
                why = stage_guard(str(self.sel.sqlglot), jobs)
            except Exception as e:  # noqa: BLE001
                why = type(e).__name__
            self.stage_ok = why is None
            self.stage_why = why
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
            return set(), f"subtest: keep (est {cpu:.1f}s CPU for {sum(map(len, per.values()))} queries{"" if getattr(self, "stage_ok", True) else "; no stage: " + str(self.stage_why)})"
        if not todo:
            return chosen, f"subtest: {sorted(chosen)} dropped (no query runs a touched statement)"
        try:
            ok = self.verify(jobs, todo, min(BUDGET_S / SCORER_SLOWDOWN, 2.5 * est + 1.0 + FALLBACK_CAP))
        except Exception as e:  # noqa: BLE001
            return set(), f"subtest: keep ({type(e).__name__})"
        if not ok:
            return set(), f"subtest: keep (verification of {len(todo)} queries failed, {self.last_child:.2f}s)"
        return chosen, (f"subtest: {sorted(chosen)} dropped ({len(todo)} queries re-run,"
                        f" {self.last_child:.2f}s)")

    def _cpu(self, x):
        suite, i, opt, mode, parse_hit, optb_hit, duck_hit = x
        q = self.meta()["suites"][suite][i]
        if mode == "stage":
            # parse / duck transpile: a few ms; optb from the checkpoint: its pinned CPU
            return 0.02 + (q["outs"]["cpu_stage"] if optb_hit else 0.0)
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
    import sqlglot.executor as E
    from sqlglot import parse_one as _parse_one, transpile as _transpile
    full = []
    fb_cpu = 0.0  # pinned CPU of the queries re-run whole after a checkpoint diverged
    # checkpoint-verified queries first (cheap; a divergence ends the child early)
    for suite, i, opt, mode, parse_hit, optb_hit, duck_hit in todo:
        if mode != "stage":
            full.append((suite, i, opt))
            continue
        q = meta["suites"][suite][i]
        signal.setitimer(signal.ITIMER_PROF, 3 * q["outs"]["cpu_stage"] + 0.8)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                if duck_hit and _transpile(q["sql"], write="duckdb")[0] != q["outs"]["duck"]:
                    return False
                if parse_hit:
                    e = pickle.loads(pickle.dumps(_parse_one(q["sql"])))
                    if strict_digest(e) != q["outs"]["d_parse"]:
                        fb_cpu += q["cpu_exec"]
                        if not FALLBACK and fb_cpu > FALLBACK_CAP: return False
                        full.append((suite, i, opt))  # parse differs: run the whole query
                        continue
                if optb_hit:
                    e = pickle.loads(q["outs"]["pexpr"])
                    sch = E.ensure_schema(meta["schemas"][suite], dialect=None)
                    oe = E.optimize(e, sch, leave_tables_isolated=True, dialect=None)
                    oe.sql(pretty=True)
                    if strict_digest(oe) != q["outs"]["d_opt"]:
                        if os.environ.get("SUBTEST_DEBUG"):
                            print("subtest stage diff:", suite, i, file=sys.stderr)
                        fb_cpu += q["cpu_exec"]
                        if not FALLBACK and fb_cpu > FALLBACK_CAP: return False
                        full.append((suite, i, opt))  # optimized tree differs: run it all
                        continue
            except Exception:  # noqa: BLE001
                return False
    tables: dict = {}
    with zipfile.ZipFile(store) as zf:
        for suite, i, opt in full:
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
