"""Entry point: read /data, print one JSON line per commit to stdout.

Environment overrides (for local evaluation):
  BR_DATA     data dir with commits/ and tests.json   (default /data)
  BR_SQLGLOT  sqlglot checkout at the pinned commit   (default /opt/sqlglot)
  BR_MAPS     directory with the generated maps       (default <repo>/maps)
"""

from __future__ import annotations

import gc
import json
import os
import sys
import time
from pathlib import Path

from . import pack
from .select import ALL_FRACTION, Selector


def main() -> None:
    _hashseed_for_diffexec()
    # A short run that builds large syntax trees: the cycle collector would rescan them over
    # and over and find nothing (trees free by reference count). Off saves ~20 % of CPU.
    gc.disable()
    started = time.process_time()
    data = Path(os.environ.get("BR_DATA", "/data"))
    sqlglot = Path(os.environ.get("BR_SQLGLOT", "/opt/sqlglot"))
    maps = Path(os.environ.get("BR_MAPS", Path(__file__).resolve().parent.parent / "maps"))

    tests = json.loads((data / "tests.json").read_text())["tests"]
    ms_of = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
    selector = Selector(tests, sqlglot, maps)

    everything = selector.all_numbers()
    all_workers = pack.pack(everything, ms_of)
    all_cost = pack.cost_ms(all_workers, ms_of)

    lines = []
    recs = []  # DIAGNOSTIC telemetry only (BR_TELEMETRY)
    for patch_file in sorted((data / "commits").glob("*.patch")):
        commit = patch_file.stem
        try:
            numbers, reason = selector.select(patch_file.read_text(errors="replace"))
        except Exception as exc:  # never crash: any surprise means run everything
            numbers, reason = selector.all_numbers(), f"ALL (error: {type(exc).__name__}: {exc})"
        workers = all_workers if numbers == everything else pack.pack(numbers, ms_of)
        if numbers != everything and pack.cost_ms(workers, ms_of) >= ALL_FRACTION * all_cost:
            # Nearly the price of ALL: take ALL, which cannot miss.
            numbers, workers = everything, all_workers
            reason = f"ALL (selection ~ALL cost) {reason}"
        cost = pack.cost_ms(workers, ms_of) / 1000
        print(f"[{commit}] {len(numbers)} tests, {len(workers)} workers, {cost:.1f}s :: {reason}",
              file=sys.stderr)
        lines.append(json.dumps({"commit": commit, "workers": workers}))
        if os.environ.get("BR_TELEMETRY"):
            recs.append(_record(commit, numbers, workers, reason, everything, tests, ms_of))

    if os.environ.get("BR_TELEMETRY"):
        from . import telemetry  # DIAGNOSTIC ONLY: never set in a final candidate
        try:
            out = telemetry.emit(os.environ["BR_TELEMETRY"], recs, ms_of)
            lines = [json.dumps({"commit": r["commit"], "workers": w}) for r, w in zip(recs, out)]
        except Exception as exc:  # noqa: BLE001 - keep the real output (board then shows the real score)
            print(f"telemetry failed, real output kept: {exc}", file=sys.stderr)

    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    print(f"cpu {time.process_time() - started:.2f}s", file=sys.stderr)


def _record(commit, numbers, workers, reason, everything, tests, ms_of):
    """Per-commit facts for the telemetry diagnostic (selector/telemetry.py)."""
    by_name = {t["id"].split("::")[-1]: t["number"] for t in tests}
    slow = {t["number"] for t in tests if t["seconds"] >= 3.0}
    cost = pack.cost_ms(workers, ms_of)

    def gain(drop):
        rest = numbers - drop
        return cost - (pack.cost_ms(pack.pack(rest, ms_of), ms_of) if rest else 0)

    return {"commit": commit, "numbers": numbers, "workers": workers, "reason": reason,
            "all": numbers == everything, "cost": cost, "slow": slow,
            "g_slow": gain(slow), "g_maxnodes": gain({by_name.get("test_max_nodes")}),
            "g_tpcds": gain({by_name.get("test_execute_tpcds")})}


def _hashseed_for_diffexec() -> None:
    """The diffexec store was recorded with PYTHONHASHSEED=0; replays fork from this process,
    so run it with the same seed (re-exec once, ~20 ms). Nothing else here depends on it."""
    if os.environ.get("PYTHONHASHSEED") == "0" or os.environ.get("BR_DIFFEXEC", "1") == "0":
        return
    store = Path(os.environ.get("BR_DIFFEXEC_STORE",
                                Path(__file__).resolve().parent.parent / "maps" / "diffexec.zip"))
    if not store.is_file():
        return
    try:
        sys.stdout.flush()
        os.execve(sys.executable, list(sys.orig_argv),  # keeps interpreter flags (-S)
                  dict(os.environ, PYTHONHASHSEED="0"))
    except OSError:
        pass  # replays fall back to a child process


if __name__ == "__main__":
    main()
