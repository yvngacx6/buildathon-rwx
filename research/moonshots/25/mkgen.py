"""Targeted one-token mutants on the lines the TPC executor runs only inside its generate calls
(the store's post_in coverage): mkgen.py SQLGLOT STORE OUTDIR [N] [SEED].
Every eval/mutants.py token site on those lines, shuffled, first N written as OUTDIR/commits/*.patch."""
import difflib, os, pickle, random, sys, zipfile, ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "eval"))
import mutants  # noqa: E402

S, store, out = sys.argv[1], sys.argv[2], sys.argv[3]
N = int(sys.argv[4]) if len(sys.argv) > 4 else 80
seed = int(sys.argv[5]) if len(sys.argv) > 5 else 25
meta = pickle.loads(zipfile.ZipFile(store).read("meta.pkl"))
lines: dict[str, set] = {}
for qs in meta["suites"].values():
    for q in qs:
        w = (q.get("phases") or {}).get("win") or {}
        if not q["outs"].get("gen"):
            continue
        for f, ls in (w.get("post_in") or {}).items():
            lines.setdefault(f, set()).update(ls)
g = mutants.Gen(Path(S), seed)
sites = []
for f, ls in sorted(lines.items()):
    text = open(os.path.join(S, f)).read()
    for s in g.token_sites(text):
        if s[0] in ls:
            sites.append((f, s))
random.Random(seed).shuffle(sites)
os.makedirs(f"{out}/commits", exist_ok=True)
n = 0
for f, (ln, c, ec, old, new) in sites:
    if n >= N:
        break
    text = open(os.path.join(S, f)).read()
    L = text.splitlines(True)
    L[ln - 1] = L[ln - 1][:c] + new + L[ln - 1][ec:]
    nt = "".join(L)
    if ast.dump(ast.parse(nt)) == ast.dump(ast.parse(text)):
        continue
    d = difflib.unified_diff(text.splitlines(True), nt.splitlines(True), f"a/{f}", f"b/{f}", n=3)
    cid = f"g{n:03d}"
    open(f"{out}/commits/{cid}.patch", "w").write(f"diff --git a/{f} b/{f}\n" + "".join(d))
    print(cid, f, ln, repr(old), "->", repr(new))
    n += 1
print(len(sites), "sites;", n, "written", file=sys.stderr)
