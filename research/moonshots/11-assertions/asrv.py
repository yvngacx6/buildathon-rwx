"""Moonshot 02 prototype: warm fork-server test oracle.

Parent: import sqlglot + every test module once (recording which code objects ran).
Per commit: fork; in the child apply the patch either by
  * code swap   (only function bodies changed, none of them ran during warm-up):
                 f.__code__ = new_code on every live function object of that code, or
  * reimport    (anything else in sqlglot/*.py or tests/*.py): purge sqlglot*/tests* from
                 sys.modules, serve patched sources from a meta-path finder, re-import;
then run the candidate tests through unittest (class fixtures, subTest) and report failures.
Spawned/forked processes inside a test are routed in-process (ProcessPoolExecutor
replaced by a pickling in-process executor) or flag the test as untrusted.

Usage: python forksrv.py SQLGLOT_DIR DATA_DIR SELECTOR_STDOUT OUT_JSON [--slow 3]
"""
from __future__ import annotations

import gc
import importlib
import importlib.abc
import importlib.util
import json
import os
import pickle
import signal
import sys
import time
import types
import unittest
from concurrent.futures import Future

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, REPO)
from selector import diff as D  # noqa: E402

SQ, DATA, SEL, OUT = sys.argv[1:5]
SLOW = float(sys.argv[sys.argv.index("--slow") + 1]) if "--slow" in sys.argv else 3.0
ONLY = sys.argv[sys.argv.index("--only") + 1].split(",") if "--only" in sys.argv else None
MODE = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "auto"
os.environ["SKIP_INTEGRATION"] = "0"
os.chdir(SQ)
sys.path.insert(0, SQ)


def cpu():
    t = os.times()
    return t.user + t.system


def cpu_children():
    t = os.times()
    return t.children_user + t.children_system


# ---------------------------------------------------------------- warm-up
t0 = cpu()
ran_codes: set = set()
MON = sys.monitoring
TOOL = 3
MON.use_tool_id(TOOL, "forksrv")


def _on_start(code, offset):
    ran_codes.add(code)
    return MON.DISABLE


MON.register_callback(TOOL, MON.events.PY_START, _on_start)
MON.set_events(TOOL, MON.events.PY_START)

import builtins  # noqa: E402
import io  # noqa: E402
_real_open = io.open
_REAL_FORK = os.fork
warm_opened: set = set()
patched_files: dict = {}  # abs path -> new text (non-.py files), child only


def _open(file, mode="r", *a, **k):
    if isinstance(file, (str, os.PathLike)):
        f = os.path.abspath(os.fspath(file))
        if not patched_files:
            warm_opened.add(f)
        elif f in patched_files and "r" in mode and "+" not in mode:
            txt = patched_files[f]
            return io.BytesIO(txt.encode()) if "b" in mode else io.StringIO(txt)
    return _real_open(file, mode, *a, **k)


builtins.open = _open
io.open = _open
tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
by_num = {t["number"]: t for t in tests}
files = sorted({t["file"] for t in tests})
import sqlglot  # noqa: E402,F401
for f in files:
    importlib.import_module(f[:-3].replace("/", "."))
MON.set_events(TOOL, 0)
MON.register_callback(TOOL, MON.events.PY_START, None)
warm_import = cpu() - t0

# index live functions by file (closures made at import included)
funcs_by_file: dict[str, list] = {}
for o in gc.get_objects():
    if isinstance(o, types.FunctionType):
        funcs_by_file.setdefault(o.__code__.co_filename, []).append(o)
ran_keys = {(c.co_filename, c.co_qualname, c.co_firstlineno) for c in ran_codes}
gc.collect()
gc.freeze()
warm_total = cpu() - t0
print(f"warm-up cpu {warm_total:.2f}s (imports {warm_import:.2f}s), ran codes {len(ran_codes)}",
      file=sys.stderr)


def resolve(test_id):
    path, cls, meth = test_id.split("::")
    mod = sys.modules[path[:-3].replace("/", ".")]
    return getattr(mod, cls), meth


# ---------------------------------------------------------------- code swap
def walk(code, out, prefix_count):
    key = code.co_qualname
    n = prefix_count.get(key, 0)
    prefix_count[key] = n + 1
    out[(key, n)] = code
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            walk(c, out, prefix_count)


