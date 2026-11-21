"""Per-fixture-case verification of test_optimizer's fixture-loop tests (moonshot 15, PROTOTYPE).

22 TestOptimizer tests call `check_file(file, func, ...)`, which runs every (sql, expected) pair
of tests/fixtures/optimizer/<file>.sql through `parse_and_optimize` in a spawn pool and asserts
`optimized.sql(pretty, dialect) == expected` (plus, for `# execute: true` cases, equal duckdb
frames of the original and optimized SQL). test_annotate_funcs loops the same way in-process.
A function-body edit usually runs in a few of the ~3000 cases only.

Build (pinned source): for every case, the sqlglot lines it runs (cold fork per case,
sys.monitoring) incl. the pickle round trips of the pool; its pinned outputs and CPU.  For every
test, the lines its NON-case code runs (setUp + the test body with check_file stubbed out; cold
fork).

Run (diffexec-style commits only: clean function-body edits; same gate as subtest.py): a test
whose non-case lines include a touched statement is kept. Otherwise the cases that ran a touched
statement are re-run in a forked child with the changed functions' code swapped in; each must
satisfy the test's own assertion and give the pinned duckdb SQL (execute cases). All do →
every subtest of the test behaves as at the pinned commit → drop. Anything else → keep.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import os
import pickle
import sys
import time
import zlib
from pathlib import Path

from .subtest import _Cov, _hit, _in_fork, _swap

REPO = Path(__file__).resolve().parent.parent
DEFAULT_STORE = REPO / "index" / "cases.pkl"
TFILE = "tests/test_optimizer.py"
PREFIX = TFILE + "::TestOptimizer::"
SCORER_SLOWDOWN = 2.0
BUDGET_S = float(os.environ.get("BR_CASES_S", "6"))
SHARE = 0.6
OVERHEAD_S = float(os.environ.get("BR_CASES_OVERHEAD", "0.25"))  # fork + swap (laptop CPU s)
HELPERS = ("parse_and_optimize", "qualify_then_canonicalize", "qualify_columns",
           "pushdown_projections", "normalize", "simplify", "pushdown_ctes", "annotate_functions")
BANNED_FILES = ("sqlglot/serde.py",)  # kwargs were pickled at build with the pinned dump()


def fingerprint(root: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    for p in sorted((root / "sqlglot").rglob("*.py")):
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    for p in sorted((root / "tests" / "fixtures" / "optimizer").rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    h.update((root / "tests" / "helpers.py").read_bytes())
    h.update((root / TFILE).read_bytes())
    return h.hexdigest()


def helper_namespace(root: str) -> dict:
    """The module-level helpers of tests/test_optimizer.py (and its sqlglot imports), without
    importing the test module (duckdb / pandas)."""
    fn = os.path.join(root, TFILE)
    src = open(fn, encoding="utf-8").read()
    tree = ast.parse(src)
    body = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
            if all(m == "sqlglot" or m.startswith("sqlglot.") or m in ("json", "sys") for m in mods):
                body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in HELPERS:
            body.append(node)
    ns: dict = {"__name__": "tests.test_optimizer"}
    exec(compile(ast.Module(body=body, type_ignores=[]), fn, "exec"), ns)
    return ns


def _test_names(root: Path) -> list[str]:
    src = (root / TFILE).read_text()
    cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "TestOptimizer")
    out = []
    for f in cls.body:
        if isinstance(f, ast.FunctionDef) and f.name.startswith("test_"):
            seg = ast.get_source_segment(src, f)
            if "self.check_file(" in seg or f.name == "test_annotate_funcs":
                out.append(f.name)
    return out


def _string_to_bool(s):
    if s is None:
        return False
    if s in (True, False):
        return s
    return s and s.lower() in ("true", "1")


def _cases_of_call(a, k, pairs) -> list[dict]:
    """check_file's case construction (pinned tests/test_optimizer.py)."""
    file, func = a[0], a[1]
    pretty = k.pop("pretty", a[2] if len(a) > 2 else False)
    execute = k.pop("execute", a[3] if len(a) > 3 else None)
    only = k.pop("only", a[4] if len(a) > 4 else None)
    kwargs = k
    out = []
    for i, (meta, sql, expected) in enumerate(pairs(f"optimizer/{file}.sql"), start=1):
        title = meta.get("title") or f"{i}, {sql}"
        if only and title != only:
            continue
        dialect = meta.get("dialect")
        fk = kwargs.copy()
        if schema := meta.get("schema"):
            import json
            fk["schema"] = json.loads(schema)
        for key in ("leave_tables_isolated", "validate_qualify_columns", "canonicalize_table_aliases"):
            v = meta.get(key)
            if v is not None:
                fk[key] = _string_to_bool(v)
        if dialect:
            fk["dialect"] = dialect
        fx = False if execute is False else meta.get("execute", execute)
        if min_python := meta.get("execute_min_python"):
            fx = _string_to_bool(fx) and sys.version_info >= tuple(map(int, min_python.split(".")))
        out.append({"kind": "file", "func": _dump_func(func), "sql": sql,
                    "dialect": dialect, "kw": pickle.dumps(fk), "pretty": pretty,
                    "expected": expected, "execute": bool(_string_to_bool(fx))})
    return out


