"""Run the leftover cheap selection directly with unittest on the patched code (round 24, V1).

After every static rule and run-time pruner (diffexec, subtest, slowverify, genmem), a commit
often keeps a few cheap tests that in fact all pass; the scorer still charges 2 s per worker.
Here, when the leftover selection is small and cheap (predicted scorer CPU <= GATE_S, from a
pinned-commit map of per-test CPU measured in the image), we run it and drop what passes:

  * a child is forked from the selector; it removes every `sqlglot*` / `tests*` module from
    `sys.modules` and imports them again, fresh, through a meta-path finder that serves the
    patched sources (selector/state.py `_PatchedFinder`). Nothing old (no replay result, no
    cache filled by old code) survives in the modules the tests use. Patched non-.py files
    (fixtures) are served by an `open()` hook. `unittest` is imported first (sqlglot reads
    `"unittest" in sys.modules` at import, as under pytest);
  * the tests run as `unittest.TestSuite([...]).run(result)`, grouped per class (setUpClass
    once), cheapest class first, stopping at the first failing test (failfast): a failing
    subtest, error or unexpected success is a failure;
  * output = the selection minus the tests that passed (all pass → nothing remains).

Never verified: tests whose file imports multiprocessing / concurrent.futures / subprocess /
threading (TestOptimizer, TestExecutor, ...), tests that measure resources (time, gc, memory,
recursion), tests the map did not see pass cleanly alone (spawned a process, skipped, failed,
too slow, wrote to an unserved file ...).

Any anomaly keeps the whole static selection: an import error (also a failed import that a
module caught: optional dependencies differ from the labelers' image), a skip or expected
failure, a class/module fixture error, a test id that does not resolve, a process or thread
start (audit hook), a read of a patched file's on-disk copy, the CPU cap (ITIMER_PROF, plus
RLIMIT_CPU as backstop), a crash. Budgets are CPU only. The child's stdout/stderr go to
/dev/null; results come back over a pipe. CPU affinity is pinned to the first two CPUs the
process may use. Switch: BR_RUNVERIFY=0.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_MAP = REPO / "maps" / "runverify.json.gz"
# Third-party packages the labelers' image had and some tests import (pytz, dateutil, tzdata,
# pinned as in requirements.lock), installed at build into a directory only the child sees.
DEPS = os.environ.get("BR_RUNVERIFY_DEPS", "/app/deps")
GATE_S = float(os.environ.get("BR_RUNVERIFY_GATE", "0.6"))  # predicted scorer CPU per commit
SCORER_SLOWDOWN = float(os.environ.get("BR_RUNVERIFY_SLOWDOWN", "2.0"))  # scorer vs map host
CAP_FACTOR, CAP_EXTRA = 4.0, 0.3  # child CPU cap: 4x the map estimate + 0.3 s (2x on scorer)
# Also verify when the predicted scorer CPU is at most this share of what dropping the whole
# selection would save (its scorer cost: busiest worker + 2 s per worker).
SHARE = float(os.environ.get("BR_RUNVERIFY_SHARE", "0"))
BATCH_S = float(os.environ.get("BR_RUNVERIFY_BATCH_S", "20"))  # total child CPU per run
SPAWN_MODULES = {"multiprocessing", "concurrent", "subprocess", "threading", "_thread",
                 "socket", "asyncio", "pty"}
RESOURCE_MODULES = {"tracemalloc", "gc", "resource", "time", "timeit", "psutil", "signal",
                    "faulthandler", "pyperf"}
RESOURCE_ATTRS = {"setrecursionlimit", "getrecursionlimit", "getsizeof", "getrefcount",
                  "get_traced_memory", "perf_counter", "process_time", "monotonic"}
# Events that start a process or thread or talk to the network: never inside a verified test.
SPAWN_EVENTS = {"os.fork", "os.forkpty", "os.posix_spawn", "os.spawn", "os.exec", "os.system",
                "os.startfile", "subprocess.Popen", "_posixsubprocess.fork_exec",
                "_thread.start_new_thread", "_thread.start_joinable_thread", "socket.connect",
                "socket.bind", "os.kill", "os.killpg", "signal.pthread_kill"}
# Paths a patch may touch for us to verify (anything else, e.g. pytest config, → keep).
SERVABLE_PREFIXES = ("sqlglot/", "tests/")
CONFIG_NAMES = {"conftest.py", "pytest.ini", "setup.cfg", "tox.ini", "pyproject.toml",
                "setup.py", "sitecustomize.py", "usercustomize.py"}


# ── child (also used by the map build) ─────────────────────────────────────────

class _Anomaly(Exception):
    pass


def _setup_child(root: str, served: dict[str, str], patched_paths: set[str], cap: float,
                 wfd: int) -> dict:
    """Everything before the first test: limits, stdio, fresh patched imports, guards."""
    import gc
    import resource
    import signal

    # CPU budget: ITIMER_PROF (sub-second) ends the child; RLIMIT_CPU is the backstop.
    def over(*_):
        os._exit(3)
    signal.signal(signal.SIGPROF, over)
    signal.setitimer(signal.ITIMER_PROF, cap)
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        lim = int(cap) + 2
        resource.setrlimit(resource.RLIMIT_CPU, (lim, lim + 1))
    except (ValueError, OSError):
        pass
    try:
        cpus = sorted(os.sched_getaffinity(0))[:2]
        os.sched_setaffinity(0, cpus)
    except (AttributeError, OSError):
        pass
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    if not os.environ.get("RUNVERIFY_DEBUG"):
        os.dup2(devnull, 2)
    import io
    sys.stdout = sys.__stdout__ = io.TextIOWrapper(io.FileIO(1, "w", closefd=False))
    sys.stderr = sys.__stderr__ = io.TextIOWrapper(io.FileIO(2, "w", closefd=False))

    # Fresh modules: nothing the selector imported (old code, caches) is reused.
    for k in [k for k in sys.modules
              if k in ("sqlglot", "tests") or k.startswith(("sqlglot.", "tests.", "diffexec"))]:
        del sys.modules[k]
    repo = os.path.realpath(REPO)
    sys.path[:] = [root] + [p for p in sys.path
                            if p not in ("", ".") and os.path.realpath(p) != repo
                            and os.path.realpath(p) != os.path.realpath(root)]
    if os.path.isdir(DEPS):
        sys.path.append(DEPS)
    sys.path_importer_cache.clear()
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"  # as the labelers ran
    sys.setrecursionlimit(1000)
    import warnings
    warnings.resetwarnings()
    gc.freeze()  # the selector's heap: never scanned again
    gc.enable()

    import importlib.machinery
    import linecache

    # Patched sources: .py through a meta-path finder, every served file through open().
    if served:
        from .state import _PatchedFinder
        sys.meta_path.insert(0, _PatchedFinder({p: s for p, s in served.items()
                                                if p.endswith(".py")}))
        for p, s in served.items():
            if p.endswith(".py"):
                lines = s.splitlines(keepends=True)
                linecache.cache[p] = (len(s), None, lines, p)
        import builtins
        real_open = builtins.open

        def _open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None,
                  closefd=True, opener=None):
            if isinstance(file, (str, bytes, os.PathLike)):
                rp = os.path.realpath(os.fsdecode(file))
                text = served.get(rp)
                if text is not None:
                    if any(c in mode for c in "wax+"):
                        raise _Anomaly("write to a patched file")
                    if "b" in mode:
                        return io.BytesIO(text.encode(encoding or "utf-8"))
                    return io.StringIO(text, newline=newline if newline is not None else None)
            return real_open(file, mode, buffering, encoding, errors, newline, closefd, opener)
        builtins.open = io.open = _open

    state = {"anomaly": None, "imports": set()}
    real_paths = {os.path.realpath(p) for p in patched_paths}

    def audit(event, args):
        if event in SPAWN_EVENTS and not (event == "os.fork" and state.get("harness_fork")):
            state["anomaly"] = state["anomaly"] or f"spawn {event}"
            raise _Anomaly(event)
        if event == "open" and real_paths and isinstance(args[0], (str, bytes)):
            rp = os.path.realpath(os.fsdecode(args[0]))
            if rp in real_paths:
                state["anomaly"] = state["anomaly"] or f"read of on-disk {rp}"
                raise _Anomaly("patched file read from disk")
        elif event == "import":
            state["imports"].add(args[0])
    sys.addaudithook(audit)
    import threading
    _start = threading.Thread.start

    def start(self, *a, **k):
        state["anomaly"] = state["anomaly"] or "thread"
        raise _Anomaly("thread start")
    threading.Thread.start = start
    del _start

    import unittest  # before sqlglot: sqlglot checks "unittest" in sys.modules at import
    state["t_unittest"] = time.process_time()
    import sqlglot  # noqa: F401
    state["t_sqlglot"] = time.process_time()
    return state


# Imports that fail at the pinned commit in this image whatever the test (optional
# accelerators / build metadata; recorded by the map build as "allowed_missing").
ALLOWED_MISSING: set[str] = set()


def _missing(state) -> set[str]:
    return {n for n in state["imports"] if n not in sys.modules and not n.startswith(".")}


def _missing_imports(state) -> str | None:
    bad = _missing(state) - ALLOWED_MISSING - state.get("allowed", set())
    return f"failed import {sorted(bad)[0]}" if bad else None


def _make_suite(plan: list[str]):
    """plan: test ids in run order → (suite, {unittest id: our id}). Raises on any miss."""
    import importlib
    import unittest
    tests, by_uid = [], {}
    for tid in plan:
        parts = tid.split("::")
        if len(parts) != 3 or not parts[0].endswith(".py"):
            raise _Anomaly(f"id {tid}")
        mod = importlib.import_module(parts[0][:-3].replace("/", "."))
        cls = getattr(mod, parts[1])
        if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)):
            raise _Anomaly(f"not a TestCase {tid}")
        if not callable(getattr(cls, parts[2], None)):
            raise _Anomaly(f"no method {tid}")
        t = cls(parts[2])
        tests.append(t)
        by_uid[t.id()] = tid
    return unittest.TestSuite(tests), by_uid


def _result_class():
    import unittest

    class Result(unittest.TestResult):
        """Records verdicts without formatting tracebacks (that would read source files)."""

        def __init__(self):
            super().__init__()
            self.failfast = True
            self.passed: list = []
            self.failed: list = []
            self.anomaly: str | None = None
            self.cap_check = None

        def startTest(self, test):
            super().startTest(test)
            if self.cap_check:
                self.cap_check()

        def _fail(self, test):
            if not isinstance(test, unittest.TestCase):  # _ErrorHolder: class / module fixture
                self.anomaly = self.anomaly or f"fixture error {test}"
            else:
                self.failed.append(test.id())
            self.stop()

        def addError(self, test, err):
            if err and isinstance(err[1], _Anomaly):
                self.anomaly = self.anomaly or f"guard: {err[1]}"
            self._fail(test)

        def addFailure(self, test, err):
            self._fail(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                if isinstance(err[1], _Anomaly):
                    self.anomaly = self.anomaly or f"guard: {err[1]}"
                self.failed.append(test.id())
                self.stop()

        def addSuccess(self, test):
            self.passed.append(test.id())

        def addSkip(self, test, reason):
            self.anomaly = self.anomaly or f"skip {test.id()}"
            self.stop()

        def addExpectedFailure(self, test, err):
            self.anomaly = self.anomaly or f"expected failure {test.id()}"
            self.stop()

        def addUnexpectedSuccess(self, test):
            self.failed.append(test.id())
            self.stop()

    return Result


def run_plan(plan: list[str], state: dict, cap: float | None = None) -> dict:
    """Run the planned tests in this (child) process → {"passed", "failed", "anomaly", ...}."""
    t0 = time.process_time()
    suite, by_uid = _make_suite(plan)
    t1 = time.process_time()
    result = _result_class()()
    if cap is not None:
        def cap_check():
            if time.process_time() > cap:
                os._exit(3)
        result.cap_check = cap_check
    suite.run(result)
    t2 = time.process_time()
    failed = sorted({by_uid.get(u, u) for u in result.failed})
    passed = [by_uid[u] for u in result.passed if u in by_uid and by_uid[u] not in failed]
    anomaly = result.anomaly or state.get("anomaly")
    if not anomaly and set(by_uid) - set(result.passed) - set(result.failed) and not failed:
        anomaly = "tests not reported"  # nothing failed, yet some test has no verdict
    if not anomaly and set(result.failed) - set(by_uid):
        anomaly = "verdict for an unplanned test"
    if not anomaly:
        anomaly = _missing_imports(state)
    return {"passed": passed, "failed": failed, "anomaly": anomaly,
            "cpu_import": t1 - t0, "cpu_run": t2 - t1}


def _child_main(root, served, patched_paths, plan, cap, wfd) -> None:
    code = 1
    try:
        state = _setup_child(root, served, patched_paths, cap, wfd)
        out = run_plan(plan, state, cap)
        out["cpu_f0"] = state["t_sqlglot"]
        out["cpu_total"] = time.process_time()
        with os.fdopen(wfd, "wb") as fh:
            fh.write(json.dumps(out).encode())
        code = 0
    except BaseException:  # noqa: BLE001
        if os.environ.get("RUNVERIFY_DEBUG"):
            import traceback
            traceback.print_exc()
            sys.stderr.flush()
        code = 1
    finally:
        os._exit(code)


def _proc_cpu(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def fork_run(root: str, served: dict[str, str], patched_paths: set[str], plan: list[str],
             cap: float) -> tuple[dict | None, float]:
    """Fork, run, collect → (result or None, child CPU seconds)."""
    import select as _select
    sys.stdout.flush()
    sys.stderr.flush()
    rfd, wfd = os.pipe()
    t0 = os.times()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        _child_main(root, served, patched_paths, plan, cap, wfd)
    os.close(wfd)
    chunks, idle, last = [], 0, None
    with os.fdopen(rfd, "rb") as fh:
        while True:
            ready, _, _ = _select.select([fh], [], [], 5.0)
            if ready:
                chunk = os.read(fh.fileno(), 1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
                continue
            # No CPU budget can end a child that waits (sleep, lock, pipe): end it only when
            # it has used no CPU for 30 s. A running child is ended by its CPU cap alone.
            now = _proc_cpu(pid)
            idle = idle + 1 if now is not None and now == last else 0
            last = now
            if idle >= 6:
                os.kill(pid, 9)
                chunks = []
                break
    _, status = os.waitpid(pid, 0)
    t1 = os.times()
    cpu = (t1.children_user - t0.children_user) + (t1.children_system - t0.children_system)
    if status != 0:
        return None, cpu
    try:
        return json.loads(b"".join(chunks)), cpu
    except ValueError:
        return None, cpu


# ── static exclusions ──────────────────────────────────────────────────────────

def file_flags(root: Path, rel: str, memo: dict | None = None) -> tuple[bool, set[str]]:
    """(the file or a tests.* module it imports uses a spawn module, names of top-level
    classes / functions that use a resource module or attribute)."""
    memo = {} if memo is None else memo
    if rel in memo:
        return memo[rel]
    memo[rel] = (True, {"*"})  # cycles / errors: conservative
    try:
        tree = ast.parse((root / rel).read_bytes())
    except (OSError, SyntaxError, ValueError):
        return memo[rel]
    spawn, bound = False, set()
    nested_spawn = set()  # top-level classes / functions with a spawn import inside
    for top_node in tree.body:
        inner = isinstance(top_node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for n in ast.walk(top_node):
            mods = []
            if isinstance(n, ast.Import):
                mods = [al.name for al in n.names]
                for al in n.names:
                    if al.name.split(".")[0] in RESOURCE_MODULES:
                        bound.add((al.asname or al.name).split(".")[0])
            elif isinstance(n, ast.ImportFrom) and not n.level:
                mods = [n.module or ""]
                if (n.module or "").split(".")[0] in RESOURCE_MODULES:
                    bound.update(al.asname or al.name for al in n.names)
            for m in mods:
                top = m.split(".")[0]
                hit = top in SPAWN_MODULES
                if top == "tests":
                    cand = m.replace(".", "/")
                    for c in (cand + ".py", cand + "/__init__.py"):
                        if (root / c).is_file() and c != rel:
                            hit = hit or file_flags(root, c, memo)[0]
                if hit and inner:
                    nested_spawn.add(top_node.name)
                elif hit:
                    spawn = True
    res = set()

    def uses(node) -> bool:
        names = {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}
        attrs = {x.attr for x in ast.walk(node) if isinstance(x, ast.Attribute)}
        imps = {a.name.split(".")[0] for x in ast.walk(node) if isinstance(x, ast.Import)
                for a in x.names}
        imps |= {(x.module or "").split(".")[0] for x in ast.walk(node)
                 if isinstance(x, ast.ImportFrom)}
        return bool(names & bound or attrs & RESOURCE_ATTRS or imps & SPAWN_MODULES
                    or imps & RESOURCE_MODULES)

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            # a test method alone -> that test; anything else in the class (helpers,
            # fixtures, class body) -> the whole class
            whole = False
            for item in node.body:
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name.startswith("test")):
                    if uses(item):
                        res.add(f"{node.name}::{item.name}")
                elif uses(item):
                    whole = True
            if whole:
                res.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and uses(node):
            res.add(node.name)
    nested_spawn = set()
    memo[rel] = (spawn, res | nested_spawn)
    return memo[rel]


# ── map build: every eligible test alone, in this harness ──────────────────────

def build(root: Path, tests_json: Path, out: Path, reps: int = 2, max_cpu: float = 3.0) -> None:
    """Per test: CPU alone (median of `reps` runs, each fork after the module is imported) and
    whether it ran cleanly; per file: import CPU after a fresh `import sqlglot`; f0: the child's
    CPU up to and including `import sqlglot`."""
    tests = json.loads(tests_json.read_text())["tests"]
    by_file: dict[str, list[str]] = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["id"])
    # As at run time: the selector has imported (old) sqlglot and the stdlib it uses before it
    # forks (the child drops the sqlglot modules and imports them again).
    sys.path.insert(0, str(root))
    import sqlglot  # noqa: F401
    import sqlglot.optimizer  # noqa: F401
    sys.path.remove(str(root))
    memo: dict = {}
    allowed: set[str] = set()
    res = {"f0": [], "modules": {}, "tests": {}}
    sroot = str(root)
    for f, ids in sorted(by_file.items()):
        spawn, resource_names = file_flags(root, f, memo)
        if spawn:
            for tid in ids:
                res["tests"][tid] = [None, "spawn-module"]
            continue
        todo = []
        for tid in ids:
            parts = tid.split("::")
            if len(parts) != 3:
                res["tests"][tid] = [None, "id"]
            elif ("*" in resource_names or parts[1] in resource_names
                  or f"{parts[1]}::{parts[2]}" in resource_names):
                res["tests"][tid] = [None, "resource-or-spawn-class"]
            else:
                todo.append(tid)
        if not todo:
            continue
        got = _build_file(sroot, f, todo, reps, max_cpu)
        if got is None:
            for tid in todo:
                res["tests"][tid] = [None, "file-error"]
            continue
        res["f0"].extend(got["f0"])
        allowed.update(got["allowed"])
        res["modules"][f] = got["module"]
        for tid in todo:
            res["tests"][tid] = got["tests"].get(tid, [None, "no-result"])
        ok = sum(1 for tid in todo if res["tests"][tid][1] == "ok")
        print(f"{f}: {ok}/{len(todo)} ok, import {got['module']:.3f}s", file=sys.stderr)
    f0 = sorted(res["f0"])
    res["f0"] = f0[len(f0) // 2] if f0 else 1.0
    res["allowed_missing"] = sorted(allowed)
    print(f"failed imports at the pinned commit: {res['allowed_missing']}", file=sys.stderr)
    with gzip.open(out, "wt") as fh:
        json.dump(res, fh, separators=(",", ":"), sort_keys=True)
    n = sum(1 for v in res["tests"].values() if v[1] == "ok")
    print(f"runverify map: {n}/{len(res['tests'])} tests ok, f0 {res['f0']:.3f}s -> {out}",
          file=sys.stderr)


def _build_file(root: str, f: str, ids: list[str], reps: int, max_cpu: float) -> dict | None:
    """In a fork: fresh imports as at run time, import the file, then each test in its own
    grandchild fork (so tests never see each other's state)."""
    import select as _select
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.close(rfd)
            out = {"f0": [], "tests": {}}
            state = _setup_child(root, {}, set(), 600.0, wfd)
            out["f0"].append(state["t_sqlglot"])
            import importlib
            t = time.process_time()
            importlib.import_module(f[:-3].replace("/", "."))
            out["module"] = time.process_time() - t
            if state["anomaly"]:
                raise _Anomaly("module import")
            out["allowed"] = sorted(_missing(state))
            state["allowed"] = set(out["allowed"])
            for tid in ids:
                cpus, status = [], "ok"
                for _ in range(reps):
                    r, cpu = _grandchild(tid, state, max_cpu)
                    if r is None:
                        status = "crash-or-cap"
                        break
                    if r["anomaly"]:
                        status = "anomaly: " + r["anomaly"][:80]
                        break
                    if r["failed"] or r["passed"] != [tid]:
                        status = "fails-at-pin"
                        break
                    cpus.append(cpu)
                out["tests"][tid] = [round(sorted(cpus)[len(cpus) // 2], 4) if cpus else None,
                                     status]
            with os.fdopen(wfd, "wb") as fh:
                fh.write(json.dumps(out).encode())
            code = 0
        except BaseException:  # noqa: BLE001
            if os.environ.get("RUNVERIFY_DEBUG"):
                import traceback
                traceback.print_exc()
                sys.stderr.flush()
            code = 1
        finally:
            os._exit(code)
    os.close(wfd)
    chunks = []
    with os.fdopen(rfd, "rb") as fh:
        while True:
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    _, status = os.waitpid(pid, 0)
    if status != 0:
        return None
    return json.loads(b"".join(chunks))


def _grandchild(tid: str, state: dict, max_cpu: float):
    import signal
    rfd, wfd = os.pipe()
    t0 = os.times()
    state["harness_fork"] = True
    pid = os.fork()
    state["harness_fork"] = False
    if pid == 0:
        code = 1
        try:
            os.close(rfd)
            signal.setitimer(signal.ITIMER_PROF, max_cpu)
            state["imports"], state["anomaly"] = set(), None  # the audit hook's dict
            r = run_plan([tid], state)
            with os.fdopen(wfd, "wb") as fh:
                fh.write(json.dumps(r).encode())
            code = 0
        except BaseException:  # noqa: BLE001
            code = 1
        finally:
            os._exit(code)
    os.close(wfd)
    with os.fdopen(rfd, "rb") as fh:
        data = fh.read()
    _, status = os.waitpid(pid, 0)
    t1 = os.times()
    cpu = (t1.children_user - t0.children_user) + (t1.children_system - t0.children_system)
    if status != 0:
        return None, cpu
    return json.loads(data), cpu


# ── selector side ──────────────────────────────────────────────────────────────

class RunVerify:
    def __init__(self, selector, data: dict) -> None:
        self.sel = selector
        self.f0 = float(data["f0"])
        self.modules = data["modules"]
        self.tests = data["tests"]
        self.spent = 0.0
        ALLOWED_MISSING.update(data.get("allowed_missing", ()))

    @classmethod
    def create(cls, selector):
        if os.environ.get("BR_RUNVERIFY", "1") == "0" or not hasattr(os, "fork"):
            return None
        path = Path(os.environ.get("BR_RUNVERIFY_MAP", DEFAULT_MAP))
        if not path.is_file():
            return None
        with gzip.open(path, "rt") as fh:
            return cls(selector, json.load(fh))

    def _served(self, files) -> tuple[dict[str, str], set[str]] | str:
        """Patched text of every changed file (absolute path → text), or why not."""
        from . import diff
        root = self.sel.sqlglot
        served, paths = {}, set()
        for fp in files:
            if fp.is_binary or not fp.old_path or not fp.new_path or fp.old_path != fp.new_path:
                return "added / deleted / renamed / binary file"
            p = fp.path
            if not p.startswith(SERVABLE_PREFIXES) or Path(p).name in CONFIG_NAMES:
                return f"{p} not servable"
            src = root / p
            if not src.is_file():
                return f"{p} missing"
            old = src.read_text(encoding="utf-8")
            try:
                new = diff.apply(old, fp)
            except Exception:  # noqa: BLE001
                return f"{p} does not apply"
            ap = os.path.realpath(src)
            served[ap] = new
            paths.add(ap)
        return served, paths

    def plan(self, numbers: set[int]) -> tuple[list[str], float] | str:
        """Run order (classes cheapest first, contiguous) and the map-host CPU estimate."""
        by_class: dict[str, list[tuple[float, str]]] = {}
        files = set()
        for n in numbers:
            tid = self.sel._id_of[n]
            entry = self.tests.get(tid)
            if not entry or entry[1] != "ok" or entry[0] is None:
                return f"{tid.split('::')[-1]}: {entry[1] if entry else 'not in map'}"
            f = tid.split("::")[0]
            if f not in self.modules:
                return f"{f}: no import cost"
            files.add(f)
            by_class.setdefault(tid.rsplit("::", 1)[0], []).append((entry[0], tid))
        est = self.f0 + sum(self.modules[f] for f in files) + sum(
            c for v in by_class.values() for c, _ in v)
        order = []
        for _, items in sorted(by_class.items(), key=lambda kv: sum(c for c, _ in kv[1])):
            order.extend(tid for _, tid in sorted(items))
        return order, est

    def prune(self, files, numbers: set[int]) -> tuple[set[int], str]:
        if not numbers:
            return numbers, ""
        got = self.plan(numbers)
        if isinstance(got, str):
            return numbers, f"runverify: skip ({got})"
        order, est = got
        scorer = SCORER_SLOWDOWN * est
        gate = GATE_S
        if SHARE > 0:
            from . import pack
            ms_of = {n: pack.duration_ms(self.sel._secs[n]) for n in numbers}
            gate = max(gate, SHARE * pack.cost_ms(pack.pack(numbers, ms_of), ms_of) / 1000)
        if scorer > gate:
            return numbers, f"runverify: skip (est {scorer:.2f}s scorer CPU)"
        if self.spent + scorer > BATCH_S:
            return numbers, "runverify: skip (batch budget)"
        served = self._served(files)
        if isinstance(served, str):
            return numbers, f"runverify: skip ({served})"
        texts, paths = served
        cap = CAP_FACTOR * est + CAP_EXTRA
        try:
            res, cpu = fork_run(str(self.sel.sqlglot), texts, paths, order, cap)
        except Exception as e:  # noqa: BLE001
            return numbers, f"runverify: keep ({type(e).__name__})"
        self.spent += SCORER_SLOWDOWN * cpu
        if res is None:
            return numbers, f"runverify: keep (child died / CPU cap; est {est:.2f}s, child {cpu:.2f}s)"
        if res.get("anomaly"):
            why = res["anomaly"][:80].replace(";", ",")
            return numbers, f"runverify: keep (anomaly: {why}; est {est:.2f}s, child {cpu:.2f}s)"
        num_of = self.sel._number_of
        passed = {num_of[t] for t in res["passed"] if t in num_of}
        failed = res["failed"]
        if not failed and passed != numbers:
            return numbers, f"runverify: keep (incomplete; est {est:.2f}s, child {cpu:.2f}s)"
        rest = numbers - passed
        return rest, (f"runverify: {len(passed)} passed, {len(failed)} failed {failed[:3]}, {len(rest)} kept "
                      f"(verdicts; est {est:.2f}s, child {cpu:.2f}s)")


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "build":
        build(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]),
              *(int(x) for x in sys.argv[5:6]))
    else:
        sys.exit("usage: python -m selector.runverify build SQLGLOT_ROOT TESTS_JSON OUT.json.gz [REPS]")
