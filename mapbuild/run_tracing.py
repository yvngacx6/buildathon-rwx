"""Trace every numbered test, one process per test, then export the maps.

Runs INSIDE the mapbuild image (see build.sh), with cwd = the sqlglot checkout:
    python /mapbuild/run_tracing.py --tests /repo/sample/tests.json --work /work --out /repo/maps

Steps (each stops the build loudly on failure):
  1. collection check: `pytest --collect-only` ids minus tests.json "skipped" == tests.json ids
  2. trace each id with trace_one.py in its own interpreter, N in parallel
  3. every traced test must have outcome "passed" (a failure at the base commit means
     the tracing hooks changed behaviour)
  4. import_time.json.gz via import_time.py (lines each module's import executes)
  5. export maps (export.py) and MANIFEST.json
"""

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import export  # noqa: E402


def die(msg):
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def collection_check(tests, skipped):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests"],
        capture_output=True,
        text=True,
    )
    collected = {ln.strip() for ln in proc.stdout.splitlines() if "::" in ln}
    expected = {t["id"] for t in tests}
    extra = sorted(collected - expected - set(skipped))
    missing = sorted(expected - collected)
    if extra or missing:
        die(f"collected ids != tests.json ids\n  extra: {extra[:20]}\n  missing: {missing[:20]}")
    print(f"collection check ok: {len(expected)} ids (+{len(collected & set(skipped))} skipped)", file=sys.stderr)


def trace(test, work):
    out = os.path.join(work, "traces", f"{test['number']}.json.gz")
    log = os.path.join(work, "logs", f"{test['number']}.log")
    t0 = time.monotonic()
    with open(log, "w") as fh:
        rc = subprocess.run(
            [sys.executable, os.path.join(HERE, "trace_one.py"), test["id"], out],
            stdout=fh,
            stderr=subprocess.STDOUT,
        ).returncode
    return test, rc, time.monotonic() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", required=True, help="tests.json from the kit")
    ap.add_argument("--work", required=True, help="scratch dir for per-test traces")
    ap.add_argument("--out", required=True, help="maps/ output dir")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()

    t_start = time.monotonic()
    spec = json.load(open(args.tests))
    tests, skipped = spec["tests"], spec.get("skipped", [])
    if [t["number"] for t in tests] != list(range(len(tests))):
        die("tests.json numbers are not 0..n-1 in order")
    collection_check(tests, skipped)

    for sub in ("traces", "logs"):
        os.makedirs(os.path.join(args.work, sub), exist_ok=True)
    order = sorted(tests, key=lambda t: -t["seconds"])  # longest first for load balance
    bad = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        for test, rc, secs in pool.map(lambda t: trace(t, args.work), order):
            done += 1
            path = os.path.join(args.work, "traces", f"{test['number']}.json.gz")
            outcome = export.load_trace(path)["outcome"] if os.path.exists(path) else "no-output"
            if outcome != "passed" or rc != 0:
                bad.append((test["id"], outcome, rc))
            if done % 100 == 0:
                print(f"  traced {done}/{len(tests)}", file=sys.stderr)
    if bad:
        die("tests did not pass under tracing (see logs/):\n" + "\n".join(map(str, bad)))
    trace_secs = time.monotonic() - t_start
    print(f"traced {len(tests)} tests in {trace_secs:.0f}s with {args.jobs} jobs", file=sys.stderr)

    # import_time.json.gz (module -> lines run at its import); see import_time.py
    rc = subprocess.run(
        [sys.executable, os.path.join(HERE, "import_time.py"), os.getcwd(), args.out, str(args.jobs)]
    ).returncode
    if rc != 0:
        die("import_time.py failed")

    export.export(tests, os.path.join(args.work, "traces"), args.out, trace_secs, args.jobs)


if __name__ == "__main__":
    main()
