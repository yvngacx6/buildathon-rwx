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
TAIL = os.environ.get("BR_SUBTEST_TAIL", "1") != "0"  # r39: Plan + executor from the final tree
SHARE = 0.6  # spend at most this share of what dropping saves
# r39 net gate: a subset the SHARE gate refuses is still verified when the scorer-charged
# estimate stays under NET_SHARE x its saving (fail-fast: a divergent query ends the child).
NET_SHARE = float(os.environ.get("BR_SUBTEST_NET", "0.95"))
NET_EST_MAX = float(os.environ.get("BR_SUBTEST_NET_MAX", "13"))  # laptop s, whole child
NET_CPU_FACTOR = float(os.environ.get("BR_SUBTEST_NET_CPU_FACTOR", str(SCORER_SLOWDOWN)))
NET_CAP_SHARE = float(os.environ.get("BR_SUBTEST_NET_CAP_SHARE", "1.0"))  # child CPU cap / saving


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


# ── moon-25: code-generation checkpoint inside the executor ───────────────────────────────
#
# PythonExecutor turns every plan expression into Python source with its generator
# (self.generator.generate(expr) → str, then compile), and _register_subquery keys subquery
# plans by query.sql(). Both are pure expression → string steps: the executor reads only the
# returned string (the input is copied first, see Generator.generate). At build time the post
# window records, for every such call, a lossless pickle of the input (root parent dropped: the
# generator copies its input, so a copy without parent is all it sees), the output and the
# call depth; coverage is split into lines run inside these calls ("post_in") and outside.
# Run time: when the touched statements run in post only inside these calls (and nowhere in
# prep / plan / post outside / glue), and the optimized tree checkpoint is equal (or optb is
# untouched), the executor's run is identical to the pinned one up to the first generate call;
# replaying every call (same order, same generator construction path, one shared instance as
# the executor has, plus once more on a fresh instance per call) and getting the same strings
# and generator state proves by induction that it stays identical, i.e. the result is the
# pinned one. Changes to Generator.generate / Expr.copy / Expr.__deepcopy__ (which decide
# whether the generator sees the uncopied, parented input) never use this mode.

GEN = os.environ.get("BR_SUBTEST_GEN", "1") != "0"
_EXPR_SLOTS: dict = {}


def _slots(cls):
    s = _EXPR_SLOTS.get(cls)
    if s is None:
        s = _EXPR_SLOTS[cls] = tuple(dict.fromkeys(
            n for c in cls.__mro__ for n in c.__dict__.get("__slots__", ())
            if n not in ("__dict__", "__weakref__")))
    return s


def freeze_expr(root) -> bytes:
    """Lossless pickle of an expression (every slot: args with None values and key order,
    _hash, _type, _meta, comments, parent links), the root's parent dropped."""
    import copyreg
    from sqlglot.expressions import Expr

    class P(pickle.Pickler):
        def reducer_override(self, obj):
            if not isinstance(obj, Expr):
                return NotImplemented
            cls = type(obj)
            st = {n: getattr(obj, n) for n in _slots(cls) if hasattr(obj, n)}
            if obj is root:
                st["parent"] = None
            return copyreg.__newobj__, (cls,), (getattr(obj, "__dict__", None) or None, st)

    buf = io.BytesIO()
    P(buf, protocol=pickle.HIGHEST_PROTOCOL).dump(root)
    return buf.getvalue()


def _hashes(root) -> str:
    """Digest of the cached _hash of every node (copy() carries it over; eq / hash read it)."""
    from sqlglot.expressions import Expr
    h = hashlib.blake2b(digest_size=8)
    seen, stack = set(), [root]
    while stack:
        n = stack.pop()
        if not isinstance(n, Expr) or id(n) in seen:
            continue
        seen.add(id(n))
        h.update(repr(getattr(n, "_hash", None)).encode())
        t = getattr(n, "_type", None)
        if t is not None:
            stack.append(t)
        for v in n.args.values():
            stack.extend(v if type(v) is list else (v,))
    return h.hexdigest()


def gen_state(gen) -> str:
    """Digest of a generator instance's state (slots; closures by their cell contents, e.g. the
    _next_name counter; object addresses stripped)."""
    import re
    import types
    h = hashlib.blake2b(digest_size=16)
    for n in _slots(type(gen)):
        if not hasattr(gen, n):
            h.update(f"{n}:unset".encode())
            continue
        v = getattr(gen, n)
        if n == "_dispatch":
            r = repr(sorted((getattr(k, "__qualname__", str(k)), getattr(f, "__qualname__", str(f)))
                            for k, f in v.items()))
        elif isinstance(v, types.FunctionType):
            r = repr((v.__qualname__, [c.cell_contents for c in (v.__closure__ or ())]))
        elif n == "dialect":
            r = type(v).__qualname__
        else:
            r = repr(v)
        h.update(f"{n}:{re.sub(r' at 0x[0-9a-f]+', '', r)}".encode())
    return h.hexdigest()


def _depth() -> int:
    f, n = sys._getframe(1), 0
    while f is not None:
        n += 1
        f = f.f_back
    return n


def _at_depth(want: int, fn):
    """Call fn with at least `want` frames below it (a patched generator that recurses deeper
    must hit the recursion limit no later than it would in the recorded run)."""
    def pad(k):
        if k <= 0:
            return fn()
        return pad(k - 1)
    return pad(want - _depth())


