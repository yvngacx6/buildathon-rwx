"""String-literal mutants (eval/mutants.py string_mutant) on the lines the TPC executor runs only
inside its generate calls: mkstr.py SQLGLOT STORE OUTDIR [N] [SEED]."""
import difflib, os, pickle, sys, zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "eval"))
import mutants  # noqa: E402

S, store, out = sys.argv[1], sys.argv[2], sys.argv[3]
N = int(sys.argv[4]) if len(sys.argv) > 4 else 60
seed = int(sys.argv[5]) if len(sys.argv) > 5 else 2525
meta = pickle.loads(zipfile.ZipFile(store).read("meta.pkl"))
lines: dict[str, set] = {}
for qs in meta["suites"].values():
    for q in qs:
        if not q["outs"].get("gen"):
            continue
        for f, ls in (((q.get("phases") or {}).get("win") or {}).get("post_in") or {}).items():
            lines.setdefault(f, set()).update(ls)
g = mutants.Gen(Path(S), seed)
os.makedirs(f"{out}/commits", exist_ok=True)
seen, n = set(), 0
files = sorted(lines)
for _ in range(4000):
    if n >= N:
        break
    f = g.rng.choice(files)
    ls = lines[f]
    res = g.string_mutant(f, line_filter=lambda ln, ls=ls: ln in ls)
    if not res:
        continue
    (rel, nt), desc = next(iter(res[0].items())), res[1]
    if nt in seen:
        continue
    seen.add(nt)
    text = open(os.path.join(S, rel)).read()
    d = difflib.unified_diff(text.splitlines(True), nt.splitlines(True), f"a/{rel}", f"b/{rel}", n=3)
    cid = f"s{n:03d}"
    open(f"{out}/commits/{cid}.patch", "w").write(f"diff --git a/{rel} b/{rel}\n" + "".join(d))
    print(cid, desc)
    n += 1
print(n, "written", file=sys.stderr)
