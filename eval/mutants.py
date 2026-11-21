#!/usr/bin/env python3
"""Generate the PRIVATE validation patch set (synthetic edits to pinned sqlglot).

Purpose: measure the selector's recall on patches it was never tuned on. The labels
(which tests each patch breaks) come from running the full suite, see eval/label/.
RULE: nothing produced here (patches, labels, statistics) may go into the selector image.
It is for measurement only. Regenerate with a new --seed before trusting a number twice.

usage:
  python3 eval/mutants.py --sqlglot <pinned sqlglot git checkout> --out <dir> [--seed 20261120]

Writes <out>/commits/<id>.patch (git-format unified diffs, a/ b/ prefixes, like sample/),
<out>/ORDER (shuffled labeling order) and <out>/meta.json ({id: {kind, desc, files}}).

Kinds
  Sample-style (the organizers' generator as far as the 30 samples show):
    code      one-token mutant in sqlglot/**.py: and<->or, ==<->!=, is->==, is not->is, <<->=,
              ><->=, int +-1, True<->False, drop unary `not`. Sites are a mix of function
              bodies, class-body / module-level definitions and continuation lines of
              multi-line conditions (lines starting with `and`/`or`).
    comment   one of the five template comments inserted before a code line.
    fixture   one-token corruption of a data line in tests/fixtures/** (text files).
    multi     2-3 of the above stacked, in different files.
  New kinds (not seen in the sample):
    docstring, whitespace, testedit (tests/test_*.py, tests/dialects/test_*.py), helper
    (tests/helpers.py, Validator in tests/dialects/test_dialect.py, tests/leakcheck.py),
    importerr, syntaxerr, newfile, deletefile, rename (functions, methods, test methods),
    config (pyproject.toml, setup.py, setup.cfg, Makefile, MANIFEST.in), fixturemeta
    (`#` metadata lines of optimizer fixtures), executor (mutants in sqlglot/executor),
    string (string-literal edits in sqlglot code), dictentry (one entry removed from a
    class-body or module-level dict/set literal).
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import random
import re
import shutil
import string
import subprocess
import tempfile
import tokenize
from pathlib import Path

PINNED = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
SAMPLE_IDS = {"2haz", "2jrq", "2qn5", "2vdj", "3utz", "4d63", "535z", "5y72", "66ks", "6vvp", "6ze8",
              "8y7j", "986c", "9u5k", "ary6", "bap8", "bj7g", "bvw2", "egys", "f4bn", "f4xa", "fryt",
              "gbav", "gbhk", "j6uz", "ktz3", "mthh", "nwnx", "rszg", "st8f"}

# How many patches of each kind (about 60% sample-style, 40% new kinds).
COUNTS = {
    "code": 90, "comment": 20, "fixture": 26, "multi": 16,
    "docstring": 10, "whitespace": 6, "testedit": 12, "helper": 10, "importerr": 6, "syntaxerr": 6,
    "newfile": 6, "deletefile": 7, "rename": 9, "config": 6, "fixturemeta": 9, "executor": 8,
    "string": 9, "dictentry": 9,
}
TEMPLATE_COMMENTS = ["Cheap check first", "Kept for backwards compatibility", "Keep in sync with the generator",
                     "Intentionally not cached", "Mirrors the behavior of the reference implementation"]
# Where code mutants go: (glob, weight). Spread over all of sqlglot.
CODE_AREAS = [
    ("sqlglot/dialects/*.py", 14), ("sqlglot/parsers/*.py", 10), ("sqlglot/generators/*.py", 10),
    ("sqlglot/typing/*.py", 5), ("sqlglot/optimizer/*.py", 14), ("sqlglot/executor/*.py", 3),
    ("sqlglot/expressions/*.py", 8), ("sqlglot/tokens.py", 4), ("sqlglot/parser.py", 6),
    ("sqlglot/generator.py", 6), ("sqlglot/transforms.py", 3), ("sqlglot/serde.py", 1),
    ("sqlglot/*.py", 8), ("sqlglot/**/*.py", 8),
]


class Gen:
    def __init__(self, src: Path, seed: int):
        self.rng = random.Random(seed)
        self.src = src
        self.used_ids = set(SAMPLE_IDS)
        self.cache: dict[str, str] = {}

    # ---------- helpers ----------
    def read(self, rel: str) -> str:
        if rel not in self.cache:
            self.cache[rel] = (self.src / rel).read_text()
        return self.cache[rel]

    def files(self, pattern: str) -> list[str]:
        return sorted(str(p.relative_to(self.src)) for p in self.src.glob(pattern) if p.is_file())

    def pick_area_file(self, areas=CODE_AREAS) -> str:
        globs, weights = zip(*areas)
        while True:
            fs = [f for f in self.files(self.rng.choices(globs, weights)[0]) if not f.endswith("__init__.py")
                  or self.rng.random() < 0.3]
            if fs:
                return self.rng.choice(fs)

    def new_id(self) -> str:
        while True:
            i = "".join(self.rng.choice(string.ascii_lowercase + string.digits) for _ in range(4))
            if i not in self.used_ids and not i.isdigit():
                self.used_ids.add(i)
                return i

    # ---------- python token mutants ----------
    @staticmethod
    def scopes(text: str) -> dict[int, str]:
        """line -> 'func' if inside a function body, else 'def' (module / class body)."""
        kind = {}
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                body = node.body if isinstance(node.body, list) else [node.body]
                for ln in range(body[0].lineno, (node.end_lineno or body[0].lineno) + 1):
                    kind[ln] = "func"
        return kind

    def token_sites(self, text: str) -> list[tuple[int, int, int, str, str]]:
        """All one-token mutation sites: (line, col, end_col, old, new). Never inside strings/comments."""
        toks = [t for t in tokenize.generate_tokens(io.StringIO(text).readline)
                if t.type not in (tokenize.NL, tokenize.COMMENT)]
        sites, src_lines = [], text.splitlines()
        for k, t in enumerate(toks):
            (ln, c), (eln, ec) = t.start, t.end
            if ln != eln:
                continue
            prev = toks[k - 1] if k else None
            nxt = toks[k + 1] if k + 1 < len(toks) else None
            s = t.string
            if t.type == tokenize.OP and s in ("==", "!=", "<", "<=", ">", ">="):
                new = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">"}[s]
                sites.append((ln, c, ec, s, new))
            elif t.type == tokenize.NAME and s in ("and", "or"):
                sites.append((ln, c, ec, s, "or" if s == "and" else "and"))
            elif t.type == tokenize.NAME and s in ("True", "False"):
                sites.append((ln, c, ec, s, "False" if s == "True" else "True"))
            elif t.type == tokenize.NAME and s == "is" and nxt is not None:
                if nxt.string == "not" and nxt.start[0] == ln:
                    sites.append((ln, c, nxt.end[1], "is not", "is"))
                else:
                    sites.append((ln, c, ec, "is", "=="))
            elif (t.type == tokenize.NAME and s == "not" and nxt is not None and nxt.string != "in"
                  and (prev is None or prev.string != "is") and nxt.start[0] == ln):
                sites.append((ln, c, nxt.start[1], src_lines[ln - 1][c:nxt.start[1]], ""))
            elif t.type == tokenize.NUMBER and re.fullmatch(r"\d+", s):
                d = 1 if (int(s) == 0 or (prev is not None and prev.string == "-")) else self.rng.choice([1, -1])
                sites.append((ln, c, ec, s, str(int(s) + d)))
        return sites

    def code_mutant(self, rel: str, where: str | None = None, ops: set | None = None):
        text = self.read(rel)
        lines = text.splitlines(True)
        scope = self.scopes(text)
        sites = self.token_sites(text)
        if ops:
            sites = [s for s in sites if s[3] in ops]
        if not sites:
            return None
        pools = {
            "func": [s for s in sites if scope.get(s[0]) == "func"],
            "def": [s for s in sites if scope.get(s[0]) != "func"],
            "cont": [s for s in sites if lines[s[0] - 1].lstrip().startswith(("and ", "or "))],
        }
        if where is None:
            where = self.rng.choices(["func", "def", "cont"], [65, 22, 13])[0]
        pool = pools[where] or sites
        ln, c, ec, old, new = self.rng.choice(pool)
        line = lines[ln - 1]
        lines[ln - 1] = line[:c] + new + line[ec:]
        newtext = "".join(lines)
        if ast.dump(ast.parse(newtext)) == ast.dump(ast.parse(text)):
            return None
        return {rel: newtext}, f"{rel}:{ln} {old!r}->{new!r} ({where})"

    def comment_insert(self, rel: str):
        text = self.read(rel)
        lines = text.splitlines(True)
        cands = [i for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith(("#", ")", "]", "}"))]
        for _ in range(50):
            i = self.rng.choice(cands)
            indent = re.match(r"\s*", lines[i]).group(0)
            new = lines[:i] + [f"{indent}# {self.rng.choice(TEMPLATE_COMMENTS)}\n"] + lines[i:]
            newtext = "".join(new)
            try:
                if ast.dump(ast.parse(newtext)) == ast.dump(ast.parse(text)):
                    return {rel: newtext}, f"{rel}:{i + 1} comment"
            except SyntaxError:
                pass
        return None

    # ---------- fixtures ----------
    def fixture_files(self) -> list[str]:
        out = []
        for f in self.files("tests/fixtures/**/*"):
            if f.endswith(".gz") or "LICENSE" in f:
                continue
            try:
                self.read(f)
                out.append(f)
            except UnicodeDecodeError:
                pass
        return out

    def fixture_corrupt(self, rel: str | None = None):
        if rel is None:
            fs = self.fixture_files()
            opt = [f for f in fs if f.startswith("tests/fixtures/optimizer/")]
            rel = self.rng.choice(opt if self.rng.random() < 0.7 else fs)
        text = self.read(rel)
        lines = text.splitlines(True)
        cands = [i for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith(("#", "--"))
                 and len(l.split()) >= 2]
        if not cands:
            return None
        i = self.rng.choice(cands)
        line = lines[i]
        words = [m for m in re.finditer(r"\S+", line)]
        op = self.rng.choice(["insert", "insert", "append42", "delete_word", "delete_char", "join"])
        if op == "insert":
            m = self.rng.choice(words)
            new = line[:m.start()] + self.rng.choice(["NULL", "42", "0", "x", "1", "y"]) + " " + line[m.start():]
        elif op == "append42":
            m = self.rng.choice(words)
            new = line[:m.end()] + "42" + line[m.end():]
        elif op == "delete_word":
            m = self.rng.choice(words[1:])
            new = line[:m.start()] + line[m.end():].lstrip(" ")
            new = new if new.endswith("\n") or not line.endswith("\n") else new + "\n"
        elif op == "delete_char":
            ps = [k for k, ch in enumerate(line) if ch in "<>(),.=*'\""]
            if not ps:
                return None
            k = self.rng.choice(ps)
            new = line[:k] + line[k + 1:]
        else:  # join: x.b -> xa .b style token split/merge
            m = self.rng.choice(words)
            k = self.rng.randrange(m.start(), m.end())
            new = line[:k + 1] + "a " + line[k + 1:]
        if new == line:
            return None
        lines[i] = new
        return {rel: "".join(lines)}, f"{rel}:{i + 1} {op}"

    def fixture_meta(self):
        fs = [f for f in self.files("tests/fixtures/optimizer/*.sql") if re.search(r"^#", self.read(f), re.M)]
        rel = self.rng.choice(fs)
        lines = self.read(rel).splitlines(True)
        metas = [i for i, l in enumerate(lines) if l.startswith("#")]
        i = self.rng.choice(metas)
        l = lines[i]
        dialects = ["duckdb", "snowflake", "bigquery", "postgres", "spark", "tsql", "mysql", "clickhouse"]
        if "execute:" in l:
            lines[i] = l.replace("true", "false") if "true" in l else l.replace("false", "true")
            d = "flip execute"
        elif "dialect:" in l:
            cur = l.split(":", 1)[1].strip()
            lines[i] = f"# dialect: {self.rng.choice([x for x in dialects if x != cur])}\n"
            d = "change dialect"
        elif self.rng.random() < 0.5:
            del lines[i]
            d = "delete meta line"
        else:
            lines[i] = l.rstrip("\n") + " (edited)\n"
            d = "edit meta text"
        if self.rng.random() < 0.3:  # sometimes add a new metadata line instead
            lines = self.read(rel).splitlines(True)
            pairs = [k for k in range(1, len(lines)) if lines[k].strip() and not lines[k - 1].strip()
                     and not lines[k].startswith(("#", "--"))]
            if pairs:
                k = self.rng.choice(pairs)
                extra = self.rng.choice([f"# dialect: {self.rng.choice(dialects)}\n", "# execute: false\n",
                                         "# execute: true\n", "# title: extra\n"])
                lines.insert(k, extra)
                d = f"add {extra.strip()!r}"
                i = k
        return {rel: "".join(lines)}, f"{rel}:{i + 1} {d}"

    # ---------- new kinds ----------
    def docstring_edit(self):
        for _ in range(100):
            rel = self.pick_area_file()
            text = self.read(rel)
            docs = []
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Module, ast.AsyncFunctionDef)) and \
                        ast.get_docstring(node) and isinstance(node.body[0], ast.Expr):
                    docs.append(node.body[0].value)
            if not docs:
                continue
            d = self.rng.choice(docs)
            lines = text.splitlines(True)
            span = [k for k in range(d.lineno - 1, d.end_lineno) if re.search(r"[A-Za-z]{3,}", lines[k])]
            if not span:
                continue
            k = self.rng.choice(span)
            l = lines[k]
            ws = list(re.finditer(r"[A-Za-z]{3,}", l))
            m = self.rng.choice(ws)
            op = self.rng.choice(["typo", "insert", "upper"])
            rep = {"typo": m.group(0)[:-1], "insert": m.group(0) + " really", "upper": m.group(0).upper()}[op]
            lines[k] = l[:m.start()] + rep + l[m.end():]
            new = "".join(lines)
            try:
                ast.parse(new)
            except SyntaxError:
                continue
            if new != text:
                return {rel: new}, f"{rel}:{k + 1} docstring {op} {'doctest' if '>>>' in l else ''}"
        return None

    def whitespace_edit(self):
        rel = self.pick_area_file()
        text = self.read(rel)
        lines = text.splitlines(True)
        op = self.rng.choice(["blank", "blank", "trailing", "dedent_blank"])
        for _ in range(50):
            i = self.rng.randrange(1, len(lines))
            if op in ("blank", "dedent_blank"):
                if lines[i - 1].rstrip().endswith("\\"):
                    continue
                new = lines[:i] + ["\n" if op == "blank" else "\n\n"] + lines[i:]
            else:
                if not lines[i].strip():
                    continue
                new = lines[:i] + [lines[i].rstrip("\n") + "  \n"] + lines[i + 1:]
            newtext = "".join(new)
            try:
                ast.parse(newtext)
            except SyntaxError:
                continue
            return {rel: newtext}, f"{rel}:{i + 1} whitespace {op}"
        return None

    def test_files(self) -> list[str]:
        return [f for f in self.files("tests/test_*.py") + self.files("tests/dialects/test_*.py")]

    def string_mutant(self, rel: str, line_filter=None):
        text = self.read(rel)
        lines = text.splitlines(True)
        doc_lines = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Module, ast.AsyncFunctionDef)) and \
                    node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                doc_lines.update(range(node.body[0].lineno, node.body[0].end_lineno + 1))
        cands = []
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            if t.type == tokenize.STRING and t.start[0] == t.end[0] and t.start[0] not in doc_lines:
                m = re.fullmatch(r"([rRbBuU]?)(['\"])(.+)\2", t.string)
                if m and 1 <= len(m.group(3)) <= 120 and (line_filter is None or line_filter(t.start[0])):
                    cands.append(t)
        if not cands:
            return None
        t = self.rng.choice(cands)
        body = t.string[len(t.string.split(t.string[-1])[0]) + 1:-1]
        op = self.rng.choice(["drop_last", "case", "add_space", "swap_word"])
        if op == "drop_last" and len(body) > 1:
            nb = body[:-1] if body[-1] != "\\" else body[:-2]
        elif op == "case" and re.search(r"[A-Za-z]", body):
            k = self.rng.choice([i for i, ch in enumerate(body) if ch.isalpha()])
            nb = body[:k] + body[k].swapcase() + body[k + 1:]
        elif op == "add_space":
            k = self.rng.randrange(0, len(body) + 1)
            nb = body[:k] + " " + body[k:]
        else:
            ws = body.split(" ")
            nb = " ".join(ws[:-1]) if len(ws) > 1 else body + "_x"
        if nb.endswith("\\") and not nb.endswith("\\\\"):
            nb += "x"
        q = t.string[-1]
        prefix = t.string.split(q)[0]
        new_tok = f"{prefix}{q}{nb}{q}"
        ln, c = t.start
        line = lines[ln - 1]
        lines[ln - 1] = line[:c] + new_tok + line[t.end[1]:]
        newtext = "".join(lines)
        try:
            if ast.dump(ast.parse(newtext)) == ast.dump(ast.parse(text)):
                return None
        except SyntaxError:
            return None
        return {rel: newtext}, f"{rel}:{ln} string {op} {t.string[:40]!r}"

    def testedit(self):
        rel = self.rng.choice(self.test_files())
        text = self.read(rel)
        scope = self.scopes(text)
        r = self.rng.random()
        if r < 0.55:
            return self.string_mutant(rel, lambda ln: scope.get(ln) == "func")
        if r < 0.8:
            return self.code_mutant(rel, "func")
        # add a failing / early-return statement at the top of a random test method
        tree = ast.parse(text)
        meths = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
        if not meths:
            return None
        m = self.rng.choice(meths)
        lines = text.splitlines(True)
        first = m.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):  # skip docstring
            first = m.body[1] if len(m.body) > 1 else first
        indent = re.match(r"\s*", lines[first.lineno - 1]).group(0)
        stmt = self.rng.choice(["self.assertTrue(False)", "return", "self.assertEqual(1, 2)"])
        lines.insert(first.lineno - 1, f"{indent}{stmt}\n")
        return {rel: "".join(lines)}, f"{rel}:{first.lineno} insert {stmt!r} into {m.name}"

    def helper_edit(self):
        rel = self.rng.choice(["tests/helpers.py", "tests/helpers.py", "tests/dialects/test_dialect.py",
                               "tests/dialects/test_dialect.py", "tests/dialects/test_dialect.py",
                               "tests/leakcheck.py"])
        text = self.read(rel)
        filt = None
        if rel.endswith("test_dialect.py"):
            cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == "Validator")
            filt = range(cls.lineno, cls.end_lineno + 1)
        if self.rng.random() < 0.6:
            res = self.code_mutant_in(rel, filt)
        else:
            res = self.string_mutant(rel, (lambda ln: ln in filt) if filt else None)
        return res

    def code_mutant_in(self, rel, line_range):
        text = self.read(rel)
        sites = [s for s in self.token_sites(text) if line_range is None or s[0] in line_range]
        if not sites:
            return None
        ln, c, ec, old, new = self.rng.choice(sites)
        lines = text.splitlines(True)
        lines[ln - 1] = lines[ln - 1][:c] + new + lines[ln - 1][ec:]
        newtext = "".join(lines)
        if ast.dump(ast.parse(newtext)) == ast.dump(ast.parse(text)):
            return None
        return {rel: newtext}, f"{rel}:{ln} {old!r}->{new!r}"

    def importerr(self):
        rel = self.pick_area_file()
        text = self.read(rel)
        lines = text.splitlines(True)
        froms = [i for i, l in enumerate(lines) if re.match(r"from sqlglot[\w.]* import \w+", l)]
        if froms and self.rng.random() < 0.5:
            i = self.rng.choice(froms)
            lines[i] = re.sub(r"(import )(\w+)", r"\1\2_missing", lines[i], count=1)
            return {rel: "".join(lines)}, f"{rel}:{i + 1} import of missing name"
        tree = ast.parse(text)
        imps = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        at = imps[-1].end_lineno if imps else 0
        lines.insert(at, f"import sqlglot_nonexistent_{self.rng.randrange(100)}\n")
        return {rel: "".join(lines)}, f"{rel}:{at + 1} import of nonexistent module"

    def syntaxerr(self):
        rel = self.pick_area_file() if self.rng.random() < 0.85 else self.rng.choice(self.test_files())
        text = self.read(rel)
        lines = text.splitlines(True)
        op = self.rng.choice(["colon", "paren", "indent"])
        for _ in range(100):
            i = self.rng.randrange(len(lines))
            l = lines[i]
            if op == "colon" and re.match(r"\s*(def|if|for|while|class|elif|else|try|with)\b.*:\s*$", l):
                new = l.rstrip()[:-1] + "\n"
            elif op == "paren" and l.rstrip().endswith(")") and l.count("(") == l.count(")"):
                new = l.rstrip()[:-1] + "\n"
            elif op == "indent" and l.strip() and not l.lstrip().startswith("#"):
                new = "  " + l
            else:
                continue
            nl = lines[:i] + [new] + lines[i + 1:]
            try:
                ast.parse("".join(nl))
            except SyntaxError:
                return {rel: "".join(nl)}, f"{rel}:{i + 1} syntax error ({op})"
        return None

    def newfile(self):
        n = self.rng.randrange(1000)
        opts = [
            (f"sqlglot/util_extra_{n}.py", "def unused_helper(x):\n    return x + 1\n"),
            (f"sqlglot/dialects/newdialect{n}.py",
             "from sqlglot.dialects.dialect import Dialect\n\n\nclass Newdialect(Dialect):\n    pass\n"),
            (f"sqlglot/optimizer/extra_rule_{n}.py", "from sqlglot import exp\n\n\ndef extra_rule(expression):\n"
                                                     "    return expression\n"),
            (f"tests/fixtures/optimizer/extra_{n}.sql", "SELECT 1;\nSELECT 1 AS \"1\";\n"),
            (f"tests/helpers_extra_{n}.py", "def helper():\n    return None\n"),
            (f"docs/notes_{n}.md", "# Notes\n\nNothing here.\n"),
            (f"tests/test_extra_{n}.py", "import unittest\n\n\nclass TestExtra(unittest.TestCase):\n"
                                         "    def test_extra(self):\n        self.assertTrue(True)\n"),
            (f"sqlglot/typing/extra_{n}.py", "EXPRESSION_METADATA = {}\n"),
        ]
        rel, body = self.rng.choice(opts)
        return {rel: body}, f"new file {rel}"

    def deletefile(self):
        area = self.rng.choice(["sqlglot/dialects/*.py", "sqlglot/generators/*.py", "sqlglot/parsers/*.py",
                                "sqlglot/typing/*.py", "sqlglot/optimizer/*.py", "sqlglot/executor/*.py",
                                "tests/fixtures/optimizer/*.sql", "tests/dialects/test_*.py", "sqlglot/*.py",
                                "*.md"])
        fs = [f for f in self.files(area) if not f.endswith("__init__.py")]
        rel = self.rng.choice(fs)
        return {rel: None}, f"delete {rel}"

    def rename(self):
        r = self.rng.random()
        if r < 0.3:  # rename a test method (its id disappears)
            rel = self.rng.choice(self.test_files())
        else:
            rel = self.pick_area_file()
        text = self.read(rel)
        tree = ast.parse(text)
        defs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (r >= 0.3 or n.name.startswith("test"))]
        if not defs:
            return None
        d = self.rng.choice(defs)
        new_name = d.name + "_renamed" if r < 0.3 or self.rng.random() < 0.5 else d.name + "2"
        consistent = r >= 0.3 and self.rng.random() < 0.5
        if consistent:  # rename the def and every reference in this file only
            newtext = re.sub(rf"\b{re.escape(d.name)}\b", new_name, text)
            desc = f"{rel}: rename {d.name} -> {new_name} (whole file)"
        else:
            lines = text.splitlines(True)
            lines[d.lineno - 1] = re.sub(rf"\bdef {re.escape(d.name)}\b", f"def {new_name}", lines[d.lineno - 1], count=1)
            newtext = "".join(lines)
            desc = f"{rel}:{d.lineno} rename def {d.name} -> {new_name} (def only)"
        return {rel: newtext}, desc

    def config(self):
        rel = self.rng.choice(["pyproject.toml", "pyproject.toml", "setup.py", "setup.cfg", "Makefile", "MANIFEST.in"])
        text = self.read(rel)
        lines = text.splitlines(True)
        if rel == "pyproject.toml" and self.rng.random() < 0.4:
            add = self.rng.choice(['\n[tool.pytest.ini_options]\naddopts = "-p no:randomly"\n',
                                   '\n[tool.pytest.ini_options]\nfilterwarnings = ["error"]\n',
                                   '\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'])
            return {rel: text + add}, f"{rel}: add pytest section {add.strip()!r}"
        nonblank = [i for i, l in enumerate(lines) if l.strip()]
        i = self.rng.choice(nonblank)
        op = self.rng.choice(["delete", "number", "comment"])
        if op == "delete":
            new = lines[:i] + lines[i + 1:]
        elif op == "number" and re.search(r"\d", lines[i]):
            new = lines[:i] + [re.sub(r"\d+", lambda m: str(int(m.group(0)) + 1), lines[i], count=1)] + lines[i + 1:]
        else:
            new = lines[:i] + ["# local tweak\n"] + lines[i:]
            op = "comment"
        return {rel: "".join(new)}, f"{rel}:{i + 1} {op}"

    def dictentry(self):
        for _ in range(200):
            rel = self.pick_area_file([a for a in CODE_AREAS if "executor" not in a[0]])
            text = self.read(rel)
            tree = ast.parse(text)
            scope = self.scopes(text)
            lines = text.splitlines(True)
            entries = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.Dict, ast.Set)) and scope.get(node.lineno) != "func" \
                        and node.end_lineno - node.lineno >= 2:
                    keys = node.keys if isinstance(node, ast.Dict) else node.elts
                    vals = node.values if isinstance(node, ast.Dict) else node.elts
                    for k, v in zip(keys, vals):
                        if k is not None and k.lineno == v.end_lineno and lines[k.lineno - 1].rstrip().endswith(","):
                            entries.append(k.lineno)
            if not entries:
                continue
            ln = self.rng.choice(entries)
            newtext = "".join(lines[:ln - 1] + lines[ln:])
            try:
                if ast.dump(ast.parse(newtext)) != ast.dump(tree):
                    return {rel: newtext}, f"{rel}:{ln} remove entry {lines[ln - 1].strip()[:60]!r}"
            except SyntaxError:
                continue
        return None

    # ---------- dispatch ----------
    def make(self, kind: str):
        if kind == "code":
            return self.code_mutant(self.pick_area_file())
        if kind == "executor":
            return self.code_mutant(self.rng.choice(self.files("sqlglot/executor/*.py")))
        if kind == "comment":
            return self.comment_insert(self.pick_area_file())
        if kind == "fixture":
            return self.fixture_corrupt()
        if kind == "fixturemeta":
            return self.fixture_meta()
        if kind == "string":
            rel = self.pick_area_file()
            scope = self.scopes(self.read(rel))
            return self.string_mutant(rel, (lambda ln: scope.get(ln) == "func") if self.rng.random() < 0.6 else None)
        if kind == "multi":
            parts = self.rng.choice([["comment", "code"], ["code", "fixture"], ["comment", "fixture"],
                                     ["comment", "code", "fixture"], ["comment", "code", "fixture"]])
            files, descs = {}, []
            for p in parts:
                for _ in range(20):
                    r = self.make(p)
                    if r and not set(r[0]) & set(files):
                        files.update(r[0])
                        descs.append(r[1])
                        break
            return (files, " + ".join(descs)) if len(descs) >= 2 else None
        return getattr(self, {"docstring": "docstring_edit", "whitespace": "whitespace_edit",
                              "helper": "helper_edit"}.get(kind, kind))()


def git(repo: Path, *args, **kw) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, **kw).stdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlglot", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--seed", type=int, default=20261120)
    a = ap.parse_args()
    assert git(a.sqlglot, "rev-parse", "HEAD").strip() == PINNED, "checkout must be at the pinned commit"
    gen = Gen(a.sqlglot, a.seed)
    tmp = Path(tempfile.mkdtemp())
    work = tmp / "sg"
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(a.sqlglot), str(work)], check=True)
    git(work, "checkout", "-q", PINNED)
    commits = a.out / "commits"
    commits.mkdir(parents=True, exist_ok=True)
    meta, seen = {}, set()
    for kind, n in COUNTS.items():
        made = 0
        while made < n:
            res = gen.make(kind)
            if not res:
                continue
            files, desc = res
            for rel, body in files.items():
                p = work / rel
                if body is None:
                    p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(body)
            git(work, "add", "-A")
            diff = git(work, "diff", "--cached", "--abbrev=7")
            git(work, "reset", "-q", "--hard", PINNED)
            git(work, "clean", "-qfd")
            if not diff or diff in seen:
                continue
            seen.add(diff)
            pid = gen.new_id()
            (commits / f"{pid}.patch").write_text(diff)
            # every patch must apply to the pristine pinned tree
            subprocess.run(["git", "-C", str(a.sqlglot), "apply", "--check", str((commits / f"{pid}.patch").resolve())],
                           check=True)
            meta[pid] = {"kind": kind, "desc": desc, "files": sorted(files)}
            made += 1
    ids = sorted(meta)
    random.Random(a.seed).shuffle(ids)
    (a.out / "ORDER").write_text("\n".join(ids) + "\n")
    (a.out / "meta.json").write_text(json.dumps(meta, indent=1))
    shutil.rmtree(tmp)
    print(f"{len(meta)} patches -> {commits}")


if __name__ == "__main__":
    main()
