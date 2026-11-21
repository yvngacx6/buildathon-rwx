"""Score oracle and simple strategies on the 30 sample commits."""
import re
from collections import defaultdict
from pathlib import Path
from lib import TESTS, ANS, MS, ROOT, lpt, best_pack, score_output, exact_makespan

ALL = [t["number"] for t in TESTS]
BYFILE = defaultdict(list)
for t in TESTS:
    BYFILE[t["file"]].append(t["number"])


def files_touched(cid):
    txt = (ROOT / "sample/commits" / f"{cid}.patch").read_text()
    return re.findall(r"^\+\+\+ b/(\S+)", txt, re.M)


def report(name, sel):
    s, missed = score_output(sel)
    print(f"{name:55s} {s:9.1f} s  misses={len(missed):2d}  per-commit={s/len(ANS):6.2f}")
    return s


print("== all tests, k workers (LPT vs exact) ==")
for k in range(1, 9):
    ws, m = lpt(ALL, k)
    wx, mx, pr = exact_makespan(ALL, k)
    print(f"k={k}: LPT makespan {m/1000:7.3f} cost {m/1000+2*k:7.3f} | exact {mx/1000:7.3f} cost {mx/1000+2*k:7.3f} proven={pr}")
report("starter: all tests, 3 workers LPT", {c: lpt(ALL, 3)[0] for c in ANS})
ws, c = best_pack(ALL)
report(f"all tests, optimal k={len(ws)}", {c_: ws for c_ in ANS})

print("\n== oracle (exact failing set, optimal packing) ==")
orc = {}
for c, v in ANS.items():
    ws, cost = best_pack(v["failing"])
    orc[c] = ws
    print(f"  {c} {v['kind']:8s} fail={len(v['failing']):2d} k={len(ws)} cost={cost/1000:7.3f}")
O = report("ORACLE test-level", orc)

# file-level oracle: run every test in each file that contains a failing test
fo = {}
for c, v in ANS.items():
    files = {TESTS[n]["file"] for n in v["failing"]}
    fo[c] = best_pack([n for f in files for n in BYFILE[f]])[0]
report("ORACLE file-level (whole files containing a failure)", fo)


def name_match_fixture(path):
    stem = Path(path).stem  # e.g. pushdown_predicates
    hits = [t["number"] for t in TESTS if t["file"] == "tests/test_optimizer.py"
            and (stem in t["id"] or t["id"].split("::")[-1].removeprefix("test_") in stem)]
    return hits


def heuristic(c, wide_core=True, opt_file=False, always=()):
    sel = set(always)
    for f in files_touched(c):
        m = re.match(r"sqlglot/(dialects|parsers|generators|tokenizers|typing)/(\w+)\.py", f)
        if f.startswith("tests/fixtures/optimizer/"):
            sel |= set(BYFILE["tests/test_optimizer.py"]) if opt_file else set(name_match_fixture(f))
        elif m and m.group(2) not in ("dialect", "__init__"):
            sel |= set(BYFILE.get(f"tests/dialects/test_{m.group(2)}.py", []))
            if opt_file:
                sel |= set(BYFILE["tests/test_optimizer.py"])
        elif f.startswith("sqlglot/optimizer/"):
            mod = Path(f).stem
            sel |= set(BYFILE["tests/test_optimizer.py"]) | set(BYFILE["tests/test_executor.py"])
        elif wide_core:
            sel |= set(ALL)
    return sel


for name, kw in [("H1 dialect->test_<d>.py, fixture->name match, core->ALL", {}),
                 ("H2 = H1 but + whole test_optimizer.py on dialect/fixture", {"opt_file": True}),
                 ("H3 = H1 but core -> nothing", {"wide_core": False})]:
    report(name, {c: best_pack(heuristic(c, **kw), exact=False)[0] for c in ANS})

print("\n== 'run nothing' & safety-net baselines ==")
report("run nothing at all", {c: [] for c in ANS})
cheap = [n for n in ALL if MS[n] <= 50]
report("every test <=50 ms (1 worker each commit)", {c: best_pack(cheap, exact=False)[0] for c in ANS})
print(f"  (cheap set: {len(cheap)} tests, {sum(MS[n] for n in cheap)/1000:.1f} s)")
