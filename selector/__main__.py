"""Entry point: read /data, print one JSON line per commit to stdout.

Environment overrides (for local evaluation):
  BR_DATA     data dir with commits/ and tests.json   (default /data)
  BR_SQLGLOT  sqlglot checkout at the pinned commit   (default /opt/sqlglot)
  BR_MAPS     directory with the generated maps       (default <repo>/maps)
  BR_DEADLINE_SCALE  multiplies the time budget (tests: 0.02 forces both late phases)

Safety (research/moonshots/09-final.md R1 / R4): a run that is killed or crashes prints
nothing, which makes it invalid. So every path out of `main` prints exactly one line per
commit file, in order: the commit's selection when it finished, else ALL (cannot miss).
  * Time budget: BUDGET_PER_COMMIT_S × commits of wall time from process start (the
    PYTHONHASHSEED re-exec carries the start in BR_T0), and the same in CPU (this process
    and its reaped children). Past VERIFY_PHASE of it, every run-time verification is off
    (Selector.degrade: those only drop tests, and commits that need the import-state diff
    take ALL); past ALL_PHASE, every remaining commit is ALL. A wall-clock timer interrupts
    the commit that is running when ALL_PHASE arrives (Deadline) and kills every
    descendant process, so a hung child cannot outlive it.
  * Selector construction (maps, indexes) failing → ALL for every commit.
  * SIGTERM → the finished lines plus ALL for the rest, then exit.
"""

from __future__ import annotations

import gc
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import pack

BUDGET_PER_COMMIT_S = 4.5  # the scorer's limit: 4.5 min for ~60 commits
VERIFY_PHASE = 0.40  # past this share of the budget: no run-time verification
ALL_PHASE = 0.70  # past this share: ALL for every remaining commit


class Deadline(BaseException):
    """ALL_PHASE reached while a commit was running (BaseException: no `except Exception`
    in the selector may swallow it)."""


class Output:
    """The run's answer: one line per commit file, written once (`emit`)."""

    def __init__(self) -> None:
        self.commits: list[str] = []
        self.lines: dict[int, str] = {}  # commit index → its finished line
        self.all_workers: list[list[int]] | None = None  # ALL, packed (from tests.json)
        self.done = False
        self.running = False  # `_run` is working on commits: Deadline may interrupt it

    def fallback(self, commit: str) -> str:
        return json.dumps({"commit": commit, "workers": self.all_workers or []})

    def emit(self) -> None:
        """Write every line (finished ones, else ALL) in one go. Only once."""
        if self.done:
            return
        self.done = True
        text = [self.lines.get(i) or self.fallback(c) for i, c in enumerate(self.commits)]
        data = ("\n".join(text) + ("\n" if text else "")).encode()
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        while data:
            try:
                n = os.write(1, data)
            except InterruptedError:
                continue
            except OSError:
                return
            data = data[n:]


class Budget:
    """Wall and CPU time against BUDGET_PER_COMMIT_S × commits."""

    def __init__(self, t0: float, n_commits: int) -> None:
        scale = _float_env("BR_DEADLINE_SCALE", 1.0)
        self.t0 = t0
        self.limit = BUDGET_PER_COMMIT_S * max(n_commits, 1) * scale

    def used(self) -> float:
        """Share of the budget used: the larger of wall and CPU."""
        t = os.times()
        cpu = t.user + t.system + t.children_user + t.children_system
        wall = time.monotonic() - self.t0
        return max(wall, cpu) / self.limit if self.limit > 0 else 1.0

    def until(self, share: float) -> float:
        """Wall seconds until `share` of the budget (≤ 0: already past)."""
        return self.t0 + share * self.limit - time.monotonic()