class GenRecorder:
    """Hooks Generator.generate during the post window: records the executor generator's calls
    ("py") and query.sql() from _register_subquery ("sql"); any other call, a nested call, an
    exception or copy=False makes the query ineligible."""

    def __init__(self):
        self.calls = []
        self.bad = None
        self.depth = [0]
        self.py_gen = None

    def install(self):
        from sqlglot.generator import Generator
        from sqlglot.generators.python import PythonGenerator
        if PythonGenerator.generate is not Generator.generate:
            self.bad = "PythonGenerator overrides generate"
        self.orig = Generator.__dict__["generate"]
        rec = self

        def generate(gen, expression, copy=True):
            if rec.depth[0]:
                rec.bad = rec.bad or "nested generate"
                return rec.orig(gen, expression, copy)
            f1 = sys._getframe(1)
            if gen is rec.py_gen and f1.f_code.co_qualname == "PythonExecutor.generate":
                kind = "py"
            elif (f1.f_code.co_qualname == "Dialect.generate"
                  and f1.f_back.f_code.co_qualname.endswith(".sql")
                  and f1.f_back.f_back.f_code.co_qualname == "PythonExecutor._register_subquery"
                  and type(gen).__name__ == "Generator"):
                kind = "sql"
            else:
                kind = None
                rec.bad = rec.bad or f"generate from {f1.f_code.co_qualname}"
            if copy is not True:
                rec.bad = rec.bad or "copy=False"
            rec.depth[0] = 2  # recorder's own work: not coverage of the query
            try:
                fz = freeze_expr(expression) if kind else b""
            except Exception as e:  # noqa: BLE001
                rec.bad = rec.bad or f"freeze: {type(e).__name__}"
                fz = b""
            d_in = strict_digest(expression) + _hashes(expression) if kind else ""
            depth = _depth()
            rec.depth[0] = 1
            try:
                out = rec.orig(gen, expression, copy)
            except BaseException:
                rec.bad = rec.bad or "generate raised"
                raise
            finally:
                rec.depth[0] = 0
            if kind:
                rec.depth[0] = 2
                d_after = strict_digest(expression) + _hashes(expression)
                rec.depth[0] = 0
                if d_after != d_in:
                    rec.bad = rec.bad or "input mutated"
                rec.calls.append((kind, fz, d_in, depth, out))
            return out

        Generator.generate = generate

    def uninstall(self):
        from sqlglot.generator import Generator
        Generator.generate = self.orig


class _SplitCov:
    """Post-window coverage split into lines run inside the recorded generate calls and lines
    run outside. A location is DISABLEd only on an outside hit (final: it counts as outside);
    inside hits keep it enabled, so a later outside hit is still seen."""

    TOOL = 4

    def __init__(self, prefix: str, depth: list):
        self.prefix = prefix
        self.inside: dict[str, set[int]] = {}
        self.outside: dict[str, set[int]] = {}
        self.dep = depth

    def start(self):
        mon = sys.monitoring
        mon.use_tool_id(self.TOOL, "subtest")
        prefix, ins, outs, plen, dep = self.prefix, self.inside, self.outside, len(self.prefix), self.dep
        DISABLE = mon.DISABLE

        def line(code, lineno):
            fn = code.co_filename
            if fn.startswith(prefix):
                if dep[0] == 2:
                    return None
                if dep[0]:
                    ins.setdefault(fn[plen:], set()).add(lineno)
                    return None
                outs.setdefault(fn[plen:], set()).add(lineno)
            return DISABLE

        mon.register_callback(self.TOOL, mon.events.LINE, line)
        mon.set_events(self.TOOL, mon.events.LINE)
        mon.restart_events()

    def stop(self):
        mon = sys.monitoring
        mon.set_events(self.TOOL, 0)
        mon.register_callback(self.TOOL, mon.events.LINE, None)
        mon.free_tool_id(self.TOOL)
        out = {k: sorted(v) for k, v in self.outside.items()}
        inside_only = {}
        for k, v in self.inside.items():
            only = sorted(v - self.outside.get(k, set()))
            if only:
                inside_only[k] = only
        union = {k: sorted(set(out.get(k, ())) | set(self.inside.get(k, ())))
                 for k in set(out) | set(self.inside)}
        return union, out, inside_only


def gen_replay(calls, iso: bool = True) -> bool:
    """Replay recorded generate calls with the code in place; True when every output string and
    the final generator state equal the recorded ones."""
    import sqlglot.executor as E
    calls, state = calls
    py = E.PythonExecutor().generator
    for kind, fz, d_in, depth, out in calls:
        e = pickle.loads(fz)
        if strict_digest(e) + _hashes(e) != d_in:
            return False
        fn = (lambda e=e: py.generate(e)) if kind == "py" else (lambda e=e: e.sql())
        if _at_depth(depth, fn) != out:
            return False
    if gen_state(py) != state:
        return False
    if iso:
        # outputs must not depend on what earlier calls left in the shared instance (the
        # executor's call order follows set iteration over plan steps)
        for kind, fz, d_in, depth, out in calls:
            if kind != "py":
                continue
            e = pickle.loads(fz)
            g = E.PythonExecutor().generator
            if _at_depth(depth, lambda: g.generate(e)) != out:
                return False
    return True


def gen_forbidden(root: str) -> dict:
    """{relpath: lines} of the functions that decide whether the generator sees its input
    uncopied (with the pinned parent): touching them disables the generate checkpoint."""
    import inspect
    from sqlglot import exp
    from sqlglot.generator import Generator
    from sqlglot.dialects.dialect import Dialect
    out: dict[str, list[int]] = {}
    cls = type(exp.column("a"))
    fns = [Generator.generate, Dialect.generate]
    for c in cls.__mro__:
        for n in ("copy", "__deepcopy__", "sql"):
            if n in c.__dict__:
                fns.append(c.__dict__[n])
    for f in fns:
        f = inspect.unwrap(f)
        lines, first = inspect.getsourcelines(f)
        path = os.path.relpath(inspect.getsourcefile(f), root)
        out.setdefault(path, []).extend(range(first, first + len(lines)))
    return {k: sorted(set(v)) for k, v in out.items()}