def own_sig(code):
    consts = tuple(("<code>", c.co_qualname) if isinstance(c, types.CodeType) else (type(c).__name__, repr(c))
                   for c in code.co_consts)
    return (code.co_code, consts, code.co_names, code.co_varnames, code.co_freevars,
            code.co_cellvars, code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
            code.co_flags, code.co_exceptiontable)


def plan_swap(filename, old_src, new_src):
    """Return list of (old_code_key, new_code) to swap, or a string reason to fall back."""
    try:
        old_tree, new_tree = {}, {}
        walk(compile(old_src, filename, "exec", dont_inherit=True), old_tree, {})
        walk(compile(new_src, filename, "exec", dont_inherit=True), new_tree, {})
    except SyntaxError:
        return "syntax error"
    if old_tree.keys() != new_tree.keys():
        return "code tree shape changed"
    swaps = []
    for k, oc in old_tree.items():
        nc = new_tree[k]
        if own_sig(oc) != own_sig(nc):
            if oc.co_qualname == "<module>" or (filename, oc.co_qualname, oc.co_firstlineno) in ran_keys:
                return f"changed code ran during warm-up: {oc.co_qualname}"
            if oc.co_freevars != nc.co_freevars:
                return "freevars changed"
        if oc != nc or own_sig(oc) != own_sig(nc):
            swaps.append((oc, nc))
    return swaps


def apply_swaps(filename, swaps):
    live = funcs_by_file.get(filename, [])
    idx = {}
    for f in live:
        idx.setdefault((f.__code__.co_qualname, f.__code__.co_firstlineno), []).append(f)
    n = 0
    for oc, nc in swaps:
        for f in idx.get((oc.co_qualname, oc.co_firstlineno), []):
            f.__code__ = nc
            n += 1
    return n


# ---------------------------------------------------------------- reimport
class PatchedFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, sources):  # abs path -> source
        self.sources = sources

    def find_spec(self, name, path, target=None):
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec and spec.origin in self.sources:
            spec.loader = self
            return spec
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        code = compile(self.sources[module.__spec__.origin], module.__spec__.origin, "exec",
                       dont_inherit=True)
        exec(code, module.__dict__)


def reimport(sources):
    for name in list(sys.modules):
        if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests."):
            del sys.modules[name]
    sys.meta_path.insert(0, PatchedFinder(sources))
    importlib.invalidate_caches()
    import sqlglot  # noqa: F401
    for f in files:
        importlib.import_module(f[:-3].replace("/", "."))


# ---------------------------------------------------------------- in-process pool
class InlinePool:
    """Stand-in for a spawn ProcessPoolExecutor: pickles args and results like the real one."""

    def __init__(self, *a, **k):
        pass

    def submit(self, fn, *args, **kwargs):
        fut = Future()
        try:
            fn2, args2, kwargs2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            res = fn2(*args2, **kwargs2)
            fut.set_result(pickle.loads(pickle.dumps(res)))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def shutdown(self, *a, **k):
        pass


untrusted_flag = [False]


def install_guards():
    import concurrent.futures as cf
    import subprocess
    cf.ProcessPoolExecutor = InlinePool
    for name in list(sys.modules):
        m = sys.modules[name]
        if name.startswith("tests") and getattr(m, "ProcessPoolExecutor", None) is not None:
            m.ProcessPoolExecutor = InlinePool
    real_popen_init = subprocess.Popen.__init__

    def popen_init(self, *a, **k):
        untrusted_flag[0] = True
        return real_popen_init(self, *a, **k)

    subprocess.Popen.__init__ = popen_init
    real_fork = os.fork

    def fork():
        untrusted_flag[0] = True
        return real_fork()

    os.fork = fork


# ---------------------------------------------------------------- run tests
class Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.failed = set()

    def _id(self, test):
        return f"{type(test).__module__}::{type(test).__qualname__}::{test._testMethodName}"

    def addError(self, test, err):
        super().addError(test, err)
        self.failed.add(getattr(test, "_testMethodName", None) and self._id(test) or repr(test))

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.failed.add(self._id(test))

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.failed.add(self._id(test))

    def addUnexpectedSuccess(self, test):
        self.failed.add(self._id(test))


