"""Prototype conservative selector used only to measure recall of pinned-commit maps.
select(patch_text) -> (set of pytest node ids, reasons). Maps: linemap (coverage), usemap (dialect/parser/generator
class instantiation per test), readmap (files opened per test). All built from the pinned commit only."""
import ast, json, re, os, collections
R = os.path.expanduser("~/rwx-research-robust/"); SG = R + "sqlglot/"
LM = json.load(open(R + "linemap.json")); USE = json.load(open(R + "usemap.json")); RD = json.load(open(R + "readmap.json"))
ALL = None
def ctx2id(c):
    parts = c.split("."); # tests.dialects.test_x.Cls.fn
    for i in range(len(parts), 0, -1):
        p = "/".join(parts[:i]) + ".py"
        if os.path.exists(SG + p): return p + "::" + "::".join(parts[i:])
USE_INV = collections.defaultdict(set); RD_INV = collections.defaultdict(set)
for t, ms in USE.items():
    for m in ms:
        mod, _, cls = m.partition(":")
        USE_INV[mod.replace(".", "/") + ".py" + (":" + cls if cls else "")].add(t)
for t, fs in RD.items():
    for f in fs: RD_INV[f].add(t)
_cache = {}
def ids(ctxs):
    out = set()
    for c in ctxs:
        if c and c not in _cache: _cache[c] = ctx2id(c)
        if c: out.add(_cache[c])
    return out
def parse_patch(text):
    files = {}; cur = None
    for line in text.splitlines(keepends=True):
        if line.startswith("--- "): old = line[4:].strip(); continue
        if line.startswith("+++ "):
            new = line[4:].strip(); cur = files.setdefault((old, new), {"old": [], "new_lines": [], "hunks": []}); continue
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)", line)
        if m: o = int(m.group(1)); cur["hunks"].append(o); prev = None; continue
        if cur is None: continue
        if line.startswith("-"): cur["old"].append(o); o += 1; prev = "-"
        elif line.startswith("+"):
            if prev != "-": cur["old"] += [o - 1, o]
            prev = "+"
        else: o += 1; prev = " "
    return files
def apply_and_parse(path, text):
    import subprocess, tempfile
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["cp", "--parents", path, d], cwd=SG, check=True)
        subprocess.run(["git", "apply", "--include", path, "-"], cwd=d, input=text.encode(), check=True, capture_output=True)
        return open(os.path.join(d, path)).read()
def class_at(src, ln):
    best = None
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.ClassDef) and n.lineno <= ln <= n.end_lineno and (best is None or n.lineno > best.lineno): best = n
    return best.name if best else None
def func_spans(src):
    spans = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            spans.append((n.body[0].lineno if hasattr(n, "body") and isinstance(n.body, list) else n.lineno, n.end_lineno, n.lineno))
    return spans
def select(text, all_ids):
    sel, why = set(), []
    for (old, new), info in parse_patch(text).items():
        path = (new if new != "/dev/null" else old)[2:]
        if not path.endswith(".py"):
            if path.startswith("tests/"):
                users = RD_INV.get(path, set())
                # class-level setUp reads are attributed only to the first test of the class -> widen to the class
                cls = {u.rsplit("::", 1)[0] for u in users}
                if os.environ.get("WIDEN_FIXTURE_CLASS", "1") == "1":
                    users |= {t for t in all_ids if t.rsplit("::", 1)[0] in cls}
                if users: sel |= users; why.append(f"fixture:{path}:{len(users)}"); continue
            sel |= all_ids; why.append(f"unknown-nonpy:{path}->ALL"); continue
        try:
            before = open(SG + path).read() if old != "/dev/null" else ""
            after = apply_and_parse(path, text) if new != "/dev/null" else ""
            ta = ast.dump(ast.parse(after)); tb = ast.dump(ast.parse(before))
        except SyntaxError:
            sel |= all_ids; why.append(f"syntaxerror:{path}->ALL"); continue
        except Exception as e:
            sel |= all_ids; why.append(f"applyfail:{path}->ALL"); continue
        if ta == tb: why.append(f"ast-identical:{path}"); continue
        if path.startswith("tests/"):
            # test-side change: whole test file, plus every test file importing this module (helpers/test_dialect Validator)
            mod = path[:-3].replace("/", ".")
            imp = {t for t in all_ids if t.split("::")[0] == path}
            for t in all_ids:
                f = t.split("::")[0]
                if f != path and re.search(rf"(from|import)\s+{re.escape(mod)}\b|from\s+tests\.{re.escape(mod.split('.')[-1])}|from\s+\.\w*\s+import|from\s+{re.escape(mod.rsplit('.',1)[0])}\s+import\s+{re.escape(mod.split('.')[-1])}", open(SG + f).read()): imp.add(t)
            sel |= imp; why.append(f"testfile:{path}:{len(imp)}"); continue
        if not path.startswith("sqlglot/") or path.endswith("__init__.py") and path.count("/") == 1:
            sel |= all_ids; why.append(f"core/other:{path}->ALL"); continue
        lm = LM.get(path, {}); spans = func_spans(before)
        file_tests = ids(c for v in lm.values() for c in v)
        for ln in info["old"]:
            inner = [s for s in spans if s[0] <= ln <= s[1]]
            if inner:
                a, b, _ = min(inner, key=lambda s: s[1] - s[0])
                t = ids(c for l in range(a, b + 1) for c in lm.get(str(l), []))
                sel |= t; why.append(f"func:{path}:{ln}:{len(t)}")
            else:  # definition-time line (module/class body): users of module + all runtime coverage of file
                cls = class_at(before, ln)
                t = file_tests | (USE_INV.get(f"{path}:{cls}", set()) if path.startswith("sqlglot/expressions/") and cls else USE_INV.get(path, set()))
                sel |= t; why.append(f"deftime:{path}:{ln}:{len(t)}")
    return sel & all_ids, why
