"""Differential execution pruner (idea I12). Optional: without a store it does nothing.

For a commit whose sqlglot/*.py edits are all inside function bodies, the selection of those
functions' tests (lines map) is checked by replaying, old code against new code, every call
input that the pinned-commit test run recorded for the changed functions (diffexec/replay.py).

Test T is dropped only if, for every changed function f:
  * f was recorded (not "dead": not too many calls, not too many bytes, not a generator), and
  * every phase whose calls can reach T (T's setup/call/teardown, the setup of every test in
    T's class (setUpClass), and all calls outside tests: import, collection, other) is "ok":
    all its recorded inputs give identical effects under old and new code, and
  * T itself has at least one recorded call of a changed function (otherwise the line map and
    the recording disagree, and we trust neither), and
  * T's test source does not measure memory, time, gc or recursion (diffexec is blind to them).

Everything else is kept. Any error, timeout or budget overrun keeps everything.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Test sources mentioning these observe resources that replay cannot compare.
RESOURCE_MODULES = {"tracemalloc", "gc", "resource", "time", "timeit", "psutil", "threading",
                    "signal", "faulthandler", "pyperf"}
RESOURCE_ATTRS = {"setrecursionlimit", "getrecursionlimit", "getsizeof", "getrefcount",
                  "get_traced_memory", "perf_counter", "process_time", "monotonic"}
COMMIT_BUDGET_S = float(os.environ.get("BR_DIFFEXEC_COMMIT_S", "3"))
BATCH_BUDGET_S = float(os.environ.get("BR_DIFFEXEC_BATCH_S", "60"))
BENEFIT_SHARE = float(os.environ.get("BR_DIFFEXEC_SHARE", "0.3"))


class DiffExec:
    def __init__(self, selector, store: Path) -> None:
        self.sel = selector
        self.store = store
        self.spent = 0.0
        self._idx = None
        self._by_name: dict | None = None
        self._warm = False
        from .pack import duration_ms
        self.ms_of = {t["number"]: duration_ms(t["seconds"]) for t in selector.tests}
        self._resource_files: dict[str, set[str]] = {}
        self._class_of: dict[str, str] = {}
        self._mates: dict[str, list[str]] = {}
        for t in selector.tests:
            parts = t["id"].split("::")
            cls = "::".join(parts[:-1]) if len(parts) > 2 else t["id"]
            self._class_of[t["id"]] = cls
            self._mates.setdefault(cls, []).append(t["id"])

    @classmethod
    def create(cls, selector):
        if os.environ.get("BR_DIFFEXEC", "1") == "0":
            return None
        store = Path(os.environ.get("BR_DIFFEXEC_STORE", REPO / "diffexec_store"))
        if not (store / "index.pkl").is_file():
            return None
        return cls(selector, store)

    # ── run the replay child ────────────────────────────────────────────────

    def _replay(self, jobs, benefit_ms: int = 10_000) -> dict | None:
        # CPU is charged: spend at most ~30 % of what the replay could save (and at most 3 s).
        budget = min(COMMIT_BUDGET_S, BENEFIT_SHARE * benefit_ms / 1000, BATCH_BUDGET_S - self.spent)
        if budget < 0.05:
            return None
        req = {"root": str(self.sel.sqlglot), "store": str(self.store), "deadline_s": budget,
               "files": [{"path": p, "new_text": text} for p, text, _ in jobs]}
        if os.environ.get("PYTHONHASHSEED") == "0" and hasattr(os, "fork"):
            return self._replay_fork(req, budget)
        env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
        t0 = os.times()
        try:
            proc = subprocess.run([sys.executable, "-m", "diffexec.replay"], cwd=REPO, env=env,
                                  input=json.dumps(req), capture_output=True, text=True,
                                  timeout=budget + 5)
        except subprocess.TimeoutExpired:
            return None
        finally:
            t1 = os.times()
            self.last_child = round((t1.children_user - t0.children_user)
                                    + (t1.children_system - t0.children_system), 2)
            self.spent += self.last_child
        if proc.returncode != 0:
            return None
        lines = proc.stdout.strip().splitlines()
        return json.loads(lines[-1]) if lines else None

    def _replay_fork(self, req: dict, budget: float) -> dict | None:
        """Same as the child process, but forked from this process with sqlglot pre-imported
        once (the selector re-executes itself with PYTHONHASHSEED=0, see __main__)."""
        if not self._warm:
            root = str(self.sel.sqlglot)
            if root not in sys.path:
                sys.path.insert(0, root)
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))
            import sqlglot  # noqa: F401
            import sqlglot.optimizer  # noqa: F401
            from diffexec import replay  # noqa: F401
            self._warm = True
        from diffexec import replay
        t0 = os.times()
        rfd, wfd = os.pipe()
        pid = os.fork()
        if pid == 0:  # child: never returns
            code = 1
            try:
                os.close(rfd)
                data = json.dumps(replay.run_request(req)).encode()
                with os.fdopen(wfd, "wb") as fh:
                    fh.write(data)
                code = 0
            except BaseException:  # noqa: BLE001
                if os.environ.get("DIFFEXEC_DEBUG"):
                    import traceback
                    traceback.print_exc()
            finally:
                os._exit(code)
        os.close(wfd)
        chunks = []
        deadline = time.monotonic() + budget + 5
        import select as _select
        status = None
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
        if timed_out:
            status = -1
        t1 = os.times()
        self.last_child = round((t1.children_user - t0.children_user)
                                + (t1.children_system - t0.children_system), 2)
        self.spent += self.last_child
        if status != 0:
            return None
        try:
            return json.loads(b"".join(chunks))
        except ValueError:
            return None

    # ── decide ──────────────────────────────────────────────────────────────

    def _index(self) -> dict:
        if self._idx is None:
            import pickle
            with open(self.store / "index.pkl", "rb") as fh:
                self._idx = pickle.load(fh)
        return self._idx

    def _precheck(self, jobs) -> str | None:
        """Cheap parent-side filter: every changed function must have a live recording.

        Matches the selector's region chains to recorded qualnames (ignoring "<locals>").
        Only decides whether to start the replay child; the child checks everything again.
        """
        index = self._index()
        if self._by_name is None:
            self._by_name = {}
            for (path, q, _), entry in index.items():
                name = q.replace("<locals>.", "")
                self._by_name.setdefault((path, name), []).append(entry)
        for path, _, _ in jobs:
            for chain in self.sel._chains.get(path, []):
                entries = self._by_name.get((path, ".".join(chain)))
                if not entries:
                    return f"{'.'.join(chain) or '<lambda>'}: no recorded calls"
                dead = [e[0] for e in entries if e[0]]
                if dead:
                    return f"{'.'.join(chain)}: not recorded ({dead[0]})"
        return None

    def prune(self, jobs, benefit_ms: int = 1) -> tuple[dict[str, set[int]], str]:
        """jobs: [(path, new text, candidate test numbers)] → ({path: dropped numbers}, note)."""
        t0 = time.process_time()
        if self.sel._mixed_py:
            return {}, "diffexec: skipped (a .py edit outside function bodies)"
        if benefit_ms <= 0:
            return {}, "diffexec: skipped (dropping every candidate would not lower the cost)"
        try:
            why = self._precheck(jobs)
        except Exception as e:  # noqa: BLE001
            why = f"precheck error {type(e).__name__}"
        if why:
            return {}, f"diffexec: keep all ({why})"
        got = self._replay(jobs, benefit_ms)
        if got is None:
            return {}, "diffexec: no answer (error / timeout / budget)"
        if got.get("bail"):
            return {}, f"diffexec: bail ({got['bail']})"
        codes = got.get("codes", {})
        if not codes:
            return {}, "diffexec: no changed code objects"
        if any(c["state"] != "ok" for c in codes.values()):
            states = {k.split("::")[1]: c["state"] for k, c in codes.items() if c["state"] != "ok"}
            return {}, f"diffexec: keep all ({states})"
        global_ok = all(v == "ok" for c in codes.values() for ph, v in c["phases"].items()
                        if "|" not in ph)
        if not global_ok:
            return {}, "diffexec: keep all (a call outside tests differs)"
        bad_tests, seen_tests = set(), set()
        for c in codes.values():
            for ph, v in c["phases"].items():
                if "|" not in ph:
                    continue
                kind, tid = ph.split("|", 1)
                seen_tests.add(tid)
                if v != "ok":
                    if kind == "SETUP":  # setUpClass work reaches the whole class
                        bad_tests.update(self._mates.get(self._class_of.get(tid, tid), [tid]))
                    bad_tests.add(tid)
        dropped: dict[str, set[int]] = {}
        n = 0
        for path, _, numbers in jobs:
            out = set()
            for num in numbers:
                tid = self.sel._id_of[num]
                if tid in bad_tests or tid not in seen_tests or self._is_resource(tid):
                    continue
                # setUpClass of a classmate that ran a changed function must also be ok
                out.add(num)
            dropped[path] = out
            n += len(out)
        stats = got.get("stats", {})
        return dropped, (f"diffexec: dropped {n} "
                         f"(blobs {stats.get('ok')}/{stats.get('blobs')}, child cpu {stats.get('cpu_s')}s, rusage {self.last_child}s, "
                         f"parent {time.process_time() - t0:.2f}s)")

    def _is_resource(self, tid: str) -> bool:
        """Does the test's class (or module-level test function) name a resource probe?

        Coarse on purpose: a use of a resource module (or RESOURCE_ATTRS) anywhere in the test's class marks all
        tests of that class. Files are parsed lazily, only for candidate tests.
        """
        f = tid.split("::")[0]
        hit = self._resource_files.get(f)
        if hit is None:
            hit = set()
            try:
                tree = ast.parse((self.sel.sqlglot / f).read_bytes())
            except (OSError, SyntaxError, ValueError):
                hit = {"*"}
            else:
                bound = set()  # local names bound to resource modules or their members
                for n in ast.walk(tree):
                    if isinstance(n, ast.Import):
                        for al in n.names:
                            if al.name.split(".")[0] in RESOURCE_MODULES:
                                bound.add((al.asname or al.name).split(".")[0])
                    elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in RESOURCE_MODULES:
                        bound.update(al.asname or al.name for al in n.names)
                for node in tree.body:
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                        attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
                        if names & bound or attrs & RESOURCE_ATTRS:
                            hit.add(node.name)
            self._resource_files[f] = hit
        parts = tid.split("::")
        return "*" in hit or (len(parts) > 1 and parts[1] in hit)
