"""Merge per-test traces into the map files described in maps/SCHEMA.md.

Every map file is {"tests": [ids...], "map": {key: [test index, ...]}}. The tests
array is tests.json in number order, so an index here equals the test's number.
Output is byte-for-byte deterministic: keys sorted, lists sorted, gzip mtime 0.
"""

import datetime
import gzip
import hashlib
import io
import json
import os

SQLGLOT_SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
IMAGE = "python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90"
BUILD_COMMAND = "mapbuild/build.sh"
IMPORT_TIME_NOTE = (
    "import_time.json.gz: mapbuild/import_time.py, one fresh interpreter per module under "
    "sqlglot/ and tests/, same image; its 'tests' array lists module roots, not tests"
)


def load_trace(path):
    with gzip.open(path, "rt") as fh:
        return json.load(fh)


def _line_key(key):
    path, _, line = key.rpartition(":")
    return path, int(line)


def write_map(path, ids, mapping, sort_key=None):
    """Write one map; returns the sha256 of the uncompressed JSON (for determinism checks)."""
    keys = sorted(mapping, key=sort_key)
    body = {"tests": ids, "map": {k: sorted(mapping[k]) for k in keys}}
    raw = json.dumps(body, separators=(",", ":")).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=0) as gz:
        gz.write(raw)
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return hashlib.sha256(raw).hexdigest()


def export(tests, trace_dir, out_dir, trace_secs, jobs):
    ids = [t["id"] for t in tests]
    lines, lines_phase, lines_full, classes, reads, imports = {}, {}, {}, {}, {}, {}
    for t in tests:
        n = t["number"]
        tr = load_trace(os.path.join(trace_dir, f"{n}.json.gz"))
        if tr["id"] != t["id"]:
            raise SystemExit(f"FATAL: trace {n} is for {tr['id']}, expected {t['id']}")
        for field, store in (("lines", lines), ("lines_phase", lines_phase), ("lines_full", lines_full)):
            for f, lns in tr[field].items():
                for ln in lns:
                    store.setdefault(f"{f}:{ln}", []).append(n)
        for c in tr["classes"]:
            classes.setdefault(c, []).append(n)
        for r in tr["reads"]:
            reads.setdefault(r, []).append(n)
        for m in tr["test_modules"]:
            imports.setdefault(m, []).append(n)

    os.makedirs(out_dir, exist_ok=True)
    files = {
        "lines.json.gz": (lines, _line_key),
        "lines_phase.json.gz": (lines_phase, _line_key),
        "lines_full.json.gz": (lines_full, _line_key),
        "classes.json.gz": (classes, None),
        "reads.json.gz": (reads, None),
        "test_imports.json.gz": (imports, None),
    }
    digests, sizes = {}, {}
    for name, (mapping, key) in files.items():
        p = os.path.join(out_dir, name)
        digests[name] = write_map(p, ids, mapping, key)
        sizes[name] = os.path.getsize(p)
    keys = {name: len(m) for name, (m, _) in files.items()}

    # import_time.json.gz is written by import_time.py (run_tracing.py runs it first)
    it = os.path.join(out_dir, "import_time.json.gz")
    if os.path.exists(it):
        raw = gzip.decompress(open(it, "rb").read())
        keys["import_time.json.gz"] = len(json.loads(raw)["map"])
        sizes["import_time.json.gz"] = os.path.getsize(it)
        digests["import_time.json.gz"] = hashlib.sha256(raw).hexdigest()

    manifest = {
        "sqlglot_sha": SQLGLOT_SHA,
        "image": IMAGE,
        "env": {"SKIP_INTEGRATION": "0", "PYTHONHASHSEED": "0"},
        "tests_traced": len(ids),
        "granularity": "one interpreter per test",
        "import_time": IMPORT_TIME_NOTE,
        "build_command": BUILD_COMMAND,
        "built_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trace_wall_seconds": round(trace_secs),
        "jobs": jobs,
        "keys": keys,
        "bytes": sizes,
        "json_sha256": digests,
    }
    with open(os.path.join(out_dir, "MANIFEST.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
        fh.write("\n")
    for name in keys:
        print(f"  {name}: {keys[name]} keys, {sizes[name] / 1e6:.2f} MB", flush=True)
