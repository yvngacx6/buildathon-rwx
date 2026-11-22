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
RULES_ON = os.environ.get("BR_SUBTEST_RULES", "1") != "0"  # moon-22 per-rule checkpoints
RULES_BUILD = os.environ.get("BR_SUBTEST_RULES_BUILD", "1") != "0"
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


# ── moon-22: per-optimizer-rule checkpoints inside the optb window ───────────
#
# optimize() = glue (ensure_schema, maybe_parse(copy=True), the rule loop) + RULES[0..13];
# execute() then calls .sql(pretty=True) (step 14). Boundary k = the tree rule k receives
# (k = 1..14; 14 = the tree .sql() receives). A touched line's "first step" is the lowest step
# whose window runs it (glue = -1). If every touched optb line first runs in step >= k, rules
# 0..k-1 run untouched code on the same input as at build → the real test's tree at boundary
# k is the stored one (checked seed-invariant, see seed_digests). Then
#   optimize(ck_k, fresh schema, rules=RULES[k:], copy=False) + .sql(pretty=True)
# under the new code must give the pinned optimized digest d_opt (as moon-05's optb check).
# Usable boundaries are proven at build: the pinned code from the unpickled checkpoint with a
# fresh schema reproduces d_opt (covers serde round-trip loss and schema cache state).

NSTEPS = 15  # 14 rules + the debug .sql()


def rule_steps(expr, schema_, cov_factory=None, keep=False, timer=False):
    """optimize(expr, schema_, leave_tables_isolated=True, dialect=None) mirrored step by step
    (same objects, same kwargs glue), then .sql(pretty=True).
    → (covs {step: cov}, boundary pickles {k: bytes} | None, boundary digests {k: str},
       final digest, cpu {step: s})"""
    import inspect
    import sqlglot.optimizer.optimizer as O
    covs, cks, digs, cpu = {}, ({} if keep else None), {}, {}

    def step(idx, fn):
        cov = cov_factory() if cov_factory else None
        t0 = time.process_time()
        if cov:
            cov.start()
        try:
            return fn()
        finally:
            if cov:
                covs[idx] = cov.stop()
            cpu[idx] = time.process_time() - t0

    def glue():
        sch = O.ensure_schema(schema_, dialect=None)
        kw = {"db": None, "catalog": None, "schema": sch, "dialect": None, "sql": None,
              "isolate_tables": True, "quote_identifiers": False, "leave_tables_isolated": True}
        return O.exp.maybe_parse(expr, dialect=None, copy=True), kw

    opt, kw = step(-1, glue)
    for k, rule in enumerate(O.RULES):
        if k:
            digs[k] = strict_digest(opt)
            if keep:
                cks[k] = ck_dumps(opt)
        params = inspect.getfullargspec(rule).args
        rkw = {p: kw[p] for p in params if p in kw}
        opt = step(k, lambda: rule(opt, **rkw))
    digs[NSTEPS - 1] = strict_digest(opt)
    if keep:
        cks[NSTEPS - 1] = ck_dumps(opt)
    step(NSTEPS - 1, lambda: opt.sql(pretty=True))
    return covs, cks, digs, strict_digest(opt), cpu


_SLOTS: dict = {}


def ck_dumps(tree) -> bytes:
    """Lossless pickle of an expression tree: every slot / __dict__ attribute of every node as
    is (args with None values, parent links, _type, _meta, _hash, sharing), bypassing Expr's
    serde-based __reduce__ (which drops None-valued args). Loading runs no sqlglot code."""
    import copyreg
    from sqlglot.expressions import Expr

    class P(pickle.Pickler):
        def reducer_override(self, obj):
            if not isinstance(obj, Expr):
                return NotImplemented
            cls = type(obj)
            slots = _SLOTS.get(cls)
            if slots is None:
                slots = _SLOTS[cls] = tuple(dict.fromkeys(
                    n for c in cls.__mro__ for n in c.__dict__.get("__slots__", ())
                    if n not in ("__dict__", "__weakref__")))
            st = {n: getattr(obj, n) for n in slots if hasattr(obj, n)}
            # copyreg.__newobj__ → NEWOBJ (cls.__new__(cls)): no reference to this module
            return copyreg.__newobj__, (cls,), (getattr(obj, "__dict__", None) or None, st)

    buf = io.BytesIO()
    P(buf, protocol=pickle.HIGHEST_PROTOCOL).dump(tree)
    return buf.getvalue()


