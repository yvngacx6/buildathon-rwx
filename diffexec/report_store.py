"""Summarise recordings: coverage of functions / calls / changed functions of a labeled set.

    python -m diffexec.report_store <rec dir> [qualname,qualname,...]
"""

from __future__ import annotations

import collections
import sys

from diffexec.build import merge


def main() -> None:
    merged, stats = merge(sys.argv[1])
    targets = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    dead = collections.Counter(m["dead"] for m in merged.values())
    calls = sum(m["calls"] for m in merged.values())
    alive_calls = sum(m["calls"] for m in merged.values() if not m["dead"])
    raw = sum(len(b[0]) for m in merged.values() for b in m["blobs"].values())
    secs = sum(m.get("secs", 0) for m in merged.values())
    print(f"workers={stats['workers']} pickle_s={stats['pickle_s']:.0f} recorded_calls={stats['recorded']}")
    print(f"functions={len(merged)} dead={dict(dead)}")
    print(f"calls seen={calls} in recorded functions={alive_calls}  distinct-input raw bytes={raw / 1e6:.1f} MB")
    bad = collections.Counter(b[2] for m in merged.values() for b in m["blobs"].values())
    print(f"blob status={dict(bad)}")
    inc = sum(1 for m in merged.values() for v in m["uses"].values() if v is None)
    print(f"incomplete (function, phase) pairs={inc}")
    for tq in targets:
        for k, m in merged.items():
            if k[1] == tq or k[1].endswith("." + tq):
                print(f"  {k[0]}:{k[1]} calls={m['calls']} dead={m['dead']} blobs={len(m['blobs'])} "
                      f"phases={len(m['uses'])} incomplete={sum(v is None for v in m['uses'].values())}")


if __name__ == "__main__":
    main()