def _annotate_cases(pairs) -> list[dict]:
    out = []
    for i, (meta, sql, expected) in enumerate(pairs("optimizer/annotate_functions.sql"), start=1):
        dialect = meta.get("dialect") or ""
        sql = f"SELECT {sql} FROM tbl"
        for d in dialect.split(", "):
            out.append({"kind": "annotate", "sql": sql, "dialect": d, "expected": expected})
    return out


ANNOTATE_SCHEMA = None  # read from the test body at build (a dict literal)


def _dump_func(func) -> bytes:
    """Pickle `func` (a function or functools.partial); the test module's own helpers by name."""
    class P(pickle.Pickler):
        def persistent_id(self, obj):
            if getattr(obj, "__module__", None) == "tests.test_optimizer" and callable(obj) \
                    and getattr(obj, "__qualname__", "") in HELPERS:
                return obj.__qualname__
            return None
    buf = io.BytesIO()
    P(buf).dump(func)
    return buf.getvalue()


def _resolve(func: bytes, ns):
    class U(pickle.Unpickler):
        def persistent_load(self, pid):
            return ns[pid]
    return U(io.BytesIO(func)).load()


def run_case(c: dict, ns: dict, annotate_schema: dict):
    """What the test does for one case → (ok per the test's own assertion, outputs)."""
    from sqlglot import exp, parse_one
    if c["kind"] == "annotate":
        d = c["dialect"]
        result = ns["parse_and_optimize"](ns["annotate_functions"], c["sql"], d,
                                          schema=annotate_schema, dialect=d)
        a = result.type.sql(d)
        b = exp.DataType.build(c["expected"], dialect=d).sql(d)
        return a == b, (a, b)
    kw = pickle.loads(c["kw"])
    func = _resolve(c["func"], ns)
    # submit (main process pickles) → worker unpickles → result pickled back
    sql, dialect, kw = pickle.loads(pickle.dumps((c["sql"], c["dialect"], kw)))
    optimized = ns["parse_and_optimize"](func, sql, dialect, **kw)
    optimized = pickle.loads(pickle.dumps(optimized))
    actual = optimized.sql(pretty=c["pretty"], dialect=c["dialect"])
    duck = None
    if c["execute"]:
        duck = []
        for e in (parse_one(c["sql"], read=c["dialect"]), optimized):
            if not isinstance(e, (exp.Query, exp.Values)):
                e = exp.select(exp.alias_(e, "_col_0"))
            duck.append(e.sql(dialect="duckdb"))
        duck = tuple(duck)
    return actual == c["expected"], (actual, duck)


# ── build ─────────────────────────────────────────────────────────────────────

def _warm(root: str) -> None:
    if root not in sys.path:
        sys.path.insert(0, root)
    import sqlglot  # noqa: F401
    import sqlglot.optimizer  # noqa: F401
    import sqlglot.serde  # noqa: F401
    import sqlglot.dialects  # noqa: F401


