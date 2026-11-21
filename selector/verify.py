"""Run-time verification of cheap selections (idea I10). Switch: ENABLED (env BR_VERIFY=0 turns it off).

Why
  The static selector answers "which tests could this patch break". When the answer is a few
  cheap tests that in fact all pass, the scorer still charges 2 s per worker plus their recorded
  seconds, while the right answer is `[]` (cost 0). Running those tests here, against the patched
  tree, costs container CPU (charged 1:1) but often less than the simulated seconds it saves.

What it does, per commit with static selection S
  1. Split S into R (tests we may run) and K (tests that stay in the output unrun):
       K = tests with recorded seconds >= SLOW_SECONDS
         ∪ tests in files that start process pools / import duckdb+pandas (POOL_MARKERS):
           their spawn pools size themselves from the host's cores, not --cpus 2, and each
           worker re-imports everything; they are never worth running here.
  2. Cost policy (no knowledge of which tests a patch breaks, only prices):
       saving   = cost(S) - cost(K)        (scorer cost: busiest worker + 2 s per worker)
       expected = (1 - P_FAIL) * saving    (P_FAIL_DATA when the patch touches a non-.py file)
       P_FAIL / P_FAIL_DATA are measured cost priors (how often a run finds a failure, on the
       labeled sets). They only decide whether running is worth the CPU: a wrong prior costs
       seconds, never a miss, because the output is always the run's failures ∪ K, or S.
       run R iff expected > estimated CPU of running R + MARGIN.
  3. Run R with pytest against the patched sqlglot (symlink overlay in /tmp, forked child of a
     server that has pytest pre-imported but never sqlglot) and judge each test with the
     labelers' rules: a test FAILS on any failed report (setup / call / teardown / subtest) or
     a collection error of its file; it PASSES only with a passed `call` report and no failure.
  4. Output = (tests of R that failed) ∪ K. A failing test is never dropped.

  Anything unexpected makes the commit fall back to the full static S: a collection error, a
  test with no report or a skip, a failed report for a node we did not ask for, a pytest exit
  status other than 0/1, a crash, the per-commit kill, patched sqlglot not being the one
  imported, a process pool or child process started by a test we expected not to, a patch
  we cannot apply, or a budget guard. Verification can only remove tests that *passed*.

Guards (the batch must finish in 3 min; the final has 4.5 min for ~1.5x the commits)
  WALL_BUDGET  no verification starts later than this many seconds after the program started.
  HARD_END     a running verification is killed at this point (its commit → static S).
  CPU_BUDGET   total verification CPU; past it the rest of the batch is static.
  Per commit: killed after max(KILL_MIN, KILL_FACTOR x estimate) seconds of wall time (a hung
  test: the commit stays static and the kill bounds the CPU lost).

Fidelity notes
  * The overlay is a symlink farm: /tmp/brv/<n>/{sqlglot,tests,...} link to /opt/sqlglot, and
    only the directories on the path of a patched file are real (filled with links to their
    siblings). Unchanged modules reuse the build-time .pyc (validated by source mtime/size);
    patched ones compile in memory (PYTHONDONTWRITEBYTECODE). A few KB of /tmp per commit.
  * After the run the child checks that `sqlglot` is the overlay's package and that no module
    was imported from the pinned tree itself; otherwise the commit stays static.
  * SKIP_INTEGRATION=0, as the labelers ran.
"""

from __future__ import annotations

import json
import os
import re
import resource
import select as _select
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import diff, pack

ENABLED = os.environ.get("BR_VERIFY", "1") != "0"

SLOW_SECONDS = 5.0
POOL_MARKERS = ("ProcessPoolExecutor", "multiprocessing", "import duckdb", "import pandas")

