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


def main():
    sel = {}
    for line in open(SEL):
        row = json.loads(line)
        sel[row["commit"]] = sorted(n for w in row["workers"] for n in w)
    out = {"warm_cpu": warm_total, "warm_import_cpu": warm_import, "commits": {}}
    commits = sorted(sel) if ONLY is None else ONLY
    if "--maxrec" in sys.argv:
        mx = float(sys.argv[sys.argv.index("--maxrec") + 1])
        mn = float(sys.argv[sys.argv.index("--minrec") + 1]) if "--minrec" in sys.argv else 0.0
        commits = [c for c in commits if mn < sum(by_num[n]["seconds"] for n in sel[c] if by_num[n]["seconds"] < SLOW) <= mx]
    if "--shard" in sys.argv:
        i, k = map(int, sys.argv[sys.argv.index("--shard") + 1].split("/"))
        commits = commits[i::k]
    if "--baseline" in sys.argv:
        commits = ["__base__"]
        sel["__base__"] = sorted(by_num)
    for c in commits:
        cheap = [n for n in sel[c] if by_num[n]["seconds"] < SLOW]
        if not cheap:
            out["commits"][c] = {"skipped": "no cheap candidates"}
            continue
        patch_text = "" if c == "__base__" else open(os.path.join(DATA, "commits", c + ".patch"), errors="replace").read()
        info = run_child(c, patch_text, cheap)
        info["n_cheap"] = len(cheap)
        info["cheap_recorded"] = sum(by_num[n]["seconds"] for n in cheap)
        info.pop("times", None) if "--keep-times" not in sys.argv else None
        out["commits"][c] = info
        print(f"[{c}] {info.get('how', info.get('fallback'))} cheap={len(cheap)} rec={info['cheap_recorded']:.2f}s "
              f"cpu={info['child_cpu']:.2f}s failed={len(info.get('failed', []))} "
              f"untrusted={len(info.get('untrusted', []))} {info.get('why_reimport', '')}", file=sys.stderr)
    json.dump(out, open(OUT, "w"))


if __name__ == "__main__":
    main()
