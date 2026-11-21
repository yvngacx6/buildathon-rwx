#!/usr/bin/env python3
"""Build maps/import_time.json.gz: which lines run while a module is being IMPORTED.

usage: python3 mapbuild/import_time.py <sqlglot checkout> <out dir> [jobs]

Coverage maps keyed by test cannot see import-time work: `import sqlglot` runs before any test
(the '' context, dropped), and a lazily imported dialect is credited only to whichever test
imported it first. Functions called while modules are imported (trie builders, decorator
factories, `X = build()` tables, metaclass helpers, module `__getattr__`) therefore look
"used by a few tests" when every importer depends on them.

For every module under sqlglot/ and tests/ ("roots"), a fresh interpreter imports just that
module with sys.monitoring LINE events on, and records each repo line executed. Root 0 is
`sqlglot` itself: a line it runs runs for every test. Only the pinned, unmodified checkout is
traced.

Format (same container as maps/SCHEMA.md, keys are lines, values index "tests" = roots):
  {"tests": ["sqlglot", "sqlglot.dialects.duckdb", "tests.test_parser", ...],
   "map": {"sqlglot/trie.py:31": [0], "sqlglot/optimizer/simplify.py:1982": [5, 9, ...]}}
A line run by root 0 is stored as [0] only.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORKER = r"""
import json, os, sys
root = os.path.realpath(sys.argv[1]) + os.sep
mod = sys.argv[2]
sys.path.insert(0, root)
hits = set()
mon = sys.monitoring
TOOL = mon.PROFILER_ID
mon.use_tool_id(TOOL, "import_time")
def on_line(code, line):
    f = code.co_filename
    if f.startswith(root):
        hits.add((f[len(root):], line))
    return mon.DISABLE
mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.set_events(TOOL, mon.events.LINE)
ok = True
try:
    __import__(mod)
except BaseException as e:
    ok = False
mon.set_events(TOOL, 0)
json.dump({"ok": ok, "hits": sorted(f"{p}:{l}" for p, l in hits)}, sys.stdout)
"""


def module_name(rel: str) -> str:
    return rel[:-3].replace("/", ".").removesuffix(".__init__")


def main() -> None:
    sg = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2])
    jobs = int(sys.argv[3]) if len(sys.argv) > 3 else os.cpu_count() or 2
    rels = sorted(p.relative_to(sg).as_posix() for d in ("sqlglot", "tests")
                  for p in (sg / d).rglob("*.py") if "__pycache__" not in p.parts)
    rels = [r for r in rels if not r.startswith("tests/fixtures/")]
    roots = ["sqlglot"] + [m for m in map(module_name, rels) if m != "sqlglot"]
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(sg))

    def run(mod: str) -> tuple[str, dict]:
        p = subprocess.run([sys.executable, "-c", WORKER, str(sg), mod], cwd=sg, env=env,
                           capture_output=True, text=True, timeout=300)
        try:
            return mod, json.loads(p.stdout)
        except json.JSONDecodeError:
            return mod, {"ok": False, "hits": []}

    with ThreadPoolExecutor(jobs) as pool:
        results = dict(pool.map(run, roots))
    base = set(results["sqlglot"]["hits"])
    table: dict[str, list[int]] = {key: [0] for key in base}
    failed = []
    for i, mod in enumerate(roots[1:], start=1):
        if not results[mod]["ok"]:
            failed.append(mod)
        for key in results[mod]["hits"]:
            if key not in base:
                table.setdefault(key, []).append(i)
    out.mkdir(parents=True, exist_ok=True)
    data = {"tests": roots, "map": dict(sorted(table.items()))}  # sorted: byte-deterministic
    (out / "import_time.json.gz").write_bytes(
        gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(roots)} roots, {len(base)} lines at `import sqlglot`, {len(table)} lines total; "
          f"roots that failed to import: {failed}", file=sys.stderr)


if __name__ == "__main__":
    main()
