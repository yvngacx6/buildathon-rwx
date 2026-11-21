"""Build maps/imports.json.gz: for every sqlglot/ and tests/ module, the tests that load it.

Each test runs alone in a fresh pytest process (its file collected, then the test run), with
mapbuild/importtrace/ on PYTHONPATH so every process – the test process, spawned pool
workers, `python -c` subprocesses – records each module it loads (audit "import" event).
A fresh process has nothing cached, so the record is exactly the modules this test needs:
its file's collection-time import closure (eager `import sqlglot` set, test helpers) plus
everything it loads lazily at run time (dialects by name, function-level imports, ...).

Consequence used by the selector: an edit confined to module M can change the outcome only
of tests that load M (or read it as a file: reads map). If M stops importing, exactly those
tests error.

Tests that fail or error when run alone are stored under the key "<isolation-failure>": the
selector adds them to every import-closure selection, since their record may be incomplete.

Usage (pinned sqlglot checkout – use a scratch copy, tests write into the tree):
  SKIP_INTEGRATION=0 python3 mapbuild/import_map.py <tests.json> <sqlglot> <out dir> [-jN] [--keep]
  … [--from=<kept trace dir>]   re-aggregate an earlier --keep run without running tests
  (python = the interpreter with sqlglot's test dependencies; it is also put first on PATH)
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TRACER = Path(__file__).resolve().parent / "importtrace"
FAILED_KEY = "<isolation-failure>"


def run_one(test_id: str, repo: Path, out: Path) -> tuple[int, float]:
    env = dict(os.environ, BR_TRACE_OUT=str(out), PYTHONPATH=str(TRACER),
               SKIP_INTEGRATION=os.environ.get("SKIP_INTEGRATION", "0"),
               PATH=f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}")
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "--no-header",
                           "-p", "no:cacheprovider", test_id],
                          cwd=repo, env=env, capture_output=True, text=True)
    (out / "returncode").write_text(str(proc.returncode))
    if proc.returncode != 0:
        (out / "pytest.log").write_text(proc.stdout[-4000:] + proc.stderr[-4000:])
    return proc.returncode, time.time() - t0


def modules_of(out: Path, repo: Path) -> set[str]:
    """Repo paths (sqlglot/…, tests/…) of every module any process of this test loaded."""
    names: set[str] = set()
    files: dict[str, str] = {}
    for f in out.glob("*.txt"):
        for line in f.read_text(errors="replace").splitlines():
            kind, _, rest = line.partition(" ")
            if kind == "import":
                names.add(rest)
            elif kind == "file":
                name, _, path = rest.partition(" ")
                files[name] = path
                names.add(name)  # loaded without an audit event (pytest imports test files itself)
    root = str(repo.resolve()) + os.sep
    paths: set[str] = set()
    for name in names:
        f = files.get(name)
        if f is not None:  # any module whose file is in the repo (also `python -m` __main__)
            f = os.path.realpath(f)
            if f.startswith(root) and f.endswith(".py"):
                paths.add(f[len(root):])
            continue
        if not name.startswith(("sqlglot", "tests")):
            continue
        base = repo / name.replace(".", "/")  # killed pool worker: no file record
        for cand in (base.with_suffix(".py"), base / "__init__.py"):
            if cand.is_file():
                paths.add(cand.relative_to(repo).as_posix())
                break
    return paths


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    jobs = next((int(a[2:]) for a in sys.argv[1:] if a.startswith("-j")), 8)
    tests_json, repo, out_dir = Path(args[0]), Path(args[1]).resolve(), Path(args[2])
    ids = [t["id"] for t in json.loads(tests_json.read_text())["tests"]]
    reuse = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--from=")), None)
    work = Path(reuse) if reuse else Path(tempfile.mkdtemp(prefix="importmap-"))

    def job(i: int) -> tuple[int, float]:
        d = work / str(i)
        d.mkdir()
        return run_one(ids[i], repo, d)

    started = time.time()
    if reuse:  # aggregate the raw traces of an earlier --keep run again
        results = [(int((work / str(i) / "returncode").read_text()), 0.0) for i in range(len(ids))]
    else:
        with ThreadPoolExecutor(jobs) as pool:
            results = list(pool.map(job, range(len(ids))))
    by_module: dict[str, list[int]] = {}
    failed = []
    for i, (code, _) in enumerate(results):
        if code != 0:
            failed.append(i)
            print(f"isolation failure ({code}): {ids[i]}", file=sys.stderr)
        own = ids[i].split("::")[0]  # its own file is always collected
        for path in modules_of(work / str(i), repo) | {own}:
            by_module.setdefault(path, []).append(i)
    by_module[FAILED_KEY] = failed
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {"tests": ids, "map": dict(sorted(by_module.items()))}
    (out_dir / "imports.json.gz").write_bytes(
        gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(ids)} tests, {len(by_module) - 1} modules, {len(failed)} isolation failures, "
          f"{time.time() - started:.0f}s; raw traces in {work}", file=sys.stderr)
    if "--keep" not in sys.argv and not reuse:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
