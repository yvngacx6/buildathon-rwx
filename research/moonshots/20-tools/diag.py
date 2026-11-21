"""Per-site breakdown of _state_readers for given patches. usage: diag.py set_dir commit..."""
import json, os, sys
from pathlib import Path
sys.path.insert(0, "/w"); sys.path.insert(0, "/w/eval")
import gap
from selector.select import Selector
data = gap.set_dir(sys.argv[1])
tests = json.loads((data / "tests.json").read_text())["tests"]
ans = json.loads((data / "answers.json").read_text())["commits"] if (data / "answers.json").exists() else {}
sel = Selector(tests, Path(os.environ["BR_SQLGLOT"]), gap.REPO / "maps")
orig = sel._site_reads
log = []
def wrapped(index, site, attr, depth=0, same=frozenset()):
    out = orig(index, site, attr, depth, same)
    log.append((depth, attr, site.what, site.flow and site.flow[0], site.use, site.recv, sorted(same), [(b, len(g)) for b, g in out]))
    return out
sel._site_reads = wrapped
for c in sys.argv[2:]:
    log.clear()
    ns, why = sel.select((data / "commits" / f"{c}.patch").read_text())
    print("=====", c, len(ns), "failing", len(ans.get(c, {}).get("failing", [])), why[:300])
    for row in log:
        if any(n for _, n in row[-1]):
            print("  ", row)