# Cost model (seconds). OVERHEAD / PER_FILE / PER_SECOND: least-squares fit of the measured child
# CPU of 114 audit runs with R under 3 recorded seconds (EC2 c7i, the scorer's docker flags;
# eval/verify_policy.py). P_FAIL / MARGIN: best realized net when the policy is replayed on the
# audit runs of all four labeled sets (experiments/LOG.md E009).
# Re-measured on the r5 image (E012, 4-vCPU EC2): single-test runs cost 0.20-0.28 s, larger runs
# less than the c7i fit, so OVERHEAD 0.12 -> 0.22 and PER_SECOND 1.4 -> 1.0.
OVERHEAD = float(os.environ.get("BR_VERIFY_OVERHEAD", "0.22"))  # fork + import sqlglot + pytest
PER_FILE = float(os.environ.get("BR_VERIFY_PER_FILE", "0.025"))  # import + collect a test module
PER_SECOND = float(os.environ.get("BR_VERIFY_PER_SECOND", "1.0"))  # CPU per recorded test second
SERVER_START = 0.25  # python + pytest import, paid once per batch if anything is verified
# The batch runs only if its expected gain clears the server start by this much: the model is
# rough (r5-verify, E012: single-test runs cost 0.20-0.27 s, not 0.15), so a batch of one or two
# marginal runs is more often a loss than a gain.
MIN_BATCH_GAIN = float(os.environ.get("BR_VERIFY_MIN_BATCH", "1.0"))
P_FAIL = float(os.environ.get("BR_VERIFY_P_FAIL", "0.6"))
P_FAIL_DATA = float(os.environ.get("BR_VERIFY_P_FAIL_DATA", "0.9"))
MARGIN = float(os.environ.get("BR_VERIFY_MARGIN", "0.0"))

WALL_BUDGET = float(os.environ.get("BR_VERIFY_WALL", "60"))
HARD_END = float(os.environ.get("BR_VERIFY_HARD_END", "90"))
CPU_BUDGET = float(os.environ.get("BR_VERIFY_CPU", "40"))
KILL_MIN = float(os.environ.get("BR_VERIFY_KILL_MIN", "1.0"))
KILL_FACTOR = float(os.environ.get("BR_VERIFY_KILL_FACTOR", "2.0"))  # kill after max(KILL_MIN, KILL_FACTOR x estimate) s

# Eval only (eval/verify_audit.py): verify every runnable commit whose estimate is at most
# AUDIT_MAX_EST seconds, ignoring the cost policy, to check fidelity and calibrate the model.
AUDIT = os.environ.get("BR_VERIFY_AUDIT") == "1"
AUDIT_MAX_EST = float(os.environ.get("BR_VERIFY_AUDIT_MAX_EST", "1e9"))

WORK = "/tmp/brv"


def log(msg: str) -> None:
    print(f"[verify] {msg}", file=sys.stderr, flush=True)


# ── planning (main process) ──────────────────────────────────────────────────────────────────


@dataclass
class Plan:
    commit: str
    patch: str
    static: set[int]  # S
    run: list[int]  # R
    keep: set[int]  # K
    estimate: float  # CPU seconds to run R
    expected: float  # expected simulated seconds saved
    note: str = ""
    result: set[int] | None = field(default=None)


