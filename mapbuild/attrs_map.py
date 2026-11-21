"""moon-19: build maps/attrs.json.gz with trace_attrs.py, one interpreter per test.

usage (cwd = sqlglot checkout, SKIP_INTEGRATION=0):
    python attrs_map.py --tests tests.json --work <scratch> --out <maps dir> [-j N]

attrs.json.gz = {"tests": [ids], "map": {"module:Qual NAME": [test idx]},
                 "tracked": {"module:Qual": [names]}, "untracked": {"module:Qual": [names]},
                 "replaced": [test idx], "failed": [test idx]}
A test is in "replaced" (some tracked entry was rebound during it) or "failed" (outcome
differs from passed under the tracer): the selector treats both as reading every entry.
"""
import argparse
import concurrent.futures
import gzip
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def trace(test, work):
    out = os.path.join(work, "attrs", f"{test['number']}.json.gz")
    if os.path.exists(out):  # resumable
        return test, 0, out
    with open(os.path.join(work, "attrlogs", f"{test['number']}.log"), "w") as fh:
        rc = subprocess.run([sys.executable, os.path.join(HERE, "trace_attrs.py"), test["id"], out],
                            stdout=fh, stderr=subprocess.STDOUT).returncode
    return test, rc, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()
    tests = json.load(open(args.tests))["tests"]
    for sub in ("attrs", "attrlogs"):
        os.makedirs(os.path.join(args.work, sub), exist_ok=True)
    t0 = time.monotonic()
    ids = [t["id"] for t in tests]
    keymap: dict[str, list[int]] = {}
    tracked: dict[str, set] = {}
    untracked: dict[str, set] = {}
    replaced, failed = [], []
    order = sorted(tests, key=lambda t: -t["seconds"])
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        for n, (test, rc, out) in enumerate(pool.map(lambda t: trace(t, args.work), order), 1):
            i = test["number"]
            try:
                rec = json.load(gzip.open(out, "rt"))
            except (OSError, ValueError):
                failed.append(i)
                continue
            if rec["outcome"] != "passed":
                failed.append(i)
            if rec["replaced"]:
                replaced.append(i)
            for k in rec["reads"]:
                keymap.setdefault(k, []).append(i)
            for cls, (tr, other) in rec["defs"].items():
                tracked.setdefault(cls, set()).update(tr)
                untracked.setdefault(cls, set()).update(other)
            if n % 200 == 0:
                print(f"  {n}/{len(tests)} {time.monotonic() - t0:.0f}s", file=sys.stderr, flush=True)
    # a name tracked in one run and not in another (should not happen): untrusted
    for cls in tracked:
        both = tracked[cls] & untracked.get(cls, set())
        tracked[cls] -= both
    out = {"tests": ids, "map": {k: sorted(v) for k, v in sorted(keymap.items())},
           "tracked": {k: sorted(v) for k, v in sorted(tracked.items())},
           "untracked": {k: sorted(v) for k, v in sorted(untracked.items())},
           "replaced": sorted(replaced), "failed": sorted(failed)}
    os.makedirs(args.out, exist_ok=True)
    with gzip.open(os.path.join(args.out, "attrs.json.gz"), "wt", compresslevel=9) as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"attrs map: {len(keymap)} keys, {len(failed)} failed, {len(replaced)} replaced, "
          f"{time.monotonic() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