def build(root: Path, out: Path, jobs: int = 4) -> None:
    root = root.resolve()
    sys.setrecursionlimit(1000)
    _warm(str(root))
    prefix = str(root) + "/"
    names = _test_names(root)

    def record():
        """Non-case coverage of every test + its check_file calls (each test in its own fork)."""
        import tests.test_optimizer as T
        import types
        T.ProcessPoolExecutor = lambda **k: types.SimpleNamespace(shutdown=lambda: None)
        res = {}
        for name in names:
            def one(name=name):
                T.TestOptimizer.setUpClass()  # duckdb tables (non-case asserts use them)
                inst = T.TestOptimizer(name)
                calls = []
                inst.check_file = lambda *a, **k: calls.append(pickle.dumps((a, k)))
                cov = _Cov(prefix)
                cov.start()
                inst.setUp()
                if name != "test_annotate_funcs":
                    getattr(inst, name)()
                lines = cov.stop()
                from tests.helpers import load_sql_fixture_pairs
                cs = []
                for blob in calls:
                    a, k = pickle.loads(blob)
                    cs.extend(_cases_of_call(list(a), dict(k), load_sql_fixture_pairs))
                return lines, cs
            res[name] = _in_fork(one)

        def glob():
            from sqlglot.schema import MappingSchema
            cov = _Cov(prefix)
            cov.start()
            MappingSchema()  # setUpClass: sqlglot.schema = MappingSchema()
            return cov.stop()
        return res, _in_fork(glob)

    rec, glob_lines = _in_fork(record)
    from tests.helpers import load_sql_fixture_pairs
    src = (root / TFILE).read_text()
    fn = next(f for f in ast.walk(ast.parse(src)) if isinstance(f, ast.FunctionDef)
              and f.name == "test_annotate_funcs")
    annotate_schema = ast.literal_eval(fn.body[0].value)
    tests = {}
    all_cases: list[dict] = []
    for name in names:
        noncase, cs = rec[name]
        idx = []
        if name == "test_annotate_funcs":
            cs = _annotate_cases(load_sql_fixture_pairs)
        for c in cs:
            c["test"] = name
            idx.append(len(all_cases))
            all_cases.append(c)
        tests[name] = {"noncase": noncase, "cases": idx}
        print(f"{name}: {len(idx)} cases, {sum(map(len, noncase.values()))} non-case lines",
              file=sys.stderr)

    ns = helper_namespace(str(root))

    def traced(i):
        c = all_cases[i]
        cov = _Cov(prefix)
        cov.start()
        try:
            ok, outs = run_case(c, ns, annotate_schema)
        except BaseException as e:  # noqa: BLE001
            ok, outs = False, repr(e)
        lines = cov.stop()
        t0 = time.process_time()
        ok2, outs2 = run_case(c, ns, annotate_schema)  # warm re-run: stable outputs
        return lines, ok and ok2 and outs == outs2, outs, time.process_time() - t0

    import multiprocessing as mp
    ctx = mp.get_context("fork")
    global _BUILD_TRACED
    _BUILD_TRACED = traced
    t0 = time.time()
    with ctx.Pool(jobs, maxtasksperchild=1) as pool:
        results = pool.map(_traced_entry, range(len(all_cases)), chunksize=1)
    print(f"traced {len(all_cases)} cases in {time.time() - t0:.0f}s", file=sys.stderr)
    # inverse index: file → line → case-set id
    sets: dict[tuple, int] = {}
    inv: dict[str, dict[int, int]] = {}
    per_line: dict[str, dict[int, list[int]]] = {}
    for i, (lines, ok, outs, cpu) in enumerate(results):
        c = all_cases[i]
        c["ok"], c["outs"], c["cpu"] = ok, outs, cpu
        for f, ls in lines.items():
            d = per_line.setdefault(f, {})
            for ln in ls:
                d.setdefault(ln, []).append(i)
    set_list = []
    for f, d in per_line.items():
        inv[f] = {}
        for ln, ids in d.items():
            key = tuple(ids)
            if key not in sets:
                sets[key] = len(set_list)
                set_list.append(key)
            inv[f][ln] = sets[key]
    meta = {"fingerprint": fingerprint(root), "tests": tests, "global": glob_lines,
            "cases": [{k: v for k, v in c.items() if k not in ("outs",)} | {"outs": c["outs"]}
                      for c in all_cases],
            "inv": inv, "sets": set_list, "annotate_schema": annotate_schema}
    bad = sum(1 for c in all_cases if not c["ok"])
    print(f"{len(all_cases)} cases, {bad} not ok/stable, {len(set_list)} distinct line sets",
          file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(zlib.compress(pickle.dumps(meta), 6))


_BUILD_TRACED = None


def warm_pass(root: Path, store: Path) -> None:
    """Cache-hit paths: the real cases run in WARM pool workers / the warm pytest process, where
    a case can take branches (memo hits, already-built tables) its cold run never takes. Run every
    case twice in one process in fixture order and store the lines any warm run executes that the
    case's own cold run did not ("warm-only"); a touched statement on one of them -> keep."""
    root = root.resolve()
    sys.setrecursionlimit(1000)
    _warm(str(root))
    meta = pickle.loads(zlib.decompress(store.read_bytes()))
    prefix = str(root) + "/"
    cold: dict[int, dict] = {}
    for f, d in meta["inv"].items():
        for ln, sid in d.items():
            for i in meta["sets"][sid]:
                cold.setdefault(i, {}).setdefault(f, set()).add(ln)

    def run():
        import tests.helpers  # noqa: F401
        ns = helper_namespace(str(root))
        extra: dict[str, set] = {}
        for _ in range(2):
            for i, c in enumerate(meta["cases"]):
                cov = _Cov(prefix)
                cov.start()
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        run_case(c, ns, meta["annotate_schema"])
                except Exception:  # noqa: BLE001
                    pass
                lines = cov.stop()
                ci = cold.get(i, {})
                for f, ls in lines.items():
                    new = set(ls) - ci.get(f, set())
                    if new:
                        extra.setdefault(f, set()).update(new)
        return {f: sorted(v) for f, v in extra.items()}

    meta["warm_only"] = _in_fork(run)
    print(f"warm-only lines: {sum(map(len, meta['warm_only'].values()))} in "
          f"{len(meta['warm_only'])} files", file=sys.stderr)
    store.write_bytes(zlib.compress(pickle.dumps(meta), 6))


def _traced_entry(i):
    return _BUILD_TRACED(i)


# ── run time ──────────────────────────────────────────────────────────────────

class Cases:
    def __init__(self, selector, store: Path):
        self.sel = selector
        self.store = store
        self._meta = None
        self._idx: dict = {}
        self.numbers = {t["number"]: t["id"][len(PREFIX):] for t in selector.tests
                        if t["id"].startswith(PREFIX)}
        self.spent = 0.0
        self.last_child = 0.0

    @classmethod
    def create(cls, selector):
        if os.environ.get("BR_CASES", "1") == "0":
            return None
        store = Path(os.environ.get("BR_CASES_STORE", DEFAULT_STORE))
        if not store.is_file():
            return None
        self = cls(selector, store)
        return self if self.numbers else None  # the store loads on first use

    def meta(self):
        if self._meta is None:
            self._meta = pickle.loads(zlib.decompress(self.store.read_bytes()))
            if self._meta["fingerprint"] != fingerprint(Path(self.sel.sqlglot)):
                self._meta = {}
        return self._meta

    def _hit_cases(self, spans: dict) -> set[int]:
        import bisect
        meta = self.meta()
        out: set[int] = set()
        for path, ss in spans.items():
            idx = self._idx.get(path)
            if idx is None:
                d = meta["inv"].get(path, {})
                lines = sorted(d)
                idx = self._idx[path] = (lines, [d[ln] for ln in lines])
            lines, sids = idx
            for first, last in ss:
                for j in range(bisect.bisect_left(lines, first), bisect.bisect_right(lines, last)):
                    out.update(meta["sets"][sids[j]])
        return out

    def plan(self, spans: dict, numbers: set[int]):
        meta = self.meta()
        if not meta or any(p in BANNED_FILES for p in spans):
            return None
        if _hit(meta["global"], spans) or _hit(meta.get("warm_only", {}), spans):
            return None  # set-up, or a line only warm runs take (cache hits): keep
        hit = self._hit_cases(spans)
        out = {}
        for n in numbers:
            t = meta["tests"].get(self.numbers[n])
            if t is None or _hit(t["noncase"], spans):
                continue  # unknown test / its own code runs a touched statement: keep
            todo = [i for i in t["cases"] if i in hit]
            if any(not meta["cases"][i]["ok"] for i in todo):
                continue
            out[n] = todo
        return out

    def prune(self, jobs, spans, candidates: set[int], benefit_of) -> tuple[set[int], str]:
        cands = candidates & set(self.numbers)
        if not cands or not spans:
            return set(), ""
        try:
            per = self.plan(spans, cands)
        except Exception as e:  # noqa: BLE001
            return set(), f"cases: keep ({type(e).__name__})"
        if not per:
            return set(), "cases: keep (set-up / non-case code touched)"
        meta = self.meta()
        cpu = lambda i: meta["cases"][i]["cpu"]  # noqa: E731
        chosen, est = [], 0.0
        for n in sorted(per, key=lambda n: sum(cpu(i) for i in per[n])):
            c = sum(cpu(i) for i in per[n]) + (OVERHEAD_S if per[n] and not est else 0.0)
            if per[n]:
                c += 0.01  # grandchild fork
            trial = set(chosen) | {n}
            spend = SCORER_SLOWDOWN * (est + c)
            gain = benefit_of(trial)
            if gain > 0 and spend <= min(BUDGET_S, SHARE * gain / 1000):
                chosen.append(n)
                est += c
        if not chosen:
            return set(), f"cases: keep (est {sum(cpu(i) for v in per.values() for i in v):.1f}s)"
        free = {n for n in chosen if not per[n]}
        work = [(n, sorted(per[n], key=cpu)) for n in chosen if per[n]]
        if not work:
            return free, f"cases: {len(free)} tests dropped (no case runs a touched statement)"
        try:
            passed = self.verify(jobs, work, min(BUDGET_S / SCORER_SLOWDOWN, 2.5 * est + 1.0))
        except Exception as e:  # noqa: BLE001
            passed = set()
        ncases = sum(len(v) for _, v in work)
        dropped = free | passed
        note = (f"cases: {len(dropped)} of {len(chosen)} tests dropped ({ncases} cases, "
                f"{len(work) - len(passed)} tests failed verification, {self.last_child:.2f}s)")
        return dropped, note

    def verify(self, jobs, work: list, budget: float) -> set[int]:
        """→ the tests whose cases all reproduce (each test verified in its own grandchild)."""
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
                passed = _child(root, jobs, work, meta, budget)
                with os.fdopen(wfd, "wb") as fh:
                    fh.write(pickle.dumps(sorted(passed)))
                code = 0
            except BaseException:  # noqa: BLE001
                if os.environ.get("CASES_DEBUG"):
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
                                + (t1.children_system - t0.children_system), 3)
        self.spent += self.last_child
        if timed_out or status != 0:
            return set()
        try:
            return set(pickle.loads(b"".join(chunks)))
        except Exception:  # noqa: BLE001
            return set()