def from_checkpoint(ck: bytes, schema_dict: dict, k: int):
    """What the run-time child does for boundary k: → optimized tree (after .sql())."""
    import sqlglot.executor as E
    import sqlglot.optimizer.optimizer as O
    e = pickle.loads(ck)
    sch = E.ensure_schema(schema_dict, dialect=None)
    oe = E.optimize(e, sch, leave_tables_isolated=True, dialect=None, rules=O.RULES[k:], copy=False)
    oe.sql(pretty=True)
    return oe


def seed_digests(root: Path) -> dict:
    """Boundary digests of every executed TPC query under this process's hash seed."""
    sys.path.insert(0, str(root))
    sys.setrecursionlimit(1000)
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs
    from sqlglot import parse_one
    import sqlglot.executor as E
    schemas = {"tpch": TPCH_SCHEMA, "tpcds": TPCDS_SCHEMA}
    out = {}
    for suite in SUITES:
        fixture = SUITES[suite] + ("tpc-h.sql" if suite == "tpch" else "tpc-ds.sql")
        for i, (qmeta, sql, _) in enumerate(load_sql_fixture_pairs(fixture)):
            if not qmeta.get("execute"):
                continue
            try:
                e = pickle.loads(pickle.dumps(parse_one(sql)))
                sch = E.ensure_schema(schemas[suite], dialect=None)
                _, _, digs, dfin, _ = rule_steps(e, sch)
                out[(suite, i)] = (digs, dfin)
            except Exception:  # noqa: BLE001
                out[(suite, i)] = None
    return out


def _seed_runs(root: Path) -> dict:
    """{(suite, i): [boundary digests per seed]} for seeds 0 (this build), 1, 2 — each in its
    own interpreter (the hash seed is fixed at start-up)."""
    import subprocess
    import tempfile
    runs = {}
    for seed in ("1", "2"):
        with tempfile.NamedTemporaryFile(suffix=".pkl") as tf:
            env = dict(os.environ, PYTHONHASHSEED=seed)
            subprocess.run([sys.executable, "-m", "selector.subtest", "seeddig", str(root), tf.name],
                           cwd=str(REPO), env=env, check=True)
            with open(tf.name, "rb") as fh:
                got = pickle.load(fh)
        for key, v in got.items():
            runs.setdefault(key, []).append(v)
    return runs


def _build_rules(outs, optb_cov, pexpr, schema_dict, prefix, seed_runs) -> None:
    """Per-step coverage (first step per line), boundary checkpoints, which are usable, and
    the CPU to verify from each. Runs inside the query's traced fork."""
    import sqlglot.executor as E

    def traced_steps():
        e = pickle.loads(pexpr)
        sch = E.ensure_schema(schema_dict, dialect=None)
        covs, cks, digs, dfin, _ = rule_steps(e, sch, lambda: _Cov(prefix), keep=True)
        cov = _Cov(prefix)
        cov.start()
        for ck in cks.values():
            pickle.loads(ck)
        ckload = cov.stop()
        return covs, cks, digs, dfin, ckload

    covs, cks, digs, dfin, ckload = _in_fork(traced_steps)
    if dfin != outs["d_opt"]:
        raise RuntimeError("rule mirror differs from optimize()")
    # lines execute()'s optb window runs that no mirrored step runs: optimize()'s own glue
    seen: dict[str, set[int]] = {}
    for c in covs.values():
        for f, ls in c.items():
            seen.setdefault(f, set()).update(ls)
    glue = dict(covs[-1])
    for f, ls in optb_cov.items():
        extra = set(ls) - seen.get(f, set())
        if extra:
            glue[f] = sorted(set(glue.get(f, ())) | extra)
    covs[-1] = glue
    # the checkpoint loads run serde under the new code: treat as glue
    for f, ls in ckload.items():
        covs[-1][f] = sorted(set(covs[-1].get(f, ())) | set(ls))
    outs["fs"] = _first_steps(covs)

    def timed_steps():
        e = pickle.loads(pexpr)
        sch = E.ensure_schema(schema_dict, dialect=None)
        return rule_steps(e, sch)[2:]

    digs2, dfin2, cpu = _in_fork(timed_steps)
    if digs2 != digs or dfin2 != dfin:
        raise RuntimeError("rule steps not reproducible")
    ok, cpu_from = [], {}
    for k in range(1, NSTEPS):
        # the same tree at boundary k under other hash seeds (the real test's seed is random)
        if not seed_runs or len(seed_runs) != 2 or any(
                r is None or r[0].get(k) != digs[k] or r[1] != dfin for r in seed_runs):
            continue

        def check(k=k):
            t0 = time.process_time()
            oe = from_checkpoint(cks[k], schema_dict, k)
            t1 = time.process_time()
            return strict_digest(oe), t1 - t0

        try:
            d, c = _in_fork(check)
        except RuntimeError:
            continue
        if d == dfin:
            ok.append(k)
            cpu_from[k] = c
    outs["ck_ok"] = ok
    outs["cpu_from"] = cpu_from
    outs["cpu_steps"] = cpu
    outs["_cks"] = {k: cks[k] for k in ok}