class Planner:
    def __init__(self, tests: list[dict], sqlglot: Path) -> None:
        self.tests = {t["number"]: t for t in tests}
        self.ms = {n: pack.duration_ms(t["seconds"]) for n, t in self.tests.items()}
        self.sqlglot = sqlglot
        self._pool_file: dict[str, bool] = {}

    def cost(self, numbers: set[int]) -> float:
        return pack.cost_ms(pack.pack(numbers, self.ms), self.ms) / 1000

    def runnable(self, number: int) -> bool:
        t = self.tests[number]
        if t["seconds"] >= SLOW_SECONDS:
            return False
        f = t["file"]
        if f not in self._pool_file:
            try:
                src = (self.sqlglot / f).read_text(errors="replace")
                self._pool_file[f] = any(m in src for m in POOL_MARKERS)
            except OSError:
                self._pool_file[f] = True
        return not self._pool_file[f]

    def plan(self, commit: str, patch: str, numbers: set[int]) -> Plan | None:
        if not numbers:
            return None
        run = sorted(n for n in numbers if self.runnable(n))
        if not run:
            return None
        keep = numbers - set(run)
        saving = self.cost(numbers) - self.cost(keep)
        try:
            paths = [fp.path for fp in diff.parse(patch)]
        except Exception:
            return None
        data = any(not p.endswith(".py") for p in paths)
        expected = (1 - (P_FAIL_DATA if data else P_FAIL)) * saving
        files = {self.tests[n]["file"] for n in run}
        estimate = OVERHEAD + PER_FILE * len(files) + PER_SECOND * sum(
            self.tests[n]["seconds"] for n in run)
        return Plan(commit, patch, numbers, run, keep, estimate, expected)


# ── outcome rules (the labelers' parse.py) ──────────────────────────────────────────────────


def norm(nodeid: str) -> str:
    """Drop a trailing ' [subtest msg]' / ' (…)' / '[param]' part, as the labelers do."""
    nodeid = nodeid.split(" [", 1)[0].split(" (", 1)[0]
    return re.sub(r"\[.*\]$", "", nodeid)


def judge(ids: list[str], raw: dict | None) -> tuple[set[str], str]:
    """(failed ids, anomaly). A non-empty anomaly means: trust nothing, output the static set."""
    if raw is None:
        return set(), "no result"
    if raw.get("error"):
        return set(), f"child error: {raw['error']}"
    if raw.get("exit") not in (0, 1):
        return set(), f"pytest exit {raw.get('exit')}"
    if raw.get("collerr"):
        return set(), f"collection error {raw['collerr'][:2]}"
    if raw.get("foreign"):
        return set(), f"not patched code: {raw['foreign'][:2]}"
    if raw.get("spawned"):
        return set(), f"started a process: {raw['spawned']}"
    wanted = set(ids)
    failed, passed, skipped = set(), set(), set()
    for nodeid, when, outcome in raw.get("reports", []):
        n = norm(nodeid)
        if outcome == "failed":
            if n not in wanted:
                return set(), f"failure outside the run set: {nodeid}"
            failed.add(n)
        elif outcome == "skipped":
            skipped.add(n)
        elif when == "call" and outcome == "passed":
            passed.add(n)
    for i in ids:
        if i in failed:
            continue
        if i in skipped:
            return set(), f"skipped: {i}"
        if i not in passed:
            return set(), f"no report: {i}"
    return failed, ""


# ── running (main process side) ─────────────────────────────────────────────────────────────


def refine(plans: list[Plan], tests: list[dict], sqlglot: Path, started: float) -> None:
    """Run the worthwhile plans; set plan.result to the tests to output (None = keep static)."""
    by_id = {t["id"]: t["number"] for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}
    if AUDIT:
        todo = [p for p in plans if p.estimate <= AUDIT_MAX_EST]
    else:
        todo = [p for p in plans if p.expected > p.estimate + MARGIN]
    if todo and not AUDIT:
        # The server start is paid once; skip it all if the total gain does not cover it.
        if sum(p.expected - p.estimate for p in todo) <= max(SERVER_START, MIN_BATCH_GAIN):
            todo = []
    log(f"{len(plans)} runnable, {len(todo)} worth running")
    if not todo:
        return
    todo.sort(key=lambda p: p.expected - p.estimate, reverse=True)
    server = Server(sqlglot)
    spent = 0.0
    try:
        for p in todo:
            now = time.monotonic() - started
            if now > WALL_BUDGET or spent > CPU_BUDGET:
                p.note = "budget"
                log(f"{p.commit}: budget reached ({now:.0f} s wall, {spent:.1f} s CPU), static")
                continue
            limit = min(max(KILL_MIN, KILL_FACTOR * p.estimate), HARD_END - now)
            ids = [id_of[n] for n in p.run]
            raw = server.ask({"patch": p.patch, "ids": ids, "limit": limit})
            spent += raw.get("cpu", 0.0) if raw else 0.0
            failed, anomaly = judge(ids, raw)
            cpu = raw.get("cpu", 0.0) if raw else 0.0
            wall = raw.get("wall", 0.0) if raw else 0.0
            if anomaly:
                p.note = f"static ({anomaly})"
            else:
                p.result = {by_id[i] for i in failed} | p.keep
                p.note = f"ran {len(ids)}, {len(failed)} failed"
            log(f"{p.commit}: {p.note[:160]}; est {p.estimate:.2f} s, cpu {cpu:.2f} s, "
                f"wall {wall:.2f} s, expected saving {p.expected:.2f} s")
    finally:
        server.close()
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    log(f"verification CPU (children so far) {ru.ru_utime + ru.ru_stime:.2f} s")