def main() -> None:
    t0 = _start_time()
    _hashseed_for_diffexec(t0)
    # A short run that builds large syntax trees: the cycle collector would rescan them over
    # and over and find nothing (trees free by reference count). Off saves ~20 % of CPU.
    gc.disable()
    started = time.process_time()
    out = Output()
    main_pid = os.getpid()

    def on_term(signum, frame):  # noqa: ARG001
        if os.getpid() != main_pid:  # a forked child inherited this handler
            os._exit(1)
        if out.done:
            return  # already writing / written: let it finish
        _stop_timer()
        _kill_descendants()
        print(f"signal {signum}: ALL for the unfinished commits", file=sys.stderr)
        out.emit()
        os._exit(0)

    def on_alarm(signum, frame):  # noqa: ARG001
        if os.getpid() != main_pid:
            os._exit(1)
        if out.running and not out.done:
            raise Deadline()

    for sig, handler in ((signal.SIGTERM, on_term), (signal.SIGINT, on_term),
                         (signal.SIGALRM, on_alarm)):
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):
            pass
    try:
        out.running = True
        try:
            _run(out, t0)
        finally:
            out.running = False
    except BaseException as exc:  # noqa: BLE001  (Deadline, a crash, …: ALL for the rest)
        try:
            print(f"stopped: {type(exc).__name__}: {exc}; ALL for the "
                  f"{len(out.commits) - len(out.lines)} unfinished commits", file=sys.stderr)
        except BaseException:  # noqa: BLE001
            pass
    finally:
        _stop_timer()
        _kill_descendants()
        out.emit()
    try:
        print(f"cpu {time.process_time() - started:.2f}s", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _run(out: Output, t0: float) -> None:
    data = Path(os.environ.get("BR_DATA", "/data"))
    sqlglot = Path(os.environ.get("BR_SQLGLOT", "/opt/sqlglot"))
    maps = Path(os.environ.get("BR_MAPS", Path(__file__).resolve().parent.parent / "maps"))

    patches = sorted((data / "commits").glob("*.patch"))
    out.commits = [p.stem for p in patches]
    budget = Budget(t0, len(patches))
    _start_timer(budget.until(ALL_PHASE))

    # ALL from tests.json alone, before anything that may fail (= Selector.all_numbers()).
    tests = json.loads((data / "tests.json").read_text())["tests"]
    ms_of = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
    everything = {t["number"] for t in tests}
    all_workers = pack.pack(everything, ms_of)
    out.all_workers = all_workers
    all_cost = pack.cost_ms(all_workers, ms_of)

    try:
        from .select import ALL_FRACTION, Selector

        selector = Selector(tests, sqlglot, maps)
    except Exception as exc:  # noqa: BLE001  (R4: maps / indexes unreadable → ALL)
        _log(f"selector unavailable ({type(exc).__name__}: {exc}): ALL for every commit")
        return

    degraded = False
    for i, patch_file in enumerate(patches):
        commit = out.commits[i]
        used = budget.used()
        if used >= ALL_PHASE:
            _log(f"[{commit}] ALL (time budget: {used:.0%} used)")
            continue  # `emit` writes ALL
        if used >= VERIFY_PHASE and not degraded:
            _log(f"time budget {used:.0%} used: run-time verification off")
            selector.degrade()
            degraded = True
        try:
            numbers, reason = selector.select(patch_file.read_text(errors="replace"))
            workers = all_workers if numbers == everything else pack.pack(numbers, ms_of)
            if numbers != everything and pack.cost_ms(workers, ms_of) >= ALL_FRACTION * all_cost:
                # Nearly the price of ALL: take ALL, which cannot miss.
                numbers, workers = everything, all_workers
                reason = f"ALL (selection ~ALL cost) {reason}"
        except Exception as exc:  # noqa: BLE001  never crash: any surprise means run everything
            numbers, workers = everything, all_workers
            reason = f"ALL (error: {type(exc).__name__}: {exc})"
        out.lines[i] = json.dumps({"commit": commit, "workers": workers})
        cost = pack.cost_ms(workers, ms_of) / 1000
        _log(f"[{commit}] {len(numbers)} tests, {len(workers)} workers, {cost:.1f}s :: {reason}")


def _log(text: str) -> None:
    """A line on stderr; never fails the run."""
    try:
        print(text, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _start_timer(seconds: float) -> None:
    """SIGALRM at ALL_PHASE, then every 0.5 s (in case something swallows Deadline)."""
    try:
        signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.001), 0.5)
    except (OSError, ValueError, AttributeError):
        pass


def _stop_timer() -> None:
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
    except (OSError, ValueError, AttributeError):
        pass


def _kill_descendants() -> None:
    """SIGKILL every process below this one (forked verifiers, state / genmem subprocesses
    and anything they started), found by walking /proc's parent links. Not a process-group
    kill: locally our group also holds the shell that started us."""
    try:
        me = os.getpid()
        children: dict[int, list[int]] = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as fh:
                    stat = fh.read()
                ppid = int(stat[stat.rindex(b")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(ppid, []).append(int(entry))
        todo, found = [me], []
        while todo:
            for c in children.get(todo.pop(), []):
                if c != me and c not in found:
                    found.append(c)
                    todo.append(c)
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for pid in found:  # reap our own children (no zombies)
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _start_time() -> float:
    """time.monotonic() at process start; carried over the PYTHONHASHSEED re-exec."""
    now = time.monotonic()
    t0 = _float_env("BR_T0", now)
    return t0 if 0 <= now - t0 < 60 else now


def _hashseed_for_diffexec(t0: float) -> None:
    """The diffexec store was recorded with PYTHONHASHSEED=0; replays fork from this process,
    so run it with the same seed (re-exec once, ~20 ms). Nothing else here depends on it."""
    if os.environ.get("PYTHONHASHSEED") == "0" or os.environ.get("BR_DIFFEXEC", "1") == "0":
        return
    store = Path(os.environ.get("BR_DIFFEXEC_STORE",
                                Path(__file__).resolve().parent.parent / "maps" / "diffexec.zip"))
    if not store.is_file():
        return
    try:
        sys.stdout.flush()
        os.execve(sys.executable, list(sys.orig_argv),  # keeps interpreter flags (-S)
                  dict(os.environ, PYTHONHASHSEED="0", BR_T0=repr(t0)))
    except OSError:
        pass  # replays fall back to a child process


if __name__ == "__main__":
    main()