def _first_steps(covs: dict) -> dict:
    """{file: (sorted lines tuple, first-step tuple)} from {step: {file: lines}}."""
    first: dict[str, dict[int, int]] = {}
    for idx in sorted(covs, reverse=True):
        for f, ls in covs[idx].items():
            d = first.setdefault(f, {})
            for ln in ls:
                d[ln] = idx
    return {f: (tuple(sorted(d)), bytes(i + 1 for _, i in sorted(d.items())))
            for f, d in first.items()}


def _min_step(fs: dict, spans: dict):
    """Lowest first step over the touched lines (None: no touched optb line)."""
    import bisect
    best = None
    for path, ss in spans.items():
        got = fs.get(path)
        if not got:
            continue
        lines, idx = got
        for first, last in ss:
            j = bisect.bisect_left(lines, first)
            while j < len(lines) and lines[j] <= last:
                v = idx[j] - 1
                best = v if best is None else min(best, v)
                j += 1
    return best


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
    ckdata: dict[str, bytes] = {}
    seeds = _seed_runs(root) if RULES_BUILD else {}
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
                        t0 = time.process_time()
                        e4 = pickle.loads(pexpr)
                        import sqlglot.executor as E
                        sch = E.ensure_schema(schemas[suite], dialect=None)
                        oe = E.optimize(e4, sch, leave_tables_isolated=True, dialect=None)
                        oe.sql(pretty=True)
                        d2 = strict_digest(oe)
                        cpu_stage = time.process_time() - t0
                        return d2, cpu_stage
                    covs, d_opt, dres = _in_fork(windows)
                    d2, cpu_stage = _in_fork(stage_timed)
                    phases["win"] = covs
                    outs["d_opt"] = d_opt if (dres == outs["exec"] and d2 == d_opt) else None
                    outs["cpu_stage"] = cpu_stage
                    if RULES_BUILD and outs["d_opt"]:
                        try:
                            _build_rules(outs, covs["optb"], pexpr, schemas[suite], prefix,
                                         seeds.get((suite, len(queries))))
                        except RuntimeError:
                            for k in ("fs", "ck_ok", "cpu_from", "_cks"):
                                outs.pop(k, None)
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
                for k, data in outs.pop("_cks", {}).items():
                    ckdata[f"ck/{suite}/{len(queries)}/{k}"] = data
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
        for name, data in ckdata.items():
            zf.writestr(name, data)
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
                    x = (suite, i, kind == "optimized", "full", False, False, False, 0)
                    win = ph.get("win")
                    if STAGE and kind == "execute" and q["execute"] and win and q["outs"].get("d_opt"):
                        wl = {}
                        for w in win.values():
                            for f, ls in w.items():
                                wl.setdefault(f, set()).update(ls)
                        glue = {f: sorted(set(ls) - wl.get(f, set())) for f, ls in ph["exec"].items()}
                        later = any(_hit(win[w], spans) for w in ("prep", "plan", "post"))
                        if not later and not _hit(glue, spans):
                            optb_hit = _hit(win["optb"], spans)
                            k = 0
                            fs = q["outs"].get("fs")
                            if RULES_ON and optb_hit and fs and q["outs"].get("ck_ok"):
                                # moon-22: start at the last usable boundary before the first
                                # step that runs a touched line
                                first = _min_step(fs, spans)
                                if first is not None and first > 0:
                                    k = max((b for b in q["outs"]["ck_ok"] if b <= first), default=0)
                            x = (suite, i, False, "stage", _hit(ph["parse"], spans),
                                 optb_hit, _hit(ph["duck"], spans), k)
                    todo.append(x)
                    cpu += self._cpu(x)
            out[n] = todo
        return out, cpu

    def verify(self, jobs, todo: list, budget: float) -> bool:
        """Run the queries in a forked child with the changed code swapped in."""
        meta = self.meta()
        root = str(self.sel.sqlglot)
        _warm(root)
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
            ok = self.verify(jobs, todo, min(BUDGET_S / SCORER_SLOWDOWN, 2.5 * est + 1.0 + FALLBACK_CAP))
        except Exception as e:  # noqa: BLE001
            return set(), f"subtest: keep ({type(e).__name__})"
        if not ok:
            return set(), f"subtest: keep (verification of {len(todo)} queries failed, {self.last_child:.2f}s)"
        nr = sum(1 for x in todo if x[3] == "stage" and x[5] and x[7])
        return chosen, (f"subtest: {sorted(chosen)} dropped ({len(todo)} queries re-run,"
                        f" {self.last_child:.2f}s) [rule checkpoints {nr}, est {est:.2f}s]")

    def _cpu(self, x):
        suite, i, opt, mode, parse_hit, optb_hit, duck_hit, k = x
        q = self.meta()["suites"][suite][i]
        if mode == "stage":
            # parse / duck transpile: a few ms; optb from the checkpoint: its pinned CPU
            if optb_hit and k:
                return 0.02 + q["outs"]["cpu_from"][k]
            return 0.02 + (q["outs"]["cpu_stage"] if optb_hit else 0.0)
        return q["cpu_opt"] if opt else q["cpu_exec"]


