"""Coverage selector v2: statement expansion, pure-comment skip, import-time fallback.
usage: eval_v2.py covmap.json.gz sample_dir sqlglot_checkout"""
import ast, gzip, io, json, re, sys, tokenize, os, functools, collections
sys.path.insert(0, os.path.dirname(__file__))
from eval_cov import parse_patch, FIX, cost  # noqa (eval_cov prints on import; silence)
covmap = json.load(gzip.open(sys.argv[1], "rt")); S = sys.argv[2]; ROOT = sys.argv[3]
tests = json.load(open(f"{S}/tests.json"))["tests"]; ans = json.load(open(f"{S}/answers.json"))["commits"]
num = {t["id"]: t["number"] for t in tests}; secs = {t["number"]: t["seconds"] for t in tests}
# dynamic fixture map from audit-hook logs; setUpClass opens (TestExecutor) widen to the whole class
import glob
OPENS = {}
for fn in glob.glob(os.path.join(sys.argv[4], "opens-*.json")): OPENS.update(json.load(open(fn)))
FIX = collections.defaultdict(set)
for tid, fs in OPENS.items():
    cls = tid.rsplit("::", 1)[0]
    widen = "TestExecutor" in cls
    for fx in fs:
        if widen: FIX["tests/" + fx] |= {t["id"] for t in tests if t["id"].startswith(cls + "::")}
        else: FIX["tests/" + fx].add(tid)
c2n = {i: num.get(t) for i, t in enumerate(covmap["tests"])}
FILES = {f: {int(k): {c2n[c] for c in v if c >= 0 and c2n.get(c) is not None} for k, v in m.items()} for f, m in covmap["files"].items()}

@functools.lru_cache(None)
def src(f): return open(os.path.join(ROOT, f), encoding="utf-8").read()

@functools.lru_cache(None)
def tree(f): return ast.parse(src(f))

def _has_lambda(node, line):
    return any(isinstance(n, ast.Lambda) and n.lineno <= line <= n.end_lineno for n in ast.walk(node)) and not getattr(node, 'body', None)

def stmt_info(f, line):
    """(line_range, in_function, enclosing_class_names) for innermost statement containing line"""
    best, func, classes = None, False, []
    def walk(node, infunc, cls):
        nonlocal best, func, classes
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.stmt) and ch.lineno <= line <= ch.end_lineno:
                best, func, classes = ch, infunc or _has_lambda(ch, line), cls
                walk(ch, infunc or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) or _has_lambda(ch, line),
                     cls + [ch.name] if isinstance(ch, ast.ClassDef) else cls)
            elif not isinstance(ch, ast.stmt):
                walk(ch, infunc, cls)
    walk(tree(f), False, [])
    if best is None: return [], False, []
    body = getattr(best, "body", None)
    first = min([best.lineno] + [d.lineno for d in getattr(best, "decorator_list", [])])
    if body and line < body[0].lineno: rng = range(first, body[0].lineno)
    elif body: rng = range(line, line + 1)
    else: rng = range(first, best.end_lineno + 1)
    return list(rng), func, classes

@functools.lru_cache(None)
def string_spans(f):
    spans = []
    for tok in tokenize.generate_tokens(io.StringIO(src(f)).readline):
        if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]: spans.append((tok.start[0], tok.end[0]))
        if getattr(tokenize, "FSTRING_START", -1) == tok.type: pass
    return spans

def pure_comment_insert(f, patch_added_lines, ins_points):
    if not all(l.strip().startswith("#") or not l.strip() for l in patch_added_lines): return False
    for p in ins_points:  # inserted before old line p: inside a multi-line string if start < p <= end
        if any(s < p <= e for s, e in string_spans(f)): return False
        if p <= 2 and any("coding" in l for l in patch_added_lines): return False
    return True

FAMILY_DIRS = ("dialects", "parsers", "generators", "typing")
def family_tests(f):
    name = os.path.basename(f)[:-3]
    out = set()
    for d in FAMILY_DIRS:
        for ln, ts in FILES.get(f"sqlglot/{d}/{name}.py", {}).items(): out |= ts
    return out

@functools.lru_cache(None)
def name_usage_tests(name):
    out = set()
    pat = re.compile(r"\b%s\b" % re.escape(name))
    for f, m in FILES.items():
        if not f.startswith("sqlglot/"): continue
        for i, l in enumerate(src(f).splitlines(), 1):
            if pat.search(l) and m.get(i): out |= m[i]
    return out

def added_lines_by_file(patch):
    out, cur = collections.defaultdict(list), None
    for l in patch.splitlines():
        if l.startswith("+++ "): cur = l[6:]
        elif cur and l.startswith("+"): out[cur].append(l[1:])
    return out

def select(patch, mode):
    sel, notes = set(), []
    added = added_lines_by_file(patch)
    for f, (rem, ins) in parse_patch(patch).items():
        if f in FIX: sel |= {num[t] for t in FIX[f] if t in num}; continue
        if f not in FILES or not f.endswith(".py"): notes.append("unmapped " + f); continue
        if not rem and pure_comment_insert(f, added[f], ins): notes.append("pure-comment"); continue
        lines = set(rem) if rem else ({p for p in ins} | {p - 1 for p in ins})
        for ln in lines:
            rng, infunc, classes = stmt_info(f, ln)
            for l in rng: sel |= FILES[f].get(l, set())
            if not infunc and rng and mode >= 2:
                d = f.split("/")[1] if f.count("/") >= 2 else ""
                if d in FAMILY_DIRS:
                    fam = family_tests(f)
                    if mode == 3:
                        # attribute name assigned on this line, intersected with family
                        names = set(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)\s*[:=]", src(f).splitlines()[rng[0]-1]))
                        use = set().union(*[name_usage_tests(n) for n in names]) if names else set()
                        fam = (fam & use) or fam if use else fam
                    sel |= fam; notes.append(f"family-fallback {f}:{ln} +{len(fam)}")
                else:
                    use = set().union(*[name_usage_tests(c) for c in classes]) if classes else set()
                    sel |= use; notes.append(f"class-usage {classes}:{ln} +{len(use)}")
    return sel, notes

print("fixture files mapped:", len(FIX), "; tpc-h.sql ->", len(FIX["tests/fixtures/optimizer/tpc-h/tpc-h.sql"]), "tests", round(sum(secs[num[t]] for t in FIX["tests/fixtures/optimizer/tpc-h/tpc-h.sql"] if t in num),1), "s")
for mode in (1, 2, 3):
    tot = miss = 0; ts = 0; n = 0
    print(f"\n=== mode {mode}")
    for cid in sorted(ans):
        fail = set(ans[cid]["failing"])
        sel, notes = select(open(f"{S}/commits/{cid}.patch").read(), mode)
        missed = fail - sel; c = cost(sel) + (300 if missed else 0)
        tot += c; miss += bool(missed); ts += sum(secs[x] for x in sel); n += len(sel)
        slow = sorted(((secs[x], x) for x in sel), reverse=True)[:3]
        print(f"{cid} {ans[cid]['kind']:7s} fail={len(fail):2d} sel={len(sel):4d} secs={sum(secs[x] for x in sel):7.2f} cost={c:6.1f} missed={sorted(missed)} {'; '.join(notes)[:90]} slow={[(round(a,1),b) for a,b in slow if a>1]}")
    print(f"TOTAL mode{mode} cost={tot:.1f} misses={miss} selected={n} secs={ts:.1f}")
