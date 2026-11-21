"""Write maps/classes_run.json.gz from per-test traces (trace_one.py "classes_run").

Usage: python mapbuild/classes_run.py <tests.json> <traces dir> <maps dir>

A test with no trace (or a trace without "classes_run") keeps its row of the existing
classes.json.gz: the map then over-approximates for it, never under.
"""

import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from export import write_map  # noqa: E402


def main(tests_json, trace_dir, maps_dir):
    tests = json.load(open(tests_json))["tests"]
    ids = [t["id"] for t in tests]
    old = json.loads(gzip.decompress(open(os.path.join(maps_dir, "classes.json.gz"), "rb").read()))
    old_rows = {}
    for key, idx in old["map"].items():
        for i in idx:
            old_rows.setdefault(old["tests"][i], set()).add(key)
    out, traced, fallback = {}, 0, 0
    for i, t in enumerate(tests):
        path = os.path.join(trace_dir, f"{t['number']}.json.gz")
        row = None
        if os.path.exists(path):
            try:
                tr = json.load(gzip.open(path, "rt"))
                if tr["id"] == t["id"] and "classes_run" in tr:
                    row = set(tr["classes_run"])
                    extra = row - set(tr["classes"])
                    if extra:
                        raise SystemExit(f"{t['id']}: classes_run not within classes: {extra}")
            except (OSError, ValueError, EOFError):
                row = None
        if row is None:
            row = old_rows.get(t["id"], set())
            fallback += 1
        else:
            traced += 1
        for c in row:
            out.setdefault(c, []).append(i)
    write_map(os.path.join(maps_dir, "classes_run.json.gz"), ids, out, None)
    print(f"classes_run: {len(out)} keys, {traced} traced, {fallback} from classes.json.gz",
          file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:4])
