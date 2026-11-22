"""Merge two key-lookup traces (mapbuild/keytrace.py) into maps/keys.json.gz.

Order matters for the tracer's "fed" heuristic too (see below). The second trace runs the tests in reverse order with another hash seed. A table whose
records differ between the two runs depends on test order (a cache filled by its first user)
or on hashing: its entries join "mutated" and are never narrowed. The others take the union.

Tests that failed in either run (order-dependent tests of the suite itself: they take another
path when they fail) are left out of the comparison; their records still join the union.

usage: python mapbuild/keymerge.py A.json.gz B.json.gz OUT.json.gz [failed test id]...
"""

import gzip
import json
import sys


def load(p):
    with gzip.open(p, "rt") as f:
        return json.load(f)


def main(a_path, b_path, out_path, *failed):
    a, b = load(a_path), load(b_path)
    assert a["tests"] == b["tests"], "different test lists"
    skip = {i for i, t in enumerate(a["tests"]) if t in set(failed)}

    def same(x, y):
        return sorted(set(x) - skip) == sorted(set(y) - skip)
    # "fed": read whole by a test that also wrote an import-time dict (maybe filling a
    # cache). A table that really feeds a cache is read so by the cache's first user in
    # BOTH orders; one flagged in a single order is a coincidence of that order. (A cache
    # that is not an import-time dict shows up as an unstable record instead.)
    fed = set(a.get("fed", ())) & set(b.get("fed", ()))
    bad = set(a["leaky"]) | set(b["leaky"]) | set(a["mutated"]) | set(b["mutated"]) | fed
    entries = {}
    unstable = set()
    for e in a["entries"].keys() | b["entries"].keys():
        pa, pb = a["entries"].get(e), b["entries"].get(e)
        if pa is None or pb is None:
            unstable.add(e)
            continue
        merged = {}
        for path in pa.keys() | pb.keys():
            ra, rb = pa.get(path), pb.get(path)
            if ra is None or rb is None:
                unstable.add(e)
                continue
            if not same(ra["W"], rb["W"]) or any(
                    not same(ra["K"].get(k, ()), rb["K"].get(k, ()))
                    for k in ra["K"].keys() | rb["K"].keys()):
                unstable.add(e)
            keys = {k: sorted(set(ra["K"].get(k, ())) | set(rb["K"].get(k, ())))
                    for k in ra["K"].keys() | rb["K"].keys()}
            merged[path] = {"W": sorted(set(ra["W"]) | set(rb["W"])), "K": keys}
        entries[e] = merged
    out = {"tests": a["tests"], "leaky": sorted(set(a["leaky"]) | set(b["leaky"])),
           "mutated": sorted(bad - set(a["leaky"]) - set(b["leaky"]) | unstable),
           "unstable": sorted(unstable), "entries": entries}
    with gzip.open(out_path, "wt") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"entries {len(entries)}, unstable {len(unstable)}, never narrowed {len(out['mutated']) + len(out['leaky'])}")
    for e in sorted(unstable)[:40]:
        print("  unstable:", e)


if __name__ == "__main__":
    main(*sys.argv[1:])
