"""Generate a PRIVATE validation set of synthetic patches against the pinned sqlglot (for measuring selector recall only;
nothing derived from their outcomes is shipped). usage: gen_validation.py <sqlglot> <outdir> [seed]"""
import difflib, os, random, re, sys
SG, OUT = sys.argv[1], sys.argv[2]; rnd = random.Random(int(sys.argv[3]) if len(sys.argv) > 3 else 7)
os.makedirs(OUT, exist_ok=True)
def rd(p): return open(os.path.join(SG, p)).read()
def emit(name, path, new, old=None, newpath=None):
    old = rd(path) if old is None else old
    d = difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                             "a/" + path if old else "/dev/null", "b/" + (newpath or path) if new else "/dev/null")
    open(os.path.join(OUT, name + ".patch"), "w").write("".join(d))
OPS = [(r"\band\b", "or"), (r"\bor\b", "and"), (r" == ", " != "), (r" != ", " == "), (r" is not ", " is "),
       (r" < ", " <= "), (r" > ", " >= "), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\bnot ", ""),
       (r"(?<![\w.])(\d+)(?![\w.])", lambda m: str(int(m.group(1)) + 1))]
def line_mutant(name, path, want=None):
    src = rd(path).splitlines(True)
    cands = [(i, o) for i, l in enumerate(src) for o in OPS
             if re.search(o[0], l) and not l.lstrip().startswith(("#", '"', "'")) and (want is None or want(l))]
    i, (pat, rep) = rnd.choice(cands)
    src[i] = re.sub(pat, rep, src[i], count=1); emit(name, path, "".join(src))
# 1) in-distribution style: random operator mutants over random sqlglot modules (incl. executor, tokenizer, core)
files = [os.path.relpath(os.path.join(r, f), SG) for r, _, fs in os.walk(SG + "/sqlglot") for f in fs if f.endswith(".py")]
for k in range(14): line_mutant(f"op{k:02d}", rnd.choice(files))
line_mutant("exec1", "sqlglot/executor/python.py"); line_mutant("tok1", "sqlglot/tokens.py")
line_mutant("cls1", "sqlglot/dialects/dialect.py", lambda l: l.startswith("    ") and not l.startswith("     ") and "=" in l)
# 2) kinds not in the sample
s = rd("sqlglot/dialects/duckdb.py"); emit("syntaxerr", "sqlglot/dialects/duckdb.py", s.replace("class DuckDB(Dialect):", "class DuckDB(Dialect)", 1))
s = rd("sqlglot/dialects/druid.py"); emit("delete_dialect", "sqlglot/dialects/druid.py", "", old=s)
emit("newfile", "sqlglot/extra_util.py", "def unused():\n    return 1\n", old="")
s = rd("tests/dialects/test_dialect.py"); emit("helper_validator", "tests/dialects/test_dialect.py", s.replace("pretty=False, check_command_warning", "pretty=True, check_command_warning", 1))
s = rd("tests/helpers.py"); emit("helper_fixtures", "tests/helpers.py", s.replace("statements[i + 1].strip()", "statements[i + 1].strip().lower()", 1))
s = rd("tests/test_parser.py"); emit("testedit", "tests/test_parser.py", s.replace("def test_union(self):\n", "def test_union(self):\n        self.assertTrue(False)\n", 1))
s = rd("pyproject.toml"); emit("pyproject", "pyproject.toml", s.replace("[tool.ruff]", "[tool.ruff]\n# tweak", 1))
s = rd("sqlglot/optimizer/simplify.py"); emit("docstring", "sqlglot/optimizer/simplify.py", re.sub(r'(""")(\s*\n)', r'\1 Note.\2', s, count=1))
s = rd("sqlglot/__init__.py"); emit("init_import", "sqlglot/__init__.py", s.replace("import logging\n", "import logging\nimport sqlglot_nonexistent_mod\n", 1))
s = rd("sqlglot/generator.py"); emit("whitespace", "sqlglot/generator.py", s.replace("\n\n\n", "\n\n\n\n", 1))
s = rd("sqlglot/optimizer/qualify_columns.py")
emit("rename_local", "sqlglot/optimizer/qualify_columns.py", re.sub(r"\bscope\b", "scp", s, count=0) if False else s.replace("def qualify_columns(", "def qualify_columns(  # noqa\n", 1).replace("  # noqa\n", "", 1) + "\n_ALIAS = 1\n")
s = rd("sqlglot/dialects/mysql.py"); emit("baseclass_attr", "sqlglot/dialects/mysql.py", s.replace("class MySQL(Dialect):\n", "class MySQL(Dialect):\n    TYPED_DIVISION = True\n", 1))
s = rd("tests/fixtures/identity.sql"); emit("fixture_identity", "tests/fixtures/identity.sql", s.replace("\n", "\nSELECT 1 1\n", 1))
