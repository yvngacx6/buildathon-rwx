"""Per-TPC query stage CPU + per-stage sqlglot line coverage + checkpoint sizes.

Stages of test_execute_tpcds / tpch per query:
  parse     parse_one(sql) + pickle round trip
  prep      ensure_tables / ensure_schema
  optimize  optimize(...)
  dbgsql    expression.sql(pretty=True)  (eager logger.debug argument)
  plan      Plan(expression)
  step:<K>  each executor step (PythonExecutor), K = step class name
  result    pickle round trip of the result
  duck      transpile(sql, write="duckdb")
Output: pickle {suite: [ {stage: (cpu, {file: lines}), ...}, ...]} and checkpoint sizes.
"""
import os, pickle, sys, time, zlib
ROOT = os.path.expanduser("~/rwx-research-robust/sqlglot")
sys.path.insert(0, ROOT)
sys.setrecursionlimit(1000)
os.environ["SKIP_INTEGRATION"] = "0"
REPO = "/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-aa9059184ad636363"
sys.path.insert(0, REPO)
from selector.subtest import _load_fixture_tables, SUITES, _Cov, _in_fork
from pathlib import Path
from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs
import sqlglot
from sqlglot import parse_one, transpile, find_tables
from sqlglot.executor import python as pyexec
from sqlglot.executor.table import Table, ensure_tables
from sqlglot.schema import ensure_schema
from sqlglot.optimizer import optimize
from sqlglot.planner import Plan
import sqlglot.dialects.duckdb  # noqa

prefix = ROOT + "/"
schemas = {"tpch": TPCH_SCHEMA, "tpcds": TPCDS_SCHEMA}
raw = {s: _load_fixture_tables(Path(ROOT), s, schemas[s]) for s in SUITES}
tables = {s: {n: Table(columns=c, rows=r) for n, (c, r) in raw[s].items()} for s in SUITES}
COV = "--cov" in sys.argv


def run(suite, q):
    st = []

    def stage(name, fn):
        cov = _Cov(prefix) if COV else None
        if cov:
            cov.start()
        t = time.process_time()
        r = fn()
        dt = time.process_time() - t
        st.append((name, dt, cov.stop() if cov else None))
        return r

    expr = stage("parse", lambda: pickle.loads(pickle.dumps(parse_one(q["sql"]))))
    ck = {"parse": len(zlib.compress(pickle.dumps(expr)))}
    if q["execute"]:
        used = {t.name: tables[suite][t.name] for t in find_tables(expr)}
        tabs, sch = stage("prep", lambda: (ensure_tables(used), ensure_schema(schemas[suite])))
        oe = stage("optimize", lambda: optimize(expr, sch, leave_tables_isolated=True))
        ck["optimize"] = len(zlib.compress(pickle.dumps(oe)))
        stage("dbgsql", lambda: oe.sql(pretty=True))
        plan = stage("plan", lambda: Plan(oe))
        ex = pyexec.PythonExecutor(tables=tabs)
        # replicate PythonExecutor.execute per step
        finished, queue, contexts = set(), set(plan.leaves), {}
        root = plan.root
        steps_ck = []
        while queue:
            node = queue.pop()
            ctx = ex.context({name: table for dep in node.dependencies for name, table in contexts[dep].tables.items()})

            def do(node=node, ctx=ctx):
                if isinstance(node, pyexec.planner.Scan):
                    return ex.scan(node, ctx)
                if isinstance(node, pyexec.planner.Aggregate):
                    return ex.aggregate(node, ctx)
                if isinstance(node, pyexec.planner.Join):
                    return ex.join(node, ctx)
                if isinstance(node, pyexec.planner.Sort):
                    return ex.sort(node, ctx)
                if isinstance(node, pyexec.planner.SetOperation):
                    return ex.set_operation(node, ctx)
                raise NotImplementedError(type(node))
            contexts[node] = stage("step:" + type(node).__name__, do)
            try:
                steps_ck.append(len(zlib.compress(pickle.dumps(contexts[node].tables), 1)))
            except Exception:
                steps_ck.append(-1)
            finished.add(node)
            for dep in node.dependents:
                if all(d in contexts for d in dep.dependencies):
                    queue.add(dep)
            for dep in node.dependencies:
                if all(d in finished for d in dep.dependents):
                    contexts.pop(dep)
        ck["steps"] = steps_ck
        stage("result", lambda: pickle.loads(pickle.dumps(contexts[root].tables["root"] if "root" in contexts[root].tables else None)))
    stage("duck", lambda: transpile(q["sql"], write="duckdb")[0])
    return st, ck


out = {}
for suite in SUITES:
    if len(sys.argv) > 1 and sys.argv[1] in SUITES and sys.argv[1] != suite:
        continue
    fixture = SUITES[suite] + ("tpc-h.sql" if suite == "tpch" else "tpc-ds.sql")
    rows = []
    for i, (qmeta, sql, optimized) in enumerate(load_sql_fixture_pairs(fixture)):
        q = {"sql": sql, "execute": bool(qmeta.get("execute"))}
        try:
            r = _in_fork(lambda q=q: run(suite, q))
        except RuntimeError as e:
            r = None
        rows.append(r)
    out[suite] = rows
    tot = {}
    for r in rows:
        if not r:
            continue
        for name, dt, _ in r[0]:
            k = name.split(":")[0] if not name.startswith("step") else name
            tot[k] = tot.get(k, 0) + dt
    print(suite, len(rows), {k: round(v, 2) for k, v in sorted(tot.items(), key=lambda x: -x[1])}, round(sum(tot.values()), 2))
pickle.dump(out, open(os.path.join(os.path.dirname(__file__), "stageprof%s.pkl" % ("_cov" if COV else "")), "wb"))
