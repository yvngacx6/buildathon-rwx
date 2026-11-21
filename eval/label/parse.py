#!/usr/bin/env python3
"""Turn raw outcome logs into test numbers.

usage: parse.py <tests.json> <out_dir> [ids...]   -> prints JSON {id: {"failing": [...], "missing": [...], "status": ...}}

A tests.json test FAILS when any of:
  - any report for it (setup/call/teardown, or a subtest report) has outcome "failed"
    (pytest-timeout and crashed xdist workers also produce failed reports);
  - its file (or a parent dir) has a failed collection report (import/collection error);
  - it has no passed/skipped report at all ("missing": not collected, renamed/deleted, run aborted).
Node ids are normalised by dropping a trailing " [subtest msg]" / "[param]" part.
Tests not in tests.json are ignored.
"""
import json, re, sys
from pathlib import Path

def norm(nid):
    nid = nid.split(" [", 1)[0].split(" (", 1)[0]
    return re.sub(r"\[.*\]$", "", nid)

def label(tests, path):
    fail, seen, collerr, ended = set(), set(), [], False
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r["k"] == "end":
            ended = True
        elif r["k"] == "c":
            collerr.append(r["id"])
        else:
            n = norm(r["id"])
            if r["o"] == "failed":
                fail.add(n)
            elif r["when"] == "call" or r["o"] == "skipped":
                seen.add(n)
    by_id = {t["id"]: t for t in tests}
    failing, missing = set(), set()
    for t in tests:
        tid, f = t["id"], t["file"]
        if tid in fail or any(f == c or f.startswith(c.rstrip("/") + "/") or c == "" for c in collerr):
            failing.add(t["number"])
        elif tid not in seen:
            missing.add(t["number"])
    return {"failing": sorted(failing | missing), "missing": sorted(missing), "collerr": collerr,
            "ended": ended, "unknown_failed": sorted(n for n in fail if n not in by_id)[:20]}

if __name__ == "__main__":
    tests = json.loads(Path(sys.argv[1]).read_text())["tests"]
    out = Path(sys.argv[2])
    ids = sys.argv[3:] or sorted(p.stem for p in out.glob("*.done"))
    res = {}
    for i in ids:
        done = (out / f"{i}.done").read_text().strip() if (out / f"{i}.done").exists() else "notdone"
        if done == "applyfail" or not (out / f"{i}.jsonl").exists():
            res[i] = {"status": done}
            continue
        res[i] = dict(label(tests, out / f"{i}.jsonl"), status=done)
    print(json.dumps(res))