def exec_windows(expr, schema, used, cov_factory=None, swap_until=None, gen_prefix=None):
    """execute(expr, schema=schema, tables=used) window by window.
    → ({window: coverage}, result digest[, generate calls])."""
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
    if gen_prefix is None:
        res = window("post", lambda: pickle.loads(pickle.dumps(E.PythonExecutor(tables=tables_).execute(plan))))
        return covs, d_opt, digest_table(res)
    rec = GenRecorder()
    cov = _SplitCov(gen_prefix, rec.depth)
    rec.install()
    cov.start()
    try:
        ex = E.PythonExecutor(tables=tables_)
        rec.py_gen = ex.generator
        res = pickle.loads(pickle.dumps(ex.execute(plan)))
    finally:
        covs["post"], covs["post_out"], covs["post_in"] = cov.stop()
        rec.uninstall()
    calls = None if rec.bad else (rec.calls, gen_state(ex.generator))
    return covs, d_opt, digest_table(res), (calls, rec.bad)


# ── build-machine calibration ────────────────────────────────────────────────────
#
# The image build may run on a busy box: CPU seconds measured there are inflated (shared cores,
# hyper-threads, caches), which made the budget refuse verifications it can afford (a store
# built at load ~20 had every query 2.4x too expensive). Each timed run is bracketed by a fixed
# pure-Python workload measured the same way, and the stored CPU is expressed in reference
# seconds: cpu * CALIB_REF / calib. CALIB_REF is the workload's CPU on the machine the budget
# constants were tuned on (laptop, idle); BR_SUBTEST_NOCALIB=1 stores raw seconds.

# rwx idle: cpu_exec(tpcds) / calib = 1004; rwx-big at load ~150: 966 (raw CPU 1.72x apart).
# 11.6 ms maps that ratio onto the laptop-idle 11.4 s the budget constants were tuned with.
CALIB_REF = 0.0116


_CALIB_SQL = ("SELECT a.x, SUM(b.y) AS s FROM a JOIN b ON a.k = b.k AND b.z > 3 "
              "WHERE a.x IN (SELECT c.x FROM c WHERE c.w < 10) GROUP BY a.x ORDER BY s DESC")
_CALIB_SCHEMA = {"a": {"x": "INT", "k": "INT"}, "b": {"y": "DOUBLE", "k": "INT", "z": "INT"},
                 "c": {"x": "INT", "w": "INT"}}


def _calib_work() -> int:
    """A small copy of what the timed runs do: optimize a query with pinned sqlglot (the stage
    part) and a hash join + aggregate over tuples with generated lambdas (the executor part)."""
    from sqlglot import parse_one
    from sqlglot.optimizer import optimize
    e = optimize(parse_one(_CALIB_SQL), _CALIB_SCHEMA)
    acc = len(e.sql())
    left = [(i, i % 101, f"v{i % 13}") for i in range(3000)]
    right = [(i % 101, float(i), i % 7) for i in range(3000)]
    index: dict = {}
    for r in right:
        index.setdefault(r[0], []).append(r)
    cond = eval("lambda l, r: r[2] > 3 and l[2] != 'v1'")  # noqa: S307
    groups: dict = {}
    for l in left:
        for r in index.get(l[1], ()):
            if cond(l, r):
                groups[l[2]] = groups.get(l[2], 0.0) + r[1]
    return acc + len(groups)


def _calib() -> float:
    """Median CPU of 5 runs of the calibration workload."""
    runs = []
    for _ in range(5):
        t = time.process_time()
        _calib_work()
        runs.append(time.process_time() - t)
    return sorted(runs)[2]


