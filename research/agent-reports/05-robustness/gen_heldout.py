"""Untuned held-out set (seed 8), biased to definition-time table edits. usage: gen_heldout.py <sqlglot> <outdir>"""
import difflib, glob, os, random, re, sys
SG, OUT = sys.argv[1], sys.argv[2]; rnd = random.Random(8); os.makedirs(OUT, exist_ok=True)
OPS = [(r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"(?<![\w.])(\d+)(?![\w.])", lambda m: str(int(m.group(1)) + 1)),
       (r" == ", " != "), (r"\band\b", "or")]
def mut(name, path, want):
    src = open(os.path.join(SG, path)).read().splitlines(True)
    c = [(i, o) for i, l in enumerate(src) for o in OPS if re.search(o[0], l) and want(l) and not l.lstrip().startswith("#")]
    if not c: return
    i, (p, r) = rnd.choice(c); old = "".join(src); src[i] = re.sub(p, r, src[i], count=1)
    open(f"{OUT}/{name}.patch", "w").write("".join(difflib.unified_diff(old.splitlines(True), src, "a/" + path, "b/" + path)))
rel = lambda pat: [os.path.relpath(f, SG) for f in glob.glob(os.path.join(SG, pat))]
for k in range(5): mut(f"h_expr{k}", rnd.choice(rel("sqlglot/expressions/*.py")), lambda l: "arg_types" in l or re.match(r"    \w+ = ", l))
for k in range(3): mut(f"h_typing{k}", rnd.choice(rel("sqlglot/typing/*.py")), lambda l: True)
for k in range(3): mut(f"h_ptab{k}", rnd.choice(rel("sqlglot/parsers/*.py")), lambda l: l.startswith("        ") and ":" in l)
for k in range(3): mut(f"h_gtab{k}", rnd.choice(rel("sqlglot/generators/*.py")), lambda l: l.startswith("    ") and not l.startswith("     ") and "=" in l)