class Timeout(BaseException):
    pass


def _alarm(sig, frm):
    raise Timeout()


setup_cpu: dict = {}


def run_tests(numbers, per_test_cpu=None, budget=None):
    """Run tests; return (failed numbers, untrusted numbers, cpu per test)."""
    signal.signal(signal.SIGPROF, _alarm)
    by_cls = {}
    for n in numbers:
        cls, meth = resolve(by_num[n]["id"])
        by_cls.setdefault(cls, []).append((n, meth))
    failed, untrusted, times = set(), set(), {}
    setup_cpu.clear()
    # class fixtures as unittest does: setUpClass once per class, tearDownClass after
    for cls, items in by_cls.items():
        result = Result()
        suite = unittest.TestSuite()
        tests_in = []
        for n, meth in items:
            t = cls(meth)
            tests_in.append((n, t))
        # run one by one inside a suite so that class fixtures are shared
        class Timed(unittest.TestCase):
            pass
        cls_failed_setup = False
        c0 = cpu()
        try:
            if hasattr(cls, "setUpClass"):
                cls.setUpClass()
        except BaseException:
            cls_failed_setup = True
        setup_cpu[f"{cls.__module__}.{cls.__qualname__}"] = cpu() - c0
        for n, t in tests_in:
            if cls_failed_setup:
                failed.add(n)
                continue
            untrusted_flag[0] = False
            c0 = cpu()
            cap = max(2.0, 10 * by_num[n]["seconds"])
            signal.setitimer(signal.ITIMER_PROF, cap, 0.05)  # repeats if a handler swallows it
            before = len(result.failed) + len(result.errors) + len(result.failures)
            try:
                t.run(result)
            except Timeout:
                failed.add(n)
            finally:
                signal.setitimer(signal.ITIMER_PROF, 0)
            after = len(result.failed) + len(result.errors) + len(result.failures)
            if after != before:
                failed.add(n)
            if untrusted_flag[0]:
                untrusted.add(n)
            times[n] = cpu() - c0
        try:
            if hasattr(cls, "tearDownClass"):
                cls.tearDownClass()
            unittest.case.doClassCleanups() if hasattr(unittest.case, "doClassCleanups") else cls.doClassCleanups()
        except BaseException:
            pass
    return failed, untrusted, times


def child(commit, patch_text, numbers):
    t0 = cpu()
    info = {"commit": commit}
    install_guards()
    fps = D.parse(patch_text)
    sources, swaps_all, how = {}, [], "swap" if MODE != "reimport" else "reimport"
    for fp in fps:
        p = fp.path
        if fp.is_binary or not (p.startswith("sqlglot/") or p.startswith("tests/")) \
                or fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path:
            info["fallback"] = f"unsupported file change {p}"
            return info
        absf = os.path.join(SQ, p)
        old = _real_open(absf).read()
        new = D.apply(old, fp)
        if not p.endswith(".py"):
            patched_files[absf] = new
            if absf in warm_opened:
                how = "reimport"
                info["why_reimport"] = f"data file read during warm-up: {p}"
            continue
        sources[absf] = new
        modname = p[:-3].replace("/", ".")
        if modname.endswith(".__init__"):
            modname = modname[:-9]
        if how == "swap":
            if modname not in sys.modules:
                # not loaded in the parent: a lazy import will read it through the finder
                continue
            plan = plan_swap(absf, old, new)
            if isinstance(plan, str):
                how = "reimport"
                info["why_reimport"] = plan
            else:
                swaps_all.append((absf, plan))
    if how == "swap":
        sys.meta_path.insert(0, PatchedFinder(sources))
        info["swapped"] = sum(apply_swaps(f, s) for f, s in swaps_all)
    else:
        try:
            reimport(sources)
        except BaseException as e:  # collection error: everything in that file fails
            info["fallback"] = f"import error {type(e).__name__}"
            return info
        install_guards()
    info["how"] = how
    info["prep_cpu"] = cpu() - t0
    failed, untrusted, times = run_tests(numbers)
    info["failed"] = sorted(failed)
    info["untrusted"] = sorted(untrusted)
    info["run_cpu"] = sum(times.values())
    info["times"] = times
    info["setup_cpu"] = dict(setup_cpu)
    return info