def _norm(cpu: float, c0: float, c1: float) -> float:
    if os.environ.get("BR_SUBTEST_NOCALIB") == "1" or not CALIB_REF:
        return cpu
    return cpu * CALIB_REF / max(1e-6, (c0 + c1) / 2)
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
    # moon-26: both interpreters side by side (independent; the build has spare cores)
    with tempfile.TemporaryDirectory() as td:
        procs = []
        for seed in ("1", "2"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            f = os.path.join(td, f"{seed}.pkl")
            procs.append((f, subprocess.Popen(
                [sys.executable, "-m", "selector.subtest", "seeddig", str(root), f],
                cwd=str(REPO), env=env)))
        for f, proc in procs:
            if proc.wait() != 0:
                raise subprocess.CalledProcessError(proc.returncode, "seeddig")
        for f, _ in procs:
            with open(f, "rb") as fh:
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
    if cpu_from:
        # one calibration per query (in its own fork, after the timed runs), as stage_timed
        c1 = _in_fork(_calib)
        cpu_from = {k: _norm(c, c1, c1) for k, c in cpu_from.items()}
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


_BUILD_ONE = None


def _build_one_entry(i):
    return _BUILD_ONE(_BUILD_ITEMS[i])


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
    if os.environ.get("BR_SUBTEST_PREIMPORT", "1") != "0":
        # r37: every module loaded first, as in the test worker (collection imported them):
        # else the first executed query imports athena / trino / presto / hive / mysql lazily
        # in every per-query fork, and their module / class-body lines (one-line lambdas
        # there are also reader sites) fill the exec phase as if the query ran them
        import importlib
        import pkgutil
        for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
            if m.name != "sqlglot.__main__":
                try:
                    importlib.import_module(m.name)
                except Exception:  # noqa: BLE001
                    pass
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
    meta["gen_forbid"] = gen_forbidden(str(root))
    ckdata: dict[str, bytes] = {}
    seeds = _seed_runs(root) if RULES_BUILD else {}
    tables = {s: make_tables(s) for s in SUITES}
    items = []
    for suite in SUITES:
        fixture = SUITES[suite] + ("tpc-h.sql" if suite == "tpch" else "tpc-ds.sql")
        for qi, (qmeta, sql, optimized) in enumerate(load_sql_fixture_pairs(fixture)):
            items.append((suite, qi, {"sql": sql, "optimized": optimized,
                                      "execute": bool(qmeta.get("execute"))}))

    global _BUILD_ITEMS
    _BUILD_ITEMS = items

    def one(item):
        """One query, everything in fresh forks of this process → (q, its checkpoints)."""
        suite, qi, q = item
        ckq = {}
        if True:

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
                        covs, d_opt, dres, gen = exec_windows(e3, schemas[suite], u, lambda: _Cov(prefix),
                                                              gen_prefix=prefix)
                        covs["prep"] = {k: sorted(set(v) | set(ft.get(k, ()))) for k, v in
                                        list(covs["prep"].items()) + list(ft.items())}
                        return covs, d_opt, dres, gen

                    def stage_timed():
                        t0 = time.process_time()
                        e4 = pickle.loads(pexpr)
                        import sqlglot.executor as E
                        sch = E.ensure_schema(schemas[suite], dialect=None)
                        oe = E.optimize(e4, sch, leave_tables_isolated=True, dialect=None)
                        oe.sql(pretty=True)
                        d2 = strict_digest(oe)
                        cpu_stage = time.process_time() - t0
                        c1 = _calib()  # after: it must not warm caches for the run
                        return d2, _norm(cpu_stage, c1, c1)
                    covs, d_opt, dres, (gcalls, gbad) = _in_fork(windows)
                    d2, cpu_stage = _in_fork(stage_timed)
                    phases["win"] = covs
                    outs["d_opt"] = d_opt if (dres == outs["exec"] and d2 == d_opt) else None
                    outs["cpu_stage"] = cpu_stage
                    if RULES_BUILD and outs["d_opt"]:
                        try:
                            _build_rules(outs, covs["optb"], pexpr, schemas[suite], prefix,
                                         seeds.get((suite, qi)))
                        except RuntimeError:
                            for k in ("fs", "ck_ok", "cpu_from", "_cks"):
                                outs.pop(k, None)
                    # moon-25: the recorded generate calls must replay with the pinned code
                    # (fresh fork, as the run-time child does) to be used
                    outs["gen"], outs["gen_cpu"], outs["gen_why"] = None, 0.0, gbad
                    if gcalls is not None and dres == outs["exec"]:
                        def gen_timed():
                            t0 = time.process_time()
                            ok = gen_replay(gcalls)
                            cpu = time.process_time() - t0
                            c1 = _calib()  # after: it must not warm caches for the run
                            return ok, _norm(cpu, c1, c1)
                        ok, gcpu = _in_fork(gen_timed)
                        if ok:
                            outs["gen"], outs["gen_cpu"] = pickle.dumps(gcalls), gcpu
                        else:
                            outs["gen_why"] = "pinned replay differs"
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
                c1 = _calib()  # after: it must not warm caches for the runs
                return a, b, _norm(t1 - t0, c1, c1), _norm(t2 - t1, c1, c1), c1

            try:
                phases, outs = _in_fork(traced)
                for k, data in outs.pop("_cks", {}).items():
                    ckq[f"ck/{suite}/{qi}/{k}"] = data
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
        return q, ckq

    # moon-26: queries are independent (each runs in fresh forks of this process, which runs
    # nothing between them), so a fork pool of BR_SUBTEST_JOBS workers runs them side by side;
    # results come back in fixture order.
    jobs = max(1, int(os.environ.get("BR_SUBTEST_JOBS", "1")))
    global _BUILD_ONE
    _BUILD_ONE = one
    if jobs > 1:
        import multiprocessing as mp
        pool = mp.get_context("fork").Pool(jobs)
        results = pool.imap(_build_one_entry, range(len(items)), chunksize=1)
    else:
        pool, results = None, map(_build_one_entry, range(len(items)))
    for (suite, qi, _), (q, ckq) in zip(items, results):
        ckdata.update(ckq)
        queries = meta["suites"].setdefault(suite, [])
        assert len(queries) == qi
        queries.append(q)
        print(f"{suite} {len(queries)} exec={q['execute']} cpu={q['cpu_exec']:.2f}/{q['cpu_opt']:.2f}"
              f" stable={q['stable']} calib={q['calib'] * 1000:.1f}ms"
              f" gen={len(g) if (g := q['outs'].get('gen')) else 0}B/{q['outs'].get('gen_cpu', 0):.3f}s"
              f" {q['outs'].get('gen_why') or ''}", file=sys.stderr)
    if pool is not None:
        pool.close()
        pool.join()
    _BUILD_ONE = None
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


def _outer_names(qual: str) -> list[str]:
    """"A.f.<locals>.g" → ["A.f.<locals>.g", "A.f"]: the def and every def enclosing it."""
    parts = qual.split(".<locals>.")
    return [".<locals>.".join(parts[:i]) for i in range(len(parts), 0, -1)]


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


def stage_guard(root: str, jobs, defaults: dict | None = None,
                skip: set | None = None) -> str | None:
    """None when every changed function (old and new version) of the jobs passes
    _writes_shared, else the reason (→ no checkpoint shortcut for this commit).
    r35: a def whose literal defaults change (`defaults`, select._default_changes: the job
    text is then the old source) keeps its body but may take other branches: it is checked
    as a changed function too.
    r37 `skip`: {(path, qualname)} of definition-time defs (metaclass methods,
    __init_subclass__) the caller showed no TPC test runs: they run at class creation, whose
    products the import-state diff holds."""
    for path, new_text, _ in jobs:
        with open(os.path.join(root, path), encoding="utf-8") as fh:
            old_text = fh.read()
        old_defs, new_defs = _defs(ast.parse(old_text)), _defs(ast.parse(new_text))
        for name, line, _params in (defaults or {}).get(path, ()):
            hits = [d for k, d in old_defs.items() if k[0].split(".")[-1] == name
                    and min([d[0].lineno] + [x.lineno for x in d[0].decorator_list]) == line]
            if len(hits) != 1:
                return f"{name}: default-changed def not found"
            why = _writes_shared(hits[0][1], path.startswith("sqlglot/dialects/") or "Dialect" in name)
            if why:
                return f"{name}: {why}"
        for k in set(old_defs) | set(new_defs):
            o, n = old_defs.get(k), new_defs.get(k)
            if o is not None and n is not None and ast.dump(o[0]) == ast.dump(n[0]):
                continue
            if skip and any((path, q) in skip for q in _outer_names(k[0])):
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
                    x = (suite, i, kind == "optimized", "full", False, False, False, 0, False)
                    win = ph.get("win")
                    if (STAGE and getattr(self, "stage_ok", False) and kind == "execute"
                            and q["execute"] and win and q["outs"].get("d_opt")):
                        wl = {}
                        for w in win.values():
                            for f, ls in w.items():
                                wl.setdefault(f, set()).update(ls)
                        glue = {f: sorted(set(ls) - wl.get(f, set())) for f, ls in ph["exec"].items()}
                        later = any(_hit(win[w], spans) for w in ("prep", "plan"))
                        gen_hit = False
                        if _hit(win["post"], spans):
                            # moon-25: post touched only inside the recorded generate calls
                            if (GEN and q["outs"].get("gen") and "post_out" in win
                                    and not _hit(win["post_out"], spans)
                                    and not _hit(meta.get("gen_forbid") or {"?": [0]}, spans)):
                                gen_hit = True
                            else:
                                later = True
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
                                 optb_hit, _hit(ph["duck"], spans), k, gen_hit)
                        elif (TAIL and NSTEPS - 1 in (q["outs"].get("ck_ok") or ())
                                and not _hit(win["prep"], spans) and not _hit(win["optb"], spans)
                                and not _hit(glue, spans) and not _hit(ph["parse"], spans)
                                and not _hit(ph["duck"], spans)):
                            # r39: only Plan / PythonExecutor run a touched line: their input
                            # (the optimized tree, the ensured tables) is the pinned one, so
                            # run just them from the stored final tree
                            x = (suite, i, False, "tail", False, False, False, NSTEPS - 1, False)
                    todo.append(x)
                    cpu += self._cpu(x)
            out[n] = todo
        return out, cpu

    def _defaults(self) -> dict:
        """select.Selector._defaults: {path: [(def name, first line, {param: (old, new)})]}
        of the default-only files (their jobs carry the old source)."""
        return dict(getattr(self.sel, "_defaults", None) or {})

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
                ok = _child(root, jobs, todo, meta, self.store, budget, self._defaults())
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
        # The CPU itimers bound the child; the wall deadline only guards a stuck child (r26:
        # budget * 2 + 2 cut verifications short on a loaded machine), bounded by the run's
        # ALL_PHASE deadline (moon-17 / r30: diffexec.wall_guard).
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
        self.stage_ok, self.stage_why = False, "off"
        if STAGE:
            try:
                why = (None if os.environ.get("BR_SUBTEST_GUARD") == "0"  # experiments only
                       else stage_guard(str(self.sel.sqlglot), jobs, self._defaults()))
            except Exception as e:  # noqa: BLE001
                why = type(e).__name__
            self.stage_ok = why is None
            self.stage_why = why
        try:
            # r37: the store is traced with every module loaded first; a touched line the
            # test ran outside every recorded query phase (warm-up, lazy import) → keep
            if self.unrecorded(spans, cands):
                return set(), "subtest: keep (a touched line ran outside the recorded queries)"
            got = self.plan(spans, cands)
        except Exception as e:  # noqa: BLE001
            return set(), f"subtest: keep ({type(e).__name__})"
        if got is None:
            return set(), "subtest: keep (set-up touched / unknown / unstable)"
        per, cpu = got
        # r26: every subset of the (at most 3) candidates, best net saving. Greedy cheapest-
        # first never dropped test_execute_tpch / test_optimized_tpch next to tpcds: alone
        # they save nothing (tpcds is the long pole), and they were not reconsidered once
        # tpcds was chosen.
        import itertools
        best = (0.0, set(), [], 0.0)
        nums = sorted(per)
        for r in range(1, len(nums) + 1):
            for subset in itertools.combinations(nums, r):
                todo = []
                for n in subset:
                    todo.extend(x for x in per[n] if x not in todo)
                est = sum(self._cpu(x) for x in todo) + (0.3 if todo else 0.0)  # fork, swap, tables
                spend = SCORER_SLOWDOWN * est
                gain = benefit_of(set(subset))
                if gain > 0 and spend <= min(BUDGET_S, SHARE * gain / 1000):
                    net = gain / 1000 - spend
                    if net > best[0]:
                        best = (net, set(subset), todo, est)
        _, chosen, todo, est = best
        net_gate = False
        if not chosen and NET_SHARE > 0:
            best = (0.0 if NET_SHARE < 100 else -1e9, set(), [], 0.0)  # >= 100: measuring only
            for r in range(1, len(nums) + 1):
                for subset in itertools.combinations(nums, r):
                    todo = []
                    for n in subset:
                        todo.extend(x for x in per[n] if x not in todo)
                    est = sum(self._cpu(x) for x in todo) + (0.3 if todo else 0.0)
                    spend = NET_CPU_FACTOR * est
                    gain = benefit_of(set(subset))
                    if gain > 0 and est <= NET_EST_MAX and spend <= NET_SHARE * gain / 1000:
                        net = gain / 1000 - spend
                        if net > best[0]:
                            best = (net, set(subset), todo, est)
            _, chosen, todo, est = best
            net_gate = bool(chosen)
        if not chosen:
            return set(), f"subtest: keep (est {cpu:.1f}s CPU for {sum(map(len, per.values()))} queries{"" if getattr(self, "stage_ok", True) else "; no stage: " + str(self.stage_why)})"
        if not todo:
            return chosen, f"subtest: {sorted(chosen)} dropped (no query runs a touched statement)"
        try:
            cap = min(BUDGET_S / SCORER_SLOWDOWN, 2.5 * est + 1.0 + FALLBACK_CAP)
            if net_gate:
                # past the saving every outcome loses (the child's CPU is charged 2x)
                # (raw CPU where it runs: on the scorer that is what is charged)
                cap = min(2.5 * est + 1.0 + FALLBACK_CAP,
                          NET_CAP_SHARE * benefit_of(chosen) / 1000 if NET_SHARE < 100 else 99)
            ok = self.verify(jobs, todo, cap)
        except Exception as e:  # noqa: BLE001
            return set(), f"subtest: keep ({type(e).__name__})"
        if not ok:
            return set(), f"subtest: keep (verification of {len(todo)} queries failed, {self.last_child:.2f}s, est {est:.2f}, cap {cap:.2f})"
        nr = sum(1 for x in todo if x[3] == "stage" and x[5] and x[7])
        return chosen, (f"subtest: {sorted(chosen)} dropped{' [net gate]' if net_gate else ''} ({len(todo)} queries re-run,"
                        f" {self.last_child:.2f}s, est {est:.2f}, cap {cap:.2f}) [rule checkpoints {nr}]")

    # ── r37: class / module-body edits, verified on the patched import ──────────────────

    def unrecorded(self, spans: dict, cands: set[int]) -> bool:
        """True when a candidate TPC test ran a span line (lines map: the whole test) that no
        recorded query phase nor the global set-up ran: it ran while the store was warmed up
        (before the per-query forks), where its effect can outlive one query."""
        meta = self.meta()
        if "_all_lines" not in self.__dict__:
            per = {}
            for suite, qs in meta["suites"].items():
                acc: dict = {}
                for q in qs:
                    ph = q["phases"]
                    parts = [v for k, v in ph.items() if k != "win"]
                    parts += list((ph.get("win") or {}).values())
                    for part in parts:
                        for f, ls in (part or {}).items():
                            acc.setdefault(f, set()).update(ls)
                for f, ls in meta["global"].items():
                    acc.setdefault(f, set()).update(ls)
                per[suite] = {f: sorted(ls) for f, ls in acc.items()}
            self._all_lines = per
        maps = self.sel.maps
        for path, ss in spans.items():
            for first, last in ss:
                ran = maps.span(path, first, last) & cands
                for suite in {TESTS[self.numbers[n]][0] for n in ran}:
                    if not _hit(self._all_lines[suite], {path: [(first, last)]}):
                        return True
        return False

    def prune_patched(self, root: str, patched: dict[str, str], spans: dict,
                      candidates: set[int], benefit_of, stage_ok: bool) -> tuple[set[int], str]:
        """r37. The TPC tests of a commit whose .py edits are not all clean function-body
        edits (class / module bodies, definition-time code: no diffexec job): `spans` are the
        OLD-file lines whose run can behave differently (touched function statements, and the
        lines reading every value the import-state diff shows changed). The queries that run
        one are re-run in a fresh interpreter importing the PATCHED package and must give the
        pinned outputs; the others run identical code on identical state."""
        cands = candidates & set(self.numbers)
        if not cands:
            return set(), ""
        self.stage_ok, self.stage_why = stage_ok, (None if stage_ok else "guard")
        try:
            got = self.plan(spans, cands)
        except Exception as e:  # noqa: BLE001
            return set(), f"tpc-patched: keep ({type(e).__name__})"
        if got is None:
            return set(), "tpc-patched: keep (set-up touched / unknown / unstable)"
        per, cpu = got
        import itertools
        best = (0.0, set(), [], 0.0)
        nums = sorted(per)
        for r in range(1, len(nums) + 1):
            for subset in itertools.combinations(nums, r):
                todo = []
                for n in subset:
                    todo.extend(x for x in per[n] if x not in todo)
                est = sum(self._cpu(x) for x in todo) + (PATCHED_START_S if todo else 0.0)
                spend = SCORER_SLOWDOWN * est
                gain = benefit_of(set(subset))
                if gain > 0 and spend <= min(PATCHED_BUDGET_S, SHARE * gain / 1000):
                    net = gain / 1000 - spend
                    if net > best[0]:
                        best = (net, set(subset), todo, est)
        _, chosen, todo, est = best
        if not chosen:
            return set(), (f"tpc-patched: keep (est {cpu:.1f}s CPU for "
                           f"{sum(map(len, per.values()))} queries)")
        if not todo:
            return chosen, f"tpc-patched: {sorted(chosen)} dropped (no query runs a changed line)"
        cap = min(PATCHED_BUDGET_S / SCORER_SLOWDOWN, 2.5 * est + 1.0)
        meta = self.meta()
        sub = {"schemas": meta["schemas"], "suites": {}}
        for x in todo:
            sub["suites"].setdefault(x[0], {})[x[1]] = meta["suites"][x[0]][x[1]]
        ok, cpu_used = run_patched(root, patched, todo, sub, str(self.store), cap)
        self.last_child = round(cpu_used, 2)
        self.spent += cpu_used
        if not ok:
            return set(), (f"tpc-patched: keep (verification of {len(todo)} queries failed,"
                           f" {cpu_used:.2f}s, est {est:.2f}, cap {cap:.2f})")
        return chosen, (f"tpc-patched: {sorted(chosen)} dropped ({len(todo)} queries re-run"
                        f" on the patched import, {cpu_used:.2f}s, est {est:.2f}, cap {cap:.2f})")

    def _cpu(self, x):
        suite, i, opt, mode, parse_hit, optb_hit, duck_hit, k, gen_hit = x
        q = self.meta()["suites"][suite][i]
        if mode == "stage":
            # parse / duck transpile: a few ms; optb from the checkpoint: its pinned CPU;
            # generate replay (moon-25): its pinned CPU
            g = q["outs"]["gen_cpu"] + 0.01 if gen_hit else 0.0
            if optb_hit and k:
                return 0.02 + q["outs"]["cpu_from"][k] + g
            return 0.02 + (q["outs"]["cpu_stage"] if optb_hit else 0.0) + g
        if mode == "tail":
            return max(q["cpu_exec"] - q["outs"]["cpu_stage"], 0.0) + q["outs"]["cpu_from"][k]
        return q["cpu_opt"] if opt else q["cpu_exec"]