class Server:
    """`python -m selector.verify <sqlglot>`: one JSON request per line in, one result per line out."""

    def __init__(self, sqlglot: Path) -> None:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0",
                   OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
        env.pop("PYTHONPATH", None)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "selector.verify", str(sqlglot)],
            cwd=Path(__file__).resolve().parent.parent, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def ask(self, request: dict) -> dict | None:
        try:
            self.proc.stdin.write(json.dumps(request) + "\n")
            self.proc.stdin.flush()
            # The server enforces the kill limit itself; this is only a backstop.
            ready, _, _ = _select.select([self.proc.stdout], [], [], request["limit"] + 15)
            line = self.proc.stdout.readline() if ready else ""
            return json.loads(line) if line else None
        except Exception as exc:  # broken pipe, bad JSON: this commit stays static
            log(f"server error {type(exc).__name__}: {exc}")
            return None

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()
            self.proc.wait()


# ── the server (its own process: pytest pre-imported, sqlglot never) ────────────────────────


def _overlay(root: str, src: str, files: dict[str, str | None]) -> None:
    """Symlink farm of `src` at `root` where `files` (repo path → text, None = deleted) differ."""
    os.makedirs(root)
    for name in os.listdir(src):
        os.symlink(f"{src}/{name}", f"{root}/{name}")
    for rel, text in files.items():
        parts = rel.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"bad path {rel}")
        cur = root
        for part in parts[:-1]:
            nxt = f"{cur}/{part}"
            if os.path.islink(nxt):
                target = os.readlink(nxt)
                os.unlink(nxt)
                os.mkdir(nxt)
                if os.path.isdir(target):
                    for entry in os.listdir(target):
                        os.symlink(f"{target}/{entry}", f"{nxt}/{entry}")
            elif not os.path.exists(nxt):
                os.mkdir(nxt)
            cur = nxt
        leaf = f"{cur}/{parts[-1]}"
        if os.path.lexists(leaf):
            os.unlink(leaf)
        if text is not None:
            with open(leaf, "w") as fh:
                fh.write(text)


def _patched_files(src: str, patch: str) -> dict[str, str | None]:
    files: dict[str, str | None] = {}
    for fp in diff.parse(patch):
        if fp.is_binary:
            raise ValueError("binary patch")
        old_text = ""
        if fp.old_path:
            with open(f"{src}/{fp.old_path}", errors="strict") as fh:
                old_text = fh.read()
        if fp.old_path and fp.old_path != fp.new_path:
            files[fp.old_path] = None
        if fp.new_path:
            files[fp.new_path] = diff.apply(old_text, fp) if fp.hunks else old_text
    return files