def run_child(commit, patch_text, numbers):
    r, w = os.pipe()
    c0 = cpu_children()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        import resource
        # hard CPU budget: a mutant that loops forever inside an except-all kills the child;
        # the parent then keeps every candidate (no oracle for this commit)
        budget = int(3 + 3 * sum(by_num[n]["seconds"] for n in numbers))
        resource.setrlimit(resource.RLIMIT_CPU, (budget, budget + 1))
        try:
            gc.enable()
            info = child(commit, patch_text, numbers)
        except BaseException as e:  # noqa: BLE001
            info = {"commit": commit, "fallback": f"child crash {type(e).__name__}: {e}"}
        data = json.dumps(info).encode()
        with os.fdopen(w, "wb") as fh:
            fh.write(data)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as fh:
        data = fh.read()
    os.waitpid(pid, 0)
    info = json.loads(data) if data else {"commit": commit, "fallback": "child died"}
    info["child_cpu"] = cpu_children() - c0
    return info




# ================================================================ Moonshot 11: assertion units
# A "unit" is (a) a call to Validator.validate_identity / validate_all / validate_transpile
# (key: caller file, call position, occurrence within the test) or (b) one fixture case of
# TestOptimizer.check_file (key: ("cf", call position, occurrence, case index)).
# Build (pinned code): per test, enumerate units, then per skippable unit a COLD fork
# (fresh test from the warm parent, every other skippable unit skipped, the target unit run
# under LINE monitoring) records the sqlglot lines the unit runs.
# Run (patched code, code swap only): the test is run for real with every skippable unit
# whose cold lines miss the changed statements skipped (and its argument signature equal to
# the recorded one); every other unit, setUp, and all residual test code run for real.
import ast  # noqa: E402
import hashlib  # noqa: E402
from collections import Counter  # noqa: E402

UNIT_METHODS = ("validate_identity", "validate_all", "validate_transpile")
SQ_PREFIX = os.path.join(SQ, "sqlglot") + os.sep
GUARD_CTX = ("assertLogs", "assertRaises", "assertRaisesRegex", "assertWarns", "assertNoLogs",
             "assertWarnsRegex")