def _swap(root: str, jobs) -> str | None:
    """Swap the changed functions' code (as diffexec/replay.py). → bail reason or None."""
    import functools
    import importlib
    import types
    from diffexec import serial
    from diffexec.replay import _walk
    swaps = {}
    for path, new_text, _ in jobs:
        filename = os.path.join(root, path)
        with open(filename, "rb") as fh:
            old_top = compile(fh.read(), filename, "exec", dont_inherit=True)
        new_top = compile(new_text.encode(), filename, "exec", dont_inherit=True)
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
    zck = zipfile.ZipFile(store)
    for suite, i, opt, mode, parse_hit, optb_hit, duck_hit, k in todo:
        if mode != "stage":
            full.append((suite, i, opt))
            continue
        q = meta["suites"][suite][i]
        signal.setitimer(signal.ITIMER_PROF, 3 * (q["outs"]["cpu_from"][k] if k else q["outs"]["cpu_stage"]) + 0.8)
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
                if optb_hit and k:
                    # moon-22: from the stored tree at boundary k, rules k.. under the new code
                    oe = from_checkpoint(zck.read(f"ck/{suite}/{i}/{k}"), meta["schemas"][suite], k)
                    if strict_digest(oe) != q["outs"]["d_opt"]:
                        if os.environ.get("SUBTEST_DEBUG"):
                            print("subtest rule diff:", suite, i, k, file=sys.stderr)
                        fb_cpu += q["cpu_exec"]
                        if not FALLBACK and fb_cpu > FALLBACK_CAP: return False
                        full.append((suite, i, opt))
                        continue
                elif optb_hit:
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
                if os.environ.get("SUBTEST_DEBUG"):
                    import traceback
                    traceback.print_exc(file=sys.__stderr__)
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
    elif len(sys.argv) == 4 and sys.argv[1] == "seeddig":
        with open(sys.argv[3], "wb") as fh:
            pickle.dump(seed_digests(Path(sys.argv[2])), fh)
    else:
        sys.exit("usage: python -m selector.subtest build SQLGLOT_ROOT OUT.zip")
