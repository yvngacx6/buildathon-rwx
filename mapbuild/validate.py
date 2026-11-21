"""Check that the maps can explain every failing test of the sample commits.

    python mapbuild/validate.py --maps maps --sample sample --sqlglot /opt/sqlglot

For each sample commit and each failing test, look for a link from the test to the
changed region of the OLD file, strongest first:
  line      the test executed a changed (or, for pure insertions, adjacent) line
  function  the test executed some line of the innermost def enclosing the change
  class     the change is in a class (body) and the test used that class (classes map)
  read      the changed file is a fixture/data file the test opened (reads map)
  import    the changed file is a tests/ module the test imports (test_imports map)
  file      the test executed some line of the changed file (weak)
A failing test with none of these is UNEXPLAINED. The script uses the maps only.
"""

import argparse
import ast
import gzip
import json
import os
import re
from collections import Counter

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def load(path):
    with gzip.open(path, "rt") as fh:
        d = json.load(fh)
    return {k: set(v) for k, v in d["map"].items()}, d["tests"]


def changed_old_lines(patch_text):
    """{old path: set of old line numbers touched}. Pure insertions mark both neighbours."""
    out, path, old = {}, None, None
    for ln in patch_text.splitlines():
        if ln.startswith("--- "):
            p = ln[4:].split("\t")[0]
            path = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
            continue
        if ln.startswith("+++ "):
            if path is None:  # new file
                p = ln[4:].split("\t")[0]
                path = p[2:] if p.startswith("b/") else p
            out.setdefault(path, set())
            continue
        m = HUNK.match(ln)
        if m:
            old = int(m.group(1))
            continue
        if old is None or path is None or ln.startswith("\\"):
            continue
        if ln.startswith("-"):
            out[path].add(old)
            old += 1
        elif ln.startswith("+"):
            out[path].update({max(old - 1, 1), old})
        else:
            old += 1
    return out


def enclosing(tree, lines):
    """Innermost function spans and class qualnames that contain any changed line."""
    funcs, classes = [], set()
    defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def walk(node, qual):
        for ch in ast.iter_child_nodes(node):
            if not isinstance(ch, defs):
                walk(ch, qual)
                continue
            start = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
            if not any(start <= l <= ch.end_lineno for l in lines):
                continue
            if isinstance(ch, ast.ClassDef):
                classes.add(".".join(qual + [ch.name]))
            else:
                funcs.append((start, ch.end_lineno))
            walk(ch, qual + [ch.name])

    walk(tree, [])
    # innermost = shortest span per changed line
    inner = set()
    for l in lines:
        spans = [s for s in funcs if s[0] <= l <= s[1]]
        if spans:
            inner.add(min(spans, key=lambda s: s[1] - s[0]))
    return inner, classes


def explain(n, path, lines, maps, src_root):
    lmap, cmap, rmap, imap = maps
    if any(n in lmap.get(f"{path}:{l}", ()) for l in lines):
        return "line"
    if path.endswith(".py") and os.path.exists(os.path.join(src_root, path)):
        tree = ast.parse(open(os.path.join(src_root, path)).read())
        funcs, classes = enclosing(tree, lines)
        for a, b in funcs:
            if any(n in lmap.get(f"{path}:{l}", ()) for l in range(a, b + 1)):
                return "function"
        mod = path[:-3].replace("/", ".").removesuffix(".__init__")
        if any(n in cmap.get(f"{mod}.{q}", ()) for q in classes):
            return "class"
    if n in rmap.get(path, ()):
        return "read"
    if n in imap.get(path, ()):
        return "import"
    prefix = path + ":"
    if any(k.startswith(prefix) and n in v for k, v in lmap.items()):
        return "file"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--maps", default="maps")
    ap.add_argument("--sample", default="sample")
    ap.add_argument("--sqlglot", required=True, help="sqlglot checkout at the pinned commit")
    args = ap.parse_args()

    names = ("lines", "classes", "reads", "test_imports")
    loaded = [load(os.path.join(args.maps, f"{n}.json.gz")) for n in names]
    maps = tuple(m for m, _ in loaded)
    ids = loaded[0][1]
    answers = json.load(open(os.path.join(args.sample, "answers.json")))["commits"]

    tally, unexplained = Counter(), []
    for cid, ans in sorted(answers.items()):
        if not ans["failing"]:
            continue
        changes = changed_old_lines(open(os.path.join(args.sample, "commits", f"{cid}.patch")).read())
        kinds = Counter()
        for n in ans["failing"]:
            how = None
            for path, lines in changes.items():
                how = explain(n, path, lines, maps, args.sqlglot)
                if how:
                    break
            kinds[how or "UNEXPLAINED"] += 1
            if not how:
                unexplained.append((cid, n, ids[n], sorted(changes)))
        tally.update(kinds)
        print(f"{cid} ({ans['kind']}, {len(ans['failing'])} failing, files {sorted(changes)}): {dict(kinds)}")
    print("\ntotal:", dict(tally))
    print(f"unexplained: {len(unexplained)}")
    for u in unexplained:
        print("  ", *u)
    raise SystemExit(1 if unexplained else 0)


if __name__ == "__main__":
    main()