def build_disc():
    """(abs file, lineno, col) of every self.validate_* / self.check_file call -> skippable?"""
    disc = {}
    for f in files:
        absf = os.path.join(SQ, f)
        tree = ast.parse(_real_open(absf).read())
        parents = {}
        for node in ast.walk(tree):
            for ch in ast.iter_child_nodes(node):
                parents[ch] = node
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in UNIT_METHODS + ("check_file",)):
                continue
            p = parents.get(node)
            ok = isinstance(p, ast.Expr)
            # inside a with assertRaises/assertLogs/...: skipping would change the outcome
            q = p
            while q is not None and ok:
                if isinstance(q, (ast.With, ast.AsyncWith)):
                    for it in q.items:
                        c = it.context_expr
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr in GUARD_CTX:
                            ok = False
                if isinstance(q, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    break
                q = parents.get(q)
            disc[(absf, node.lineno, node.col_offset)] = ok
    return disc


DISC = build_disc()
_pos_cache: dict = {}


def call_pos(frame):
    code = frame.f_code
    pl = _pos_cache.get(code)
    if pl is None:
        pl = _pos_cache[code] = list(code.co_positions())
    p = pl[frame.f_lasti // 2]
    return (code.co_filename, p[0], p[2])


class StopUnit(BaseException):
    pass


class U:
    mode = None          # "enum" | "cold" | "run"
    occ: Counter = Counter()
    depth = 0
    target = None        # cold: key
    skip: set = set()    # run: keys to skip
    sigs: dict = {}      # run: key -> recorded signature
    log: list = []       # enum: [key, disc, sig, cpu]
    cov: set = set()
    cold_res = None
    n_skipped = 0
    n_ran = 0
    mismatch = 0


def _on_line(code, line):
    fn = code.co_filename
    if fn.startswith(SQ_PREFIX):
        U.cov.add((fn[len(SQ) + 1:], line))
    return MON.DISABLE


LTOOL = 4
LINE_ONLY_STORE = "--line-only-store" in sys.argv


def _on_start(code, offset):
    # needed: with LINE alone, CPython 3.13 emits no LINE event for a code object's first line
    # (one-line lambdas in dispatch tables were invisible -> false drop on private rf8w)
    fn = code.co_filename
    if fn.startswith(SQ_PREFIX):
        U.cov.add((fn[len(SQ) + 1:], code.co_firstlineno))
    return MON.DISABLE


def cov_start():
    U.cov = set()
    MON.use_tool_id(LTOOL, "units")
    MON.register_callback(LTOOL, MON.events.LINE, _on_line)
    MON.register_callback(LTOOL, MON.events.PY_START, _on_start)
    MON.set_events(LTOOL, MON.events.LINE | MON.events.PY_START)
    MON.restart_events()


def cov_stop():
    MON.set_events(LTOOL, 0)
    MON.free_tool_id(LTOOL)


def sig_of(*parts):
    return hashlib.md5(repr(parts).encode()).hexdigest()[:12]


def run_unit(key, disc, sig, fn):
    if U.depth:  # nested unit: part of the outer one
        return fn()
    if U.mode == "enum":
        c0 = cpu()
        U.depth += 1
        try:
            return fn()
        finally:
            U.depth -= 1
            U.log.append([key, disc, sig, cpu() - c0])
    if U.mode == "cold":
        if key == U.target:
            ok = True
            cov_start()
            U.depth += 1
            try:
                fn()
            except BaseException as e:  # noqa: BLE001
                ok = f"{type(e).__name__}"
            finally:
                U.depth -= 1
                cov_stop()
            U.cold_res = (ok, sorted(U.cov))
            raise StopUnit()
        if disc:
            return None
        return fn()
    if U.mode == "run":
        if disc and key in U.skip:
            if U.sigs.get(key) == sig:
                U.n_skipped += 1
                return None
            U.mismatch += 1
        U.n_ran += 1
        U.depth += 1
        try:
            return fn()
        finally:
            U.depth -= 1
    return fn()


def _wrap(name, orig):
    def w(self, *a, **k):
        pos = call_pos(sys._getframe(1))
        n = U.occ[pos]
        U.occ[pos] += 1
        key = (pos[0][len(SQ) + 1:], pos[1], pos[2], n)
        disc = DISC.get(pos, False)
        sig = sig_of(name, self.dialect, a, sorted(k.items()))
        return run_unit(key, disc, sig, lambda: orig(self, *a, **k))
    w.__wrapped__ = orig
    return w


def install_units():
    from tests.dialects.test_dialect import Validator
    for m in UNIT_METHODS:
        orig = getattr(Validator, m)
        if not hasattr(orig, "__wrapped__"):
            setattr(Validator, m, _wrap(m, orig))
    import tests.test_optimizer as to
    if not hasattr(to.TestOptimizer.check_file, "__wrapped__"):
        orig_cf = to.TestOptimizer.check_file

        def check_file(self, file, func, pretty=False, execute=None, only=None, **kwargs):
            pos = call_pos(sys._getframe(1))
            n = U.occ[pos]
            U.occ[pos] += 1
            disc = DISC.get(pos, False)
            base = ("cf", pos[0][len(SQ) + 1:], pos[1], pos[2], n)
            for i, (meta, sql, expected) in enumerate(to.load_sql_fixture_pairs(f"optimizer/{file}.sql"), start=1):
                title = meta.get("title") or f"{i}, {sql}"
                if only and title != only:
                    continue
                key = base + (i,)
                sig = sig_of(file, getattr(func, "__qualname__", repr(func)), pretty, execute,
                             sorted(kwargs.items()), sorted(meta.items()), sql, expected)
                run_unit(key, disc, sig,
                         lambda: _cf_case(self, to, meta, sql, expected, title, func, pretty, execute, kwargs))
        check_file.__wrapped__ = orig_cf
        to.TestOptimizer.check_file = check_file


def _cf_case(self, to, meta, sql, expected, title, func, pretty, execute, kwargs):
    """One fixture case of TestOptimizer.check_file, same statements as the original."""
    dialect = meta.get("dialect")
    leave_tables_isolated = meta.get("leave_tables_isolated")
    validate_qualify_columns = meta.get("validate_qualify_columns")
    canonicalize_table_aliases = meta.get("canonicalize_table_aliases")
    func_kwargs = kwargs.copy()
    if schema := meta.get("schema"):
        func_kwargs["schema"] = json.loads(schema)
    if leave_tables_isolated is not None:
        func_kwargs["leave_tables_isolated"] = to.string_to_bool(leave_tables_isolated)
    if validate_qualify_columns is not None:
        func_kwargs["validate_qualify_columns"] = to.string_to_bool(validate_qualify_columns)
    if dialect:
        func_kwargs["dialect"] = dialect
    if canonicalize_table_aliases is not None:
        func_kwargs["canonicalize_table_aliases"] = to.string_to_bool(canonicalize_table_aliases)
    fixture_execute = False if execute is False else meta.get("execute", execute)
    if min_python := meta.get("execute_min_python"):
        fixture_execute = to.string_to_bool(fixture_execute) and sys.version_info >= tuple(
            map(int, min_python.split(".")))
    future = self.pool.submit(to.parse_and_optimize, func, sql, dialect, **func_kwargs)
    with self.subTest(title):
        optimized = future.result()
        actual = optimized.sql(pretty=pretty, dialect=dialect)
        self.assertEqual(expected, actual)
        if to.string_to_bool(fixture_execute):
            with self.subTest(f"(execute) {title}"):
                dfs = []
                for expression in (to.parse_one(sql, read=dialect), optimized):
                    if not isinstance(expression, (to.exp.Query, to.exp.Values)):
                        expression = to.exp.select(to.exp.alias_(expression, "_col_0"))
                    dfs.append(self.conn.execute(expression.sql(dialect="duckdb")).df())
                to.assert_frame_equal(*dfs)


def fork_call(fn):
    """Run fn() in a forked child; return its picklable result (None if it died)."""
    r, w = os.pipe()
    pid = _REAL_FORK()
    if pid == 0:
        os.close(r)
        try:
            res = fn()
        except BaseException as e:  # noqa: BLE001
            res = {"crash": f"{type(e).__name__}: {e}"}
        data = pickle.dumps(res)
        with os.fdopen(w, "wb") as fh:
            fh.write(data)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as fh:
        data = fh.read()
    os.waitpid(pid, 0)
    return pickle.loads(data) if data else None


def run_one_test(n):
    """Run test n (setUpClass, setUp, body) under the current U mode; return failed?"""
    cls, meth = resolve(by_num[n]["id"])
    result = Result()
    try:
        if hasattr(cls, "setUpClass"):
            cls.setUpClass()
    except BaseException:
        return True
    t = cls(meth)
    t.run(result)
    errs = result.errors + result.failures
    if any("StopUnit" in e[1] for e in errs):
        return "stopped"
    return bool(result.failed or errs)


def record_test(n):
    """Build-time record of one test (called in a fork of the warm parent)."""
    install_guards()
    install_units()

    def enum():
        U.mode, U.occ, U.log = "enum", Counter(), []
        untrusted_flag[0] = False
        c0 = cpu()
        failed = run_one_test(n)
        return {"failed": failed, "units": U.log, "cpu": cpu() - c0, "untrusted": untrusted_flag[0]}

    e = fork_call(enum)
    if not e or "crash" in e:
        return {"error": e}
    out = dict(e)
    units = []
    c0 = cpu_children()
    for key, disc, sig, ucpu in e["units"]:
        if not disc:
            units.append([key, disc, sig, ucpu, None, None])
            continue

        def cold(key=key):
            U.mode, U.occ, U.target, U.cold_res = "cold", Counter(), key, None
            run_one_test(n)
            return U.cold_res

        res = fork_call(cold)
        if res is None or isinstance(res, dict):
            units.append([key, disc, sig, ucpu, "noresult", None])
        else:
            units.append([key, disc, sig, ucpu, res[0], res[1]])
    out["units"] = units
    out["cold_cpu"] = cpu_children() - c0
    return out


_HEADER_TYPES = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.FunctionDef, ast.AsyncFunctionDef,
                 ast.ClassDef, ast.AsyncFor, ast.AsyncWith)


def changed_statements(old_src, removed_lines):
    """Old-file lines whose execution means a changed statement ran: each changed line is
    widened to its innermost statement (simple statement: all its lines; compound: header)."""
    tree = ast.parse(old_src)
    hit = set()
    spans = []
    for s in ast.walk(tree):
        if not isinstance(s, ast.stmt):
            continue
        end = s.end_lineno
        body = getattr(s, "body", None)
        if isinstance(s, _HEADER_TYPES) and isinstance(body, list) and body:
            end = max(s.lineno, body[0].lineno - 1)
        spans.append((s.lineno, end))
    for ln in removed_lines:
        cand = [sp for sp in spans if sp[0] <= ln <= sp[1]]
        if not cand:
            return None
        best = min(cand, key=lambda sp: sp[1] - sp[0])
        hit.update(range(best[0], best[1] + 1))
    return hit


def sliced_child(commit, patch_text, numbers, store):
    t0 = cpu()
    info = {"commit": commit}
    install_guards()
    fps = D.parse(patch_text)
    sources, swaps_all, changed = {}, [], set()
    for fp in fps:
        p = fp.path
        if fp.is_binary or not p.startswith("sqlglot/") or not p.endswith(".py") \
                or fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path:
            info["fallback"] = f"unsupported file change {p}"
            return info
        absf = os.path.join(SQ, p)
        old = _real_open(absf).read()
        new = D.apply(old, fp)
        removed, added = D.changed_lines(fp)
        if not removed:
            info["fallback"] = "pure insertion"
            return info
        st = changed_statements(old, removed)
        if st is None:
            info["fallback"] = "changed line outside statements"
            return info
        fi = store['files'].get(p)
        if fi is not None:
            changed |= {(fi << 16) | ln for ln in st}
        sources[absf] = new
        # only edits inside function bodies: a changed module / class body (even of a module
        # not loaded yet) changes state that skipped units read (false drop on private 22yd)
        try:
            ot, nt = {}, {}
            walk(compile(old, absf, "exec", dont_inherit=True), ot, {})
            walk(compile(new, absf, "exec", dont_inherit=True), nt, {})
        except SyntaxError:
            info["fallback"] = "syntax error"
            return info
        if ot.keys() != nt.keys():
            info["fallback"] = "code tree shape changed"
            return info
        for kk, oc in ot.items():
            if own_sig(oc) != own_sig(nt[kk]) and not (oc.co_flags & 0x1):  # CO_OPTIMIZED: a function
                info["fallback"] = f"module/class body changed: {oc.co_qualname}"
                return info
            # a store recorded with LINE events only has no event for a code object's first
            # line (one-line lambdas / defs): such an edit is not sliceable with that store
            if LINE_ONLY_STORE and (oc.co_flags & 0x1) and oc.co_firstlineno in st:
                info["fallback"] = f"edit on first line of {oc.co_qualname}"
                return info
        modname = p[:-3].replace("/", ".")
        if modname.endswith(".__init__"):
            modname = modname[:-9]
        if modname not in sys.modules:
            continue
        plan = plan_swap(absf, old, new)
        if isinstance(plan, str):
            info["fallback"] = plan
            return info
        swaps_all.append((absf, plan))
    sys.meta_path.insert(0, PatchedFinder(sources))
    for f, s in swaps_all:
        apply_swaps(f, s)
    install_units()
    info["prep_cpu"] = cpu() - t0
    res, plans = {}, []
    for n in numbers:
        rec = store['tests'].get(n)
        if rec is None or rec.get("failed") or "units" not in rec:
            res[n] = {"v": "nostore"}
            continue
        skip, sigs, aff, pred = set(), {}, 0, 0.0
        for key, disc, sig, ucpu, ok, lines in rec["units"]:
            key = tuple(key)
            if not disc or ok is not True or not changed.isdisjoint(lines):
                aff += 1
                pred += ucpu
                continue
            skip.add(key)
            sigs[key] = sig
        plans.append((n, skip, sigs, aff, pred, len(rec["units"])))

    def go_all():
        out, setup = {}, {}
        signal.signal(signal.SIGPROF, _alarm)
        for n, skip, sigs, aff, pred, nu in plans:
            cls, meth = resolve(by_num[n]["id"])
            ck = f"{cls.__module__}.{cls.__qualname__}"
            if ck not in setup:
                c0 = cpu()
                try:
                    if hasattr(cls, "setUpClass"):
                        cls.setUpClass()
                    setup[ck] = cpu() - c0
                except BaseException:  # noqa: BLE001
                    setup[ck] = None
            U.mode, U.occ, U.skip, U.sigs = "run", Counter(), skip, sigs
            U.n_skipped = U.n_ran = U.mismatch = 0
            untrusted_flag[0] = False
            c0 = cpu()
            failed = setup[ck] is None
            if not failed:
                signal.setitimer(signal.ITIMER_PROF, max(2.0, 10 * by_num[n]["seconds"]), 0.05)
                try:
                    result = Result()
                    cls(meth).run(result)
                    failed = bool(result.failed or result.errors or result.failures)
                except Timeout:
                    failed = True
                finally:
                    signal.setitimer(signal.ITIMER_PROF, 0)
            out[n] = {"failed": failed, "cpu": cpu() - c0, "skipped": U.n_skipped, "ran": U.n_ran,
                      "mismatch": U.mismatch, "untrusted": untrusted_flag[0], "affected": aff,
                      "pred": pred, "n_units": nu, "cls": ck}
        return {"tests": out, "setup": setup}

    r = fork_call(go_all) or {"tests": {}, "setup": {}, "died": True}
    for n, skip, sigs, aff, pred, nu in plans:
        t = r["tests"].get(n, {"failed": True, "died": True})
        t["v"] = "fail" if (t.get("failed") or t.get("untrusted") or t.get("died")) else "pass"
        res[n] = t
    info["setup"] = r.get("setup", {})
    info["tests"] = res
    info["run_cpu"] = cpu() - t0
    return info


def main():
    mode = sys.argv[sys.argv.index("--do") + 1]
    if mode == "record":
        nums = [int(x) for x in _real_open(sys.argv[sys.argv.index("--tests") + 1]).read().split()]
        if "--shard" in sys.argv:
            i, k = map(int, sys.argv[sys.argv.index("--shard") + 1].split("/"))
            nums = nums[i::k]
        out = {}
        for n in nums:
            c0 = cpu_children()
            r = fork_call(lambda: record_test(n)) or {"error": "died"}
            r["total_cpu"] = cpu_children() - c0
            out[n] = r
            u = r.get("units", [])
            print(f"[{n}] {by_num[n]['id']} units={len(u)} failed={r.get('failed')} cpu={r['total_cpu']:.2f}s "
                  f"rec={by_num[n]['seconds']} {r.get('error', '')}", file=sys.stderr, flush=True)
        pickle.dump(out, _real_open(OUT, "wb"))
        return
    if mode == "run":
        store = pickle.load(_real_open(sys.argv[sys.argv.index("--store") + 1], "rb"))
        sel = {}
        for line in _real_open(SEL):
            row = json.loads(line)
            sel[row["commit"]] = sorted(x for w in row["workers"] for x in w)
        commits = sorted(sel) if ONLY is None else ONLY
        if "--shard" in sys.argv:
            i, k = map(int, sys.argv[sys.argv.index("--shard") + 1].split("/"))
            commits = commits[i::k]
        out = {}
        for c in commits:
            cand = [x for x in sel[c] if by_num[x]["seconds"] < SLOW and x in store["tests"]]
            if not cand:
                continue
            patch_text = _real_open(os.path.join(DATA, "commits", c + ".patch"), errors="replace").read()
            c0 = cpu_children()

            def job(c=c, patch_text=patch_text, cand=cand):
                import resource
                resource.setrlimit(resource.RLIMIT_CPU, (60, 61))
                gc.enable()
                return sliced_child(c, patch_text, cand, store)
            info = fork_call(job) or {"fallback": "died"}
            info["child_cpu"] = cpu_children() - c0
            out[c] = info
            json.dump(out, _real_open(OUT, "w"), default=str)
            t = info.get("tests", {})
            print(f"[{c}] {info.get('fallback', '')} cand={len(cand)} pass={sum(v['v'] == 'pass' for v in t.values())} "
                  f"fail={sum(v['v'] == 'fail' for v in t.values())} cpu={info['child_cpu']:.2f}",
                  file=sys.stderr, flush=True)
        json.dump(out, _real_open(OUT, "w"), default=str)


if __name__ == "__main__":
    main()