def _warm_code(root: str, jobs) -> None:
    """Compile the jobs' pinned and patched files in the parent, once per commit for all the
    forked verifiers (diffexec replay, subtest, slowverify): see diffexec/replay.py."""
    try:
        from diffexec.replay import warm_files
        warm_files(root, [(path, new_text) for path, new_text, _ in jobs])
    except Exception:  # noqa: BLE001  (the child compiles them itself)
        pass


def _swap(root: str, jobs, defaults: dict | None = None) -> str | None:
    """Swap the changed functions' code (as diffexec/replay.py). → bail reason or None.
    r35 `defaults` ({path: [(def name, first line, {param: (old, new)})]}, default-only files
    whose job text is the old source): the def's live function objects get the new literal
    `__defaults__` / `__kwdefaults__` (as diffexec/replay.py), which is all the patched
    module builds differently (select._default_changes; nothing reads `__defaults__`)."""
    import functools
    import importlib
    import types
    from diffexec import serial
    from diffexec.replay import _new_code, _old_code, _walk
    swaps = {}
    new_defaults = {}  # (filename, qualname, first line) → {param: new default}
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
        for name, line, params in (defaults or {}).get(path, ()):
            hits = [o for o in old_codes.values() if o.co_name == name
                    and o.co_firstlineno == line and o.co_flags & 0x01]
            argn = hits[0].co_varnames[:hits[0].co_argcount + hits[0].co_kwonlyargcount] \
                if len(hits) == 1 else ()
            if len(hits) != 1 or not set(params) <= set(argn):
                return f"default change of {name} not matched"
            new_defaults[(filename, hits[0].co_qualname, hits[0].co_firstlineno)] = {
                p: v[1] for p, v in params.items()}
        importlib.import_module(path[:-3].replace("/", ".").removesuffix(".__init__"))
    if not swaps and not new_defaults:
        return "nothing changed"
    seen = set()
    seen_defaults = set()
    for obj in gc.get_objects():
        if type(obj) is types.FunctionType:
            c = obj.__code__
            k = (c.co_filename, c.co_qualname, c.co_firstlineno)
            params = new_defaults.get(k)
            if params is not None:
                old_d, old_kw = obj.__defaults__, obj.__kwdefaults__
                npos = c.co_argcount
                pos_names = c.co_varnames[npos - len(old_d or ()):npos]
                if old_d is not None:
                    obj.__defaults__ = tuple(params.get(nm, v) for nm, v in zip(pos_names, old_d))
                if old_kw is not None:
                    obj.__kwdefaults__ = {kk: params.get(kk, v) for kk, v in old_kw.items()}
                if set(params) <= set(pos_names) | set(old_kw or ()):
                    seen_defaults.add(k)
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
    if seen_defaults != set(new_defaults):
        return "a default-changed function has no live function object"
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


