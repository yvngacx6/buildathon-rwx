"""Record the diffexec store (idea I12) inside the map-building image. Called by diffexec_build.sh.

    python /repo/mapbuild/diffexec_run.py --tests /repo/sample/tests.json --work /work \
        --out /repo/maps/diffexec.zip [-j N]

* One pytest process per test FILE (`pytest -p diffexec.record <file>`), run in parallel. A
  file's recording therefore does not depend on which other files ran, or on -j.
* Every function under sqlglot/ is recorded (no list of functions, nothing from labels or
  patches). Caps are applied after merging (diffexec/build.py), so they do not depend on -j.
* Tests that fail or error while recorded are listed; the selector never drops them.
* Reads only tests.json (for the file list). Writes the zip and a "diffexec" entry in
  maps/MANIFEST.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SQLGLOT = Path("/opt/sqlglot")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("-j", type=int, default=os.cpu_count())
    ap.add_argument("--resume", action="store_true",
                    help="keep per-file recordings already in --work (after an interrupted run)")
    a = ap.parse_args()
    repo = Path(a.out).resolve().parent.parent
    work = Path(a.work)
    files = sorted({t["file"] for t in json.loads(Path(a.tests).read_text())["tests"]})
    env = dict(os.environ, PYTHONHASHSEED="0", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=f"{SQLGLOT}:{repo}", DIFFEXEC_OUT=str(work), DIFFEXEC_ROOT=str(SQLGLOT))

    def run(f: str, out: Path = work, extra: dict | None = None):
        tag = f.replace("/", "_").removesuffix(".py")
        t0 = time.time()
        if a.resume and (out / f"rec_{tag}.pkl").is_file():
            return f, 0, True, 0.0, "resumed"
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "diffexec.record",
                               "-p", "no:cacheprovider", f],
                              cwd=SQLGLOT, env=dict(env, DIFFEXEC_TAG=tag, DIFFEXEC_OUT=str(out),
                                                    **(extra or {})),
                              capture_output=True, text=True)
        ok = (out / f"rec_{tag}.pkl").is_file()
        return f, proc.returncode, ok, time.time() - t0, proc.stdout[-300:]

    start = time.time()
    cpu0 = os.times()
    # Pass 1 (census, cheap): suite-wide call count per function. A function over the call cap
    # is dropped after merging anyway, so pass 2 does not record it (same store, far less work).
    census = work / "census"
    census.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(a.j) as pool:
        counted = list(pool.map(lambda f: run(f, census, {"DIFFEXEC_CENSUS": "1"}), files))
    if any(not r[2] for r in counted):
        sys.exit(f"FATAL: census failed for {[r[0] for r in counted if not r[2]]}")
    import pickle
    calls: dict = {}
    for p in census.glob("rec_*.pkl"):
        for key, r in pickle.loads(p.read_bytes())["recs"].items():
            calls[key] = calls.get(key, 0) + r["calls"]
    cap = int(os.environ.get("DIFFEXEC_CALL_CAP", "50000"))
    skip = {k for k, n in calls.items() if n > cap}
    (work / "skip.pkl").write_bytes(pickle.dumps(skip))
    env["DIFFEXEC_SKIP"] = str(work / "skip.pkl")
    print(f"census: {len(calls)} functions, {len(skip)} over {cap} calls (not recorded), "
          f"{time.time() - start:.0f}s", file=sys.stderr)
    with ThreadPoolExecutor(a.j) as pool:
        results = list(pool.map(run, files))
    cpu1 = os.times()
    missing = [r for r in results if not r[2]]
    if missing:
        sys.exit(f"FATAL: no recording for {[m[0] for m in missing]}: {missing[0][4]}")
    for f, rc, _, secs, tail in results:
        if rc not in (0, 1):  # 1 = some test failed (e.g. tracemalloc under recording): listed
            sys.exit(f"FATAL: {f} exited {rc}: {tail}")
    record_wall = time.time() - start
    record_cpu = (cpu1.children_user - cpu0.children_user) + (cpu1.children_system - cpu0.children_system)

    built = subprocess.run([sys.executable, "-m", "diffexec.build", str(work), a.out], cwd=repo,
                           env=env, capture_output=True, text=True, check=True).stdout.strip()
    print(built)
    data = Path(a.out).read_bytes()
    manifest_path = repo / "maps" / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    import ast
    stats = ast.literal_eval(built)
    manifest["diffexec"] = {
        "file": "diffexec.zip",
        "what": "per sqlglot function: deduplicated call inputs seen while each test ran, with a digest "
                "of each call's effect (args after the call + result/exception). Built only from the "
                "pinned commit's own test run; no labels, patches or mutants. See research/ideas/"
                "I12-differential-execution.md.",
        "image": "python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90"
                 " + requirements.lock",
        "sqlglot_sha": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        "env": {"PYTHONHASHSEED": "0", "SKIP_INTEGRATION": "0"},
        "build_command": "mapbuild/diffexec_build.sh",
        "granularity": "one pytest process per test file; caps applied after merging",
        "caps": {"calls_per_function": int(os.environ.get("DIFFEXEC_CALL_CAP", "50000")),
                 "distinct_input_bytes_per_function": int(float(os.environ.get("DIFFEXEC_BYTE_CAP", "300e3"))),
                 "bytes_per_input": int(float(os.environ.get("DIFFEXEC_INPUT_CAP", "16e3")))},
        "test_files": len(files),
        "jobs": a.j,
        "record_wall_seconds": round(record_wall),
        "record_cpu_seconds": round(record_cpu),
        "functions": stats["functions"],
        "functions_recorded": stats["alive"],
        "dead_by_reason": stats["dead_by_reason"],
        "tests_failed_while_recorded": stats["failed_tests"],
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"record wall {record_wall:.0f}s cpu {record_cpu:.0f}s; store {len(data)} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