def _child(root: str, src: str, ids: list[str], out: str) -> None:
    """Runs in the forked child: never returns."""
    result: dict = {"reports": [], "collerr": [], "exit": None, "foreign": [], "spawned": []}
    try:
        os.setpgid(0, 0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        os.chdir(root)
        here = str(Path(__file__).resolve().parent.parent)
        sys.path[:] = [root] + [p for p in sys.path if p not in ("", src, here, os.getcwd())]
        os.environ["PYTHONPATH"] = root

        # A test that starts a process pool or multiprocessing child we did not plan for: refuse
        # it (a spawn pool sized by the host's cores could hit the pid / memory limits) and mark
        # the run as an anomaly, so the commit keeps its static selection.
        import concurrent.futures.process as cfp
        import multiprocessing.process as mpp

        def refuse(what):
            def guard(self, *a, **k):
                result["spawned"].append(what)
                raise RuntimeError(f"selector.verify: {what} refused")
            return guard

        cfp.ProcessPoolExecutor.__init__ = refuse("ProcessPoolExecutor")
        mpp.BaseProcess.start = refuse("multiprocessing.Process")

        import pytest

        class Recorder:
            def pytest_runtest_logreport(self, report):
                result["reports"].append((report.nodeid, report.when, report.outcome))

            def pytest_collectreport(self, report):
                if report.failed:
                    result["collerr"].append(report.nodeid)

            def pytest_sessionfinish(self, session, exitstatus):
                result["exit"] = int(exitstatus)

        rc = pytest.main(["-q", "-p", "no:cacheprovider", "--rootdir", root, "-o", "addopts=",
                          *ids], plugins=[Recorder()])
        result["rc"] = int(rc)
        # sqlglot must be the overlay's. Not pre-imported on purpose: if the patch makes the
        # tests resolve `sqlglot` elsewhere (e.g. deleting tests/__init__.py lets the
        # tests/sqlglot/ shim shadow it), the labelers saw that too, so we must not hide it.
        loaded = getattr(sys.modules.get("sqlglot"), "__file__", None)
        if loaded != f"{root}/sqlglot/__init__.py":
            result["foreign"].append(f"sqlglot from {loaded}")
        for mod in list(sys.modules.values()):
            f = getattr(mod, "__file__", None) or ""
            if f.startswith(src + "/"):  # the pinned tree itself, not the overlay
                result["foreign"].append(f)
    except BaseException as exc:  # noqa: BLE001 - anything at all → the commit stays static
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    try:
        with open(out, "w") as fh:
            json.dump(result, fh)
    finally:
        os._exit(0)


def serve(src: str) -> None:
    import pytest  # noqa: F401  (pre-imported once; every child inherits it)

    assert "sqlglot" not in sys.modules
    sys.path[:] = [p for p in sys.path if p not in ("", src)]
    os.makedirs(WORK, exist_ok=True)
    n = 0
    for line in sys.stdin:
        n += 1
        req = json.loads(line)
        root, out = f"{WORK}/{n}", f"{WORK}/{n}.json"
        t0 = time.monotonic()
        reply: dict = {}
        pid = 0
        try:
            files = _patched_files(src, req["patch"])
            _overlay(root, src, files)
            pid = os.fork()
            if pid == 0:
                _child(root, src, req["ids"], out)
            deadline = t0 + req["limit"]
            rusage = None
            while True:
                got, status, rusage = os.wait4(pid, os.WNOHANG)
                if got:
                    break
                if time.monotonic() > deadline:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _, status, rusage = os.wait4(pid, 0)
                    reply["error"] = f"killed after {req['limit']:.1f} s"
                    break
                time.sleep(0.005)
            reply["cpu"] = rusage.ru_utime + rusage.ru_stime
            if "error" not in reply:
                with open(out) as fh:
                    reply.update(json.load(fh))
        except Exception as exc:  # noqa: BLE001
            reply["error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)  # stray grandchildren, if any
                except (ProcessLookupError, PermissionError):
                    pass
            shutil.rmtree(root, ignore_errors=True)
            if os.path.exists(out):
                os.unlink(out)
        reply["wall"] = time.monotonic() - t0
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    serve(sys.argv[1])