# ── r37: the patched-import child ────────────────────────────────────────────────

PATCHED_START_S = float(os.environ.get("BR_TPCPATCH_START", "0.6"))  # spawn + patched import
PATCHED_BUDGET_S = float(os.environ.get("BR_TPCPATCH_S", "8"))  # scorer s (≈ 4 s here)

_RUN_PATCHED = (f"import sys; sys.path.insert(0, {str(REPO)!r}); "
                "from selector.subtest import _patched_main; _patched_main(sys.argv[1:]); "
                "sys.stdout.flush(); import os; os._exit(0)")


def _patched_main(argv: list[str]) -> None:
    """Child: import the patched package (selector/state.py `_PatchedFinder`) as the test
    worker does (unittest loaded first, every patched module imported), then run `todo`
    exactly as the forked function-body child does, minus the code swap."""
    import signal
    root = argv[0]
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    req = pickle.loads(sys.stdin.buffer.read())
    signal.setitimer(signal.ITIMER_PROF, req["budget"])
    try:
        import importlib
        import warnings
        sys.path.insert(0, root)
        from selector.state import _PatchedFinder
        sys.meta_path.insert(0, _PatchedFinder({os.path.join(root, p): s
                                                for p, s in req["patched"].items()}))
        warnings.simplefilter("ignore")
        import unittest  # noqa: F401  (sqlglot checks it at import, as under the test runner)
        _warm(root)
        for p in sorted(req["patched"]):
            importlib.import_module(p[:-3].replace("/", ".").removesuffix(".__init__"))
        ok = _child(root, [], req["todo"], req["meta"], req["store"], req["budget"],
                    patched=True)
    except BaseException:  # noqa: BLE001
        if os.environ.get("SUBTEST_DEBUG"):
            import traceback
            traceback.print_exc()
        ok = False
    signal.setitimer(signal.ITIMER_PROF, 0)
    sys.stdout.write("OK" if ok else "NO")


