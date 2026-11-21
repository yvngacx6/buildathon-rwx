"""Entry point: read /data, print one JSON line per commit to stdout.

Environment overrides (for local evaluation):
  BR_DATA     data dir with commits/ and tests.json   (default /data)
  BR_SQLGLOT  sqlglot checkout at the pinned commit   (default /opt/sqlglot)
  BR_MAPS     directory with the generated maps       (default <repo>/maps)
  BR_VERIFY   0 turns off run-time verification of cheap selections (selector/verify.py)
"""

from __future__ import annotations

import gc
import json
import os
import sys
import time
from pathlib import Path

from . import pack, verify
from .select import ALL_FRACTION, Selector


def main() -> None:
    # A short run that builds large syntax trees: the cycle collector would rescan them over
    # and over and find nothing (trees free by reference count). Off saves ~20 % of CPU.
    gc.disable()
    started = time.process_time()
    wall_start = time.monotonic()
    data = Path(os.environ.get("BR_DATA", "/data"))
    sqlglot = Path(os.environ.get("BR_SQLGLOT", "/opt/sqlglot"))
    maps = Path(os.environ.get("BR_MAPS", Path(__file__).resolve().parent.parent / "maps"))

    tests = json.loads((data / "tests.json").read_text())["tests"]
    ms_of = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
    selector = Selector(tests, sqlglot, maps)

    everything = selector.all_numbers()
    all_workers = pack.pack(everything, ms_of)
    all_cost = pack.cost_ms(all_workers, ms_of)

    # Pass 1: static selections. Pass 2 (selector/verify.py, optional): run cheap ones.
    rows = []
    for patch_file in sorted((data / "commits").glob("*.patch")):
        commit = patch_file.stem
        patch = patch_file.read_text(errors="replace")
        try:
            numbers, reason = selector.select(patch)
        except Exception as exc:  # never crash: any surprise means run everything
            numbers, reason = selector.all_numbers(), f"ALL (error: {type(exc).__name__}: {exc})"
        if numbers != everything and pack.cost_ms(pack.pack(numbers, ms_of), ms_of) >= (
                ALL_FRACTION * all_cost):
            # Nearly the price of ALL: take ALL, which cannot miss.
            numbers, reason = everything, f"ALL (selection ~ALL cost) {reason}"
        rows.append((commit, patch, numbers, reason))

    verified = {}
    if verify.ENABLED:
        try:
            planner = verify.Planner(tests, sqlglot)
            plans = [p for p in (planner.plan(c, patch, n) for c, patch, n, _ in rows) if p]
            verify.refine(plans, tests, sqlglot, wall_start)
            verified = {p.commit: p for p in plans if p.result is not None}
        except Exception as exc:  # verification is an optimisation: on any error keep static
            print(f"verify failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            verified = {}

    lines = []
    for commit, _, numbers, reason in rows:
        if commit in verified:
            numbers, reason = verified[commit].result, f"verified: {verified[commit].note}; {reason}"
        workers = all_workers if numbers == everything else pack.pack(numbers, ms_of)
        cost = pack.cost_ms(workers, ms_of) / 1000
        print(f"[{commit}] {len(numbers)} tests, {len(workers)} workers, {cost:.1f}s :: {reason}",
              file=sys.stderr)
        lines.append(json.dumps({"commit": commit, "workers": workers}))

    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    print(f"cpu {time.process_time() - started:.2f}s", file=sys.stderr)
    _report_container_cpu()


def _report_container_cpu() -> None:
    """Log what the scorer charges: CPU of every process in the container (cgroup v2), if visible."""
    try:
        stat = Path("/sys/fs/cgroup/cpu.stat").read_text()
        usec = int(stat.split("usage_usec", 1)[1].split()[0])
        print(f"container cpu {usec / 1e6:.2f}s", file=sys.stderr)
    except Exception:
        pass


if __name__ == "__main__":
    main()
