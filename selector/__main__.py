"""Entry point: read /data, print one JSON line per commit to stdout.

Environment overrides (for local evaluation):
  BR_DATA     data dir with commits/ and tests.json   (default /data)
  BR_SQLGLOT  sqlglot checkout at the pinned commit   (default /opt/sqlglot)
  BR_MAPS     directory with the generated maps       (default <repo>/maps)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import pack
from .select import Selector


def main() -> None:
    started = time.process_time()
    data = Path(os.environ.get("BR_DATA", "/data"))
    sqlglot = Path(os.environ.get("BR_SQLGLOT", "/opt/sqlglot"))
    maps = Path(os.environ.get("BR_MAPS", Path(__file__).resolve().parent.parent / "maps"))

    tests = json.loads((data / "tests.json").read_text())["tests"]
    ms_of = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
    selector = Selector(tests, sqlglot, maps)

    lines = []
    for patch_file in sorted((data / "commits").glob("*.patch")):
        commit = patch_file.stem
        try:
            numbers, reason = selector.select(patch_file.read_text(errors="replace"))
        except Exception as exc:  # never crash: any surprise means run everything
            numbers, reason = selector.all_numbers(), f"ALL (error: {type(exc).__name__}: {exc})"
        workers = pack.pack(numbers, ms_of)
        cost = pack.cost_ms(workers, ms_of) / 1000
        print(f"[{commit}] {len(numbers)} tests, {len(workers)} workers, {cost:.1f}s :: {reason}",
              file=sys.stderr)
        lines.append(json.dumps({"commit": commit, "workers": workers}))

    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    print(f"cpu {time.process_time() - started:.2f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