def run_patched(root: str, patched: dict[str, str], todo: list, meta: dict, store: str,
                budget: float) -> tuple[bool, float]:
    """(every query gave the pinned outputs?, child CPU s). Any failure → False."""
    import subprocess
    from .diffexec import wall_guard
    env = {"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if os.environ.get("SUBTEST_DEBUG"):
        env["SUBTEST_DEBUG"] = "1"
    req = pickle.dumps({"patched": patched, "todo": todo, "meta": meta, "store": store,
                        "budget": budget}, pickle.HIGHEST_PROTOCOL)
    t0 = os.times()
    try:
        proc = subprocess.run([sys.executable, "-P", "-s", "-S", "-c", _RUN_PATCHED, root],
                              input=req, capture_output=True, env=env,
                              timeout=wall_guard(budget), cwd="/")
    except (OSError, subprocess.SubprocessError):
        return False, 0.0
    finally:
        t1 = os.times()
    cpu = (t1.children_user - t0.children_user) + (t1.children_system - t0.children_system)
    if os.environ.get("SUBTEST_DEBUG") and proc.stderr:
        sys.stderr.write(proc.stderr.decode(errors="replace")[-3000:])
    return proc.returncode == 0 and proc.stdout == b"OK", cpu


def _run_tail(q, schema_dict, tables, zf, suite, i):
    """r39 "tail" mode: execute()'s plan / post windows (exec_windows) on the stored final
    optimized tree, its prep window on the query's tables. Only for queries whose touched
    statements run in neither parse, prep, optimize, duckdb transpile nor the glue: those
    windows give the pinned results, so this is what the test computes. → result digest."""
    import sqlglot.executor as E
    from sqlglot import find_tables, parse_one
    from sqlglot.executor.table import Table
    oe = from_checkpoint(zf.read(f"ck/{suite}/{i}/{NSTEPS - 1}"), schema_dict, NSTEPS - 1)
    if strict_digest(oe) != q["outs"]["d_opt"]:
        return None
    used = {}
    for t in find_tables(parse_one(q["sql"])):
        key = (suite, t.name)
        if key not in tables:
            cols, rows = marshal.loads(zf.read(f"{suite}/{t.name}"))
            tables[key] = Table(columns=cols, rows=rows)
        used[t.name] = tables[key]
    tables_ = E.ensure_tables(used, dialect=None)
    schema_ = E.ensure_schema(schema_dict, dialect=None)
    if tables_.supported_table_args and tables_.supported_table_args != schema_.supported_table_args:
        return None
    plan = E.Plan(oe)
    res = pickle.loads(pickle.dumps(E.PythonExecutor(tables=tables_).execute(plan)))
    return digest_table(res)


def _child(root, jobs, todo, meta, store, budget, defaults=None, patched=False) -> bool:
    import signal
    sys.setrecursionlimit(1000)  # the pool worker's default (this stack is deeper: stricter)
    # CPU cap: SIGPROF's default action ends the child (→ keep the tests)
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    if patched:
        # r37: `budget` caps the whole interpreter (start-up and imports included): each
        # query's own cap is cut to what is left
        _setitimer, _sigprof = signal.setitimer, signal.SIGPROF

        class _Capped:
            ITIMER_PROF = signal.ITIMER_PROF

            @staticmethod
            def setitimer(which, cap):
                left = budget - time.process_time()
                if left <= 0.01:
                    os.kill(os.getpid(), _sigprof)
                return _setitimer(which, min(cap, left))
        signal = _Capped  # noqa: F811  (the per-query arming below goes through the cap)
        signal.setitimer(signal.ITIMER_PROF, budget)
    else:
        signal.setitimer(signal.ITIMER_PROF, budget)
    _warm(root)
    from sqlglot.executor.table import Table
    why = None if patched else _swap(root, jobs, defaults)
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
    for suite, i, opt, mode, parse_hit, optb_hit, duck_hit, k, gen_hit in todo:
        if mode != "stage":
            full.append((suite, i, opt, mode == "tail"))
            continue
        q = meta["suites"][suite][i]
        signal.setitimer(signal.ITIMER_PROF, 3 * ((q["outs"]["cpu_from"][k] if k else q["outs"]["cpu_stage"])
                                                  + q["outs"].get("gen_cpu", 0.0)) + 0.8)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                if duck_hit and _transpile(q["sql"], write="duckdb")[0] != q["outs"]["duck"]:
                    return False
                if parse_hit:
                    e = pickle.loads(pickle.dumps(_parse_one(q["sql"])))
                    if strict_digest(e) != q["outs"]["d_parse"]:
                        fb_cpu += q["cpu_exec"]
                        if not FALLBACK and fb_cpu > FALLBACK_CAP: return False
                        full.append((suite, i, opt, False))  # parse differs: run the whole query
                        continue
                if optb_hit and k:
                    # moon-22: from the stored tree at boundary k, rules k.. under the new code
                    oe = from_checkpoint(zck.read(f"ck/{suite}/{i}/{k}"), meta["schemas"][suite], k)
                    if strict_digest(oe) != q["outs"]["d_opt"]:
                        if os.environ.get("SUBTEST_DEBUG"):
                            print("subtest rule diff:", suite, i, k, file=sys.stderr)
                        fb_cpu += q["cpu_exec"]
                        if not FALLBACK and fb_cpu > FALLBACK_CAP: return False
                        full.append((suite, i, opt, False))
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
                        full.append((suite, i, opt, False))  # optimized tree differs: run it all
                        continue
                if gen_hit and not gen_replay(pickle.loads(q["outs"]["gen"])):
                    if os.environ.get("SUBTEST_DEBUG"):
                        print("subtest gen diff:", suite, i, file=sys.stderr)
                    fb_cpu += q["cpu_exec"]
                    if not FALLBACK and fb_cpu > FALLBACK_CAP: return False
                    full.append((suite, i, opt, False))  # generated code differs: run it all
                    continue
            except Exception:  # noqa: BLE001
                if os.environ.get("SUBTEST_DEBUG"):
                    import traceback
                    traceback.print_exc(file=sys.__stderr__)
                return False
    tables: dict = {}
    with zipfile.ZipFile(store) as zf:
        for suite, i, opt, tail in full:
            q = meta["suites"][suite][i]
            # each query (with its table loading): at most 3x its pinned CPU + 0.8 s, else the
            # child ends (→ keep)
            signal.setitimer(signal.ITIMER_PROF, 3 * (q["cpu_opt"] if opt else q["cpu_exec"]) + 0.8)
            if tail:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        got = _run_tail(q, meta["schemas"][suite], tables, zf, suite, i)
                    except Exception:  # noqa: BLE001
                        if os.environ.get("SUBTEST_DEBUG"):
                            import traceback
                            traceback.print_exc(file=sys.__stderr__)
                        return False
                if got != q["outs"].get("exec"):
                    if os.environ.get("SUBTEST_DEBUG"):
                        print("subtest tail diff:", suite, i, file=sys.stderr)
                    return False
                continue
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