def _child(root, jobs, work, meta, budget) -> set[int]:
    import signal
    sys.setrecursionlimit(1000)  # the pool worker's default
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_PROF, budget)
    why = _swap(root, jobs)
    if why:
        if os.environ.get("CASES_DEBUG"):
            print("cases bail:", why, file=sys.stderr)
        return set()
    ns = helper_namespace(root)
    passed = set()
    for n, todo in work:
        # one grandchild per test: a failing / state-changing case cannot leak into another test
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                code = 0 if _run_test_cases(todo, meta, ns) else 1
            except BaseException:  # noqa: BLE001
                code = 1
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        if status == 0:
            passed.add(n)
    return passed


def _run_test_cases(todo, meta, ns) -> bool:
    import signal
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    for i in todo:
        c = meta["cases"][i]
        signal.setitimer(signal.ITIMER_PROF, 3 * c["cpu"] + 0.5)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                ok, outs = run_case(c, ns, meta["annotate_schema"])
            except Exception:  # noqa: BLE001
                return False
        if not ok or (c["kind"] == "file" and outs[1] != c["outs"][1]):
            if os.environ.get("CASES_DEBUG"):
                print("cases diff:", c["test"], i, file=sys.stderr)
            return False
    signal.setitimer(signal.ITIMER_PROF, 0)
    return True


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "build":
        build(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]) if len(sys.argv) > 4 else 4)
        warm_pass(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) == 4 and sys.argv[1] == "warm":
        warm_pass(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        sys.exit("usage: python -m selector.cases build SQLGLOT_ROOT OUT.pkl [jobs]")
