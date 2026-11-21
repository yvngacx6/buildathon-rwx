import re, ast, collections, os
DIALECT_DIRS = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/", "sqlglot/tokenizers/")

def parse_patch(text):
    files = {}; cur = None; oldno = None
    for ln in text.split("\n"):
        if ln.startswith("diff --git"): cur = None; oldno = None
        elif ln.startswith("--- ") and oldno is None: pass
        elif ln.startswith("+++ ") and oldno is None:
            p = ln[4:].strip(); p = p[2:] if p.startswith("b/") else p
            cur = files.setdefault(p, {"old": set(), "adj": set(), "hunks": []})
        elif ln.startswith("@@") and cur is not None:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
            oldno = int(m.group(1)); cur["hunks"].append([oldno, int(m.group(2) or 1), []])
        elif cur is not None and oldno is not None and ln[:1] in (" ", "-", "+"):
            cur["hunks"][-1][2].append(ln)
            if ln[0] == " ": oldno += 1
            elif ln[0] == "-": cur["old"].add(oldno); oldno += 1
            else: cur["adj"].update((oldno - 1, oldno))
    return files

def scopes(src):
    """line -> innermost (kind, start, end, qualname) where kind in func/class/module"""
    tree = ast.parse(src); out = []
    def visit(node, qual):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = qual + [ch.name]
                start = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                out.append(("class" if isinstance(ch, ast.ClassDef) else "func", start, ch.end_lineno, ".".join(q), ch.lineno))
                visit(ch, q)
            else:
                visit(ch, qual)
    visit(tree, [])
    return out

class Selector:
    def __init__(self, m, tests, root):
        self.lm = m["line_map"]; self.im = m["import_lines"]; self.om = m["open_map"]; self.cu = m["class_users"]
        self.tests = tests; self.root = root
        self.by_file = collections.defaultdict(set)
        for (f, l), s in self.lm.items(): self.by_file[f] |= s
        self.test_file = collections.defaultdict(set)
        for t in tests: self.test_file[t["file"]].add(t["number"])
        self.mod_users = collections.defaultdict(set)   # module -> tests instantiating a class from it
        for c, s in self.cu.items(): self.mod_users[c.split(":")[0]] |= s
    def lines_users(self, f, a, b):
        s = set()
        for l in range(a, b + 1): s |= self.lm.get((f, l), set())
        return s
    def dialect_users(self, f):
        stem = os.path.basename(f)[:-3]; s = set()
        for d in DIALECT_DIRS:
            s |= self.mod_users.get(d.replace("/", ".") + stem, set())
        return s
    def candidates(self, patch, skip=()):
        files = self.files = parse_patch(patch); cand = set(); notes = []
        for f, info in files.items():
            if f in skip: notes.append("ast-equal " + f); continue
            if f.endswith(".py") and f.startswith("sqlglot/"):
                mod = f[:-3].replace("/", ".")
                src = open(os.path.join(self.root, f)).read()
                sc = scopes(src)
                for l in sorted(info["old"] | info["adj"]):
                    inner = [x for x in sc if x[1] <= l <= x[2]]
                    inner = max(inner, key=lambda x: x[1]) if inner else None
                    if (f, l) in self.lm and l in info["old"]:
                        cand |= self.lm[(f, l)]
                    elif inner and inner[0] == "func":
                        # header/adjacent/uncovered line inside a function: tests running that function body
                        cand |= self.lines_users(f, inner[4], inner[2])
                    else:
                        # class body or module level: users of the class / module
                        if inner and inner[0] == "class":
                            q = inner[3]
                            u = set().union(*[s for c, s in self.cu.items() if c == f"{mod}:{q}"] or [set()])
                            u |= self.lines_users(f, inner[1], inner[2])
                            notes.append(f"classlevel {f}:{l} {q}")
                        else:
                            u = self.by_file[f] | self.mod_users.get(mod, set()); notes.append(f"modlevel {f}:{l}")
                        cand |= u | self.dialect_users(f) if f.startswith(DIALECT_DIRS) else u
            elif f.startswith("tests/") and f.endswith(".py"):
                cand |= self.test_file.get(f, set()); notes.append("testfile " + f)
            else:
                s = self.om.get(f)
                if s is None: notes.append("UNKNOWN " + f)
                else: cand |= s; notes.append("data " + f)
        return cand, notes
