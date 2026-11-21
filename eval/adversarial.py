#!/usr/bin/env python3
"""Write an ADVERSARIAL private patch set against the pinned sqlglot, for measuring recall only.

usage: python3 eval/adversarial.py <sqlglot checkout> <outdir>

Each patch aims at a spot where a coverage-map selector is structurally blind: code that runs at
import or class-creation time, decorators and their factories, default arguments, class-body
dict lambdas, metaclass / __init_subclass__ derived settings, lazy dialect loading, deleted or
renamed names, subclass dialects, Expression arg_types, serde, the executor, test helpers,
fixture metadata, fixtures read in setUpClass, diff-format corner cases, coding cookies, typing/.

Writes <outdir>/commits/<name>.patch and <outdir>/kinds.json ({name: target}). The labels
(which tests fail) come from running the full suite in the pinned image; patches and labels are
never committed and nothing derived from them is read by the selector.
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

SG = Path(sys.argv[1])
OUT = Path(sys.argv[2])
(OUT / "commits").mkdir(parents=True, exist_ok=True)
KINDS: dict[str, str] = {}


def read(path: str) -> str:
    return (SG / path).read_text()


def file_diff(path: str, old: str | None, new: str | None) -> str:
    a = "a/" + path if old is not None else "/dev/null"
    b = "b/" + path if new is not None else "/dev/null"
    head = f"diff --git a/{path} b/{path}\n"
    if old is None:
        head += "new file mode 100644\n"
    if new is None:
        head += "deleted file mode 100644\n"
    body = "".join(difflib.unified_diff((old or "").splitlines(True), (new or "").splitlines(True), a, b))
    return head + body


def replaced(path: str, old: str, new: str, count: int = 1) -> str:
    text = read(path)
    assert text.count(old) >= 1, f"{path}: {old!r} not found"
    return text.replace(old, new, count)


def emit(name: str, kind: str, parts: list[str]) -> None:
    KINDS[name] = kind
    (OUT / "commits" / f"{name}.patch").write_text("".join(parts))


def R(name: str, kind: str, path: str, old: str, new: str, count: int = 1) -> None:
    emit(name, kind, [file_diff(path, read(path), replaced(path, old, new, count))])


def M(name: str, kind: str, edits: list[tuple[str, str, str]]) -> None:
    emit(name, kind, [file_diff(p, read(p), replaced(p, o, n)) for p, o, n in edits])


# ── code that runs at import / class-creation time ───────────────────────────────────────────
R("imp_new_trie", "import-time function (trie built in __init_subclass__ / metaclass)",
  "sqlglot/trie.py", "current = current.setdefault(char, {})",
  "current = current.setdefault(char.lower() if isinstance(char, str) else char, {})")
R("imp_quotes_to_format", "import-time function called from __init_subclass__",
  "sqlglot/tokens.py", "return {k: (v, token_type) for k, v in _convert_quotes(arr).items()}",
  "return {k: (k, token_type) for k, v in _convert_quotes(arr).items()}")
R("imp_strict_inverse", "function called from metaclass __new__",
  "sqlglot/dialects/dialect.py", "        if strict_format in inverse_mapping:\n",
  "        if strict_format not in inverse_mapping:\n")
R("imp_coerces_to", "module-level X = build() function",
  "sqlglot/optimizer/annotate_types.py", "        exp.DType.BIGINT,\n        exp.DType.INT,\n",
  "        exp.DType.INT,\n        exp.DType.BIGINT,\n")
R("imp_gen_dispatch", "module-level X = build() function",
  "sqlglot/optimizer/simplify.py", "dispatch[name[:-4]] = getattr(Gen, name)",
  "dispatch[name[:-4].upper()] = getattr(Gen, name)")
R("imp_subclasses", "helper run at import (ALL_FUNCTIONS)",
  "sqlglot/helper.py", "issubclass(obj, classes) and obj not in exclude",
  "issubclass(obj, classes) and obj not in exclude and not obj.__name__.startswith('Array')")
R("imp_dialects_getattr", "lazy dialect loading __getattr__",
  "sqlglot/dialects/__init__.py", "    module_name = MODULE_BY_ATTRIBUTE.get(name)\n",
  "    module_name = MODULE_BY_ATTRIBUTE.get(name.capitalize())\n")
R("imp_null_if_any", "decorator factory run at import (executor env)",
  "sqlglot/executor/env.py", "    if len(required) == 1 and callable(required[0]):\n",
  "    if len(required) >= 1 and callable(required[0]):\n")
R("imp_unsupported_args", "decorator factory outer body",
  "sqlglot/generator.py", "            diagnostic_by_arg[arg] = None\n",
  "            diagnostic_by_arg[arg] = f\"Argument '{arg}' is not supported\"\n")
R("imp_rename_func", "factory whose lambda runs later",
  "sqlglot/dialects/dialect.py",
  "return lambda self, expression: self.func(name, *flatten(expression.args.values()))",
  "return lambda self, expression: self.func(name.lower(), *flatten(expression.args.values()))")
R("imp_dialects_list", "module-level list in dialects/__init__",
  "sqlglot/dialects/__init__.py", '    "Druid",\n', "")
R("imp_func_by_name", "module-level dict in expressions/__init__",
  "sqlglot/expressions/__init__.py",
  "FUNCTION_BY_NAME = {name: func for func in ALL_FUNCTIONS for name in func.sql_names()}",
  "FUNCTION_BY_NAME = {name: func for func in ALL_FUNCTIONS for name in func.sql_names()[:1]}")

# ── deleted / renamed names ──────────────────────────────────────────────────────────────────
src = read("sqlglot/dialects/dialect.py")
start = src.index("def approx_count_distinct_sql(")
end = src.index("def if_sql(")
emit("del_func_imported", "deleted function imported by other modules",
     [file_diff("sqlglot/dialects/dialect.py", src, src[:start] + src[end:])])
R("ren_func_imported", "renamed function imported by other modules",
  "sqlglot/dialects/dialect.py", "def no_ilike_sql(", "def no_ilike_to_like_sql(")
R("ren_const_imported", "renamed module constant imported by other modules",
  "sqlglot/generators/hive.py", 'HIVE_DATE_FORMAT = "\'yyyy-MM-dd\'"', 'HIVE_DATE_FMT = "\'yyyy-MM-dd\'"')

# ── class bodies, subclasses, metaclass-derived settings ─────────────────────────────────────
R("cls_hive_gen_attr", "parent generator class attr (Spark/Databricks inherit)",
  "sqlglot/generators/hive.py", '    LIMIT_FETCH = "LIMIT"\n', '    LIMIT_FETCH = "FETCH"\n')
R("cls_hive_transforms_lambda", "class-body TRANSFORMS lambda",
  "sqlglot/generators/hive.py",
  'exp.ArrayToString: lambda self, e: self.func("CONCAT_WS", e.expression, e.this),',
  'exp.ArrayToString: lambda self, e: self.func("CONCAT_WS", e.this, e.expression),')
R("cls_hive_time_mapping", "metaclass-derived TIME_TRIE / INVERSE_TIME_MAPPING",
  "sqlglot/dialects/hive.py", '        "yy": "%y",\n', '        "yy": "%Y",\n')
R("cls_hive_quotes", "tokenizer setting compiled by __init_subclass__",
  "sqlglot/dialects/hive.py", "        QUOTES = [\"'\", '\"']\n", "        QUOTES = [\"'\"]\n")
R("cls_arg_types_required", "Expression arg_types",
  "sqlglot/expressions/functions.py",
  'arg_types = {"this": True, "expressions": False, "is_nvl": False, "is_null": False}',
  'arg_types = {"this": True, "expressions": True, "is_nvl": False, "is_null": False}')
R("cls_var_len_args", "Expression is_var_len_args",
  "sqlglot/expressions/string.py",
  'arg_types = {"expressions": True, "safe": False, "coalesce": False}\n    is_var_len_args = True\n',
  'arg_types = {"expressions": True, "safe": False, "coalesce": False}\n')
R("cls_sql_names", "Expression _sql_names",
  "sqlglot/expressions/functions.py", '_sql_names = ["COALESCE", "IFNULL", "NVL"]',
  '_sql_names = ["COALESCE", "IFNULL"]')
R("cls_error_base", "exception class hierarchy",
  "sqlglot/errors.py", "class UnsupportedError(SqlglotError):", "class UnsupportedError(Exception):")
R("cls_expr_init_subclass", "Expr.__init_subclass__ (required_args)",
  "sqlglot/expressions/core.py", "cls.required_args = {k for k, v in cls.arg_types.items() if v}",
  "cls.required_args = {k for k, v in cls.arg_types.items() if v is True}")

# ── module-level data, serde, executor, typing ───────────────────────────────────────────────
R("mod_serde_key", "serde constant (pickling path via process pool)",
  "sqlglot/serde.py", 'ARG_KEY = "k"', 'ARG_KEY = "a"')
R("fn_reduce", "Expr.__reduce__ (pickling)",
  "sqlglot/expressions/core.py", "        return (load, (dump(self),))\n",
  "        return (load, (dump(self.copy()),)) if self.parent else (load, (dump(self),))\n")
R("exec_env_decorated", "executor env decorated function",
  "sqlglot/executor/env.py", "    return this.find(substr, position) + 1\n",
  "    return this.find(substr, position)\n")
R("typing_init_entry", "typing/__init__ metadata table",
  "sqlglot/typing/__init__.py", "            exp.Length,\n", "")
R("typing_duckdb", "typing/<dialect> table",
  "sqlglot/typing/duckdb.py", "    exp.DType.TIMESTAMP,\n", "", 1)

# ── default arguments, strings, cookies ──────────────────────────────────────────────────────
R("default_arg_new_trie", "default argument of an import-time function",
  "sqlglot/trie.py", "    trie = {} if trie is None else trie\n", "    trie = {0: False} if trie is None else trie\n")
R("cookie_ascii", "coding cookie",
  "sqlglot/optimizer/resolver.py", "from __future__ import annotations\n",
  "# -*- coding: ascii -*-\nfrom __future__ import annotations\n")

# ── tests side ───────────────────────────────────────────────────────────────────────────────
R("test_helper_meta", "tests/helpers.py fixture metadata parsing",
  "tests/helpers.py", 'meta[key.lstrip("#").strip()] = val.strip()', 'meta[key.lstrip("#")] = val.strip()')
R("test_validator", "Validator helper in a test file",
  "tests/dialects/test_dialect.py",
  "        self, sql, write_sql=None, pretty=False, check_command_warning=False, identify=False\n",
  "        self, sql, write_sql=None, pretty=False, check_command_warning=False, identify=True\n")
R("test_setupclass_data", "setUpClass data",
  "tests/test_optimizer.py", "        INSERT INTO x VALUES (1, 1);\n", "        INSERT INTO x VALUES (1, 2);\n")

# ── fixtures ─────────────────────────────────────────────────────────────────────────────────
src = read("tests/fixtures/optimizer/simplify.sql").splitlines(True)
i = next(k for k, l in enumerate(src) if l.startswith("# dialect: duckdb"))
new = src[:i] + ["# dialect: postgres\n"] + src[i + 1:]
emit("fix_meta_line", "fixture '#' metadata line",
     [file_diff("tests/fixtures/optimizer/simplify.sql", "".join(src), "".join(new))])
R("fix_setupclass_tpch", "fixture read in setUpClass",
  "tests/fixtures/optimizer/tpc-h/tpc-h.sql", "SELECT", "SELECT DISTINCT", 1)
emit("fix_rename_plus_comment", "pure rename (no hunks) next to a comment edit", [
    "diff --git a/tests/fixtures/pretty.sql b/tests/fixtures/pretty2.sql\n"
    "similarity index 100%\nrename from tests/fixtures/pretty.sql\nrename to tests/fixtures/pretty2.sql\n",
    file_diff("sqlglot/trie.py", read("sqlglot/trie.py"),
              replaced("sqlglot/trie.py", "class TrieResult(Enum):\n", "# results\nclass TrieResult(Enum):\n")),
])
emit("fix_delete_empty_init", "deleted empty file (no hunks) next to another edit", [
    "diff --git a/tests/__init__.py b/tests/__init__.py\ndeleted file mode 100644\nindex e69de29..0000000\n",
    file_diff("sqlglot/trie.py", read("sqlglot/trie.py"),
              replaced("sqlglot/trie.py", "class TrieResult(Enum):\n", "# results\nclass TrieResult(Enum):\n")),
])
R("fix_jsonpath_cts", "json fixture",
  "tests/fixtures/jsonpath/cts.json", '"selector": "$', '"selector": "$$', 1)

# ── multi-file: comment + import-time change elsewhere ───────────────────────────────────────
M("multi_comment_and_import_time", "multi: comment + import-time code", [
    ("sqlglot/optimizer/scope.py", "from sqlglot import exp\n", "from sqlglot import exp\n# Kept for backwards compatibility\n"),
    ("sqlglot/tokens.py", "    return dict((item, item) if isinstance(item, str) else (item[0], item[1]) for item in arr)",
     "    return dict((item, item) if isinstance(item, str) else (item[0], item[0]) for item in arr)"),
])

# ── round 2: names removed from non-base modules, factory outer bodies, derived tables ───────
src = read("sqlglot/transforms.py")
start = src.index("def move_schema_columns_to_partitioned_by(")
end = src.index("\ndef ", start + 10) + 1
emit("del_transforms_func", "deleted function imported by generators",
     [file_diff("sqlglot/transforms.py", src, src[:start] + src[end:])])
R("ren_scope_func", "renamed function imported across optimizer",
  "sqlglot/optimizer/scope.py", "def traverse_scope(expression: exp.Expr) -> list[Scope]:",
  "def traverse_scopes(expression: exp.Expr) -> list[Scope]:")
R("imp_binary_range_default", "default argument of an import-time factory",
  "sqlglot/parser.py", "    expr_type: Type[exp.Expr], reverse_args: bool = False\n",
  "    expr_type: Type[exp.Expr], reverse_args: bool = True\n")
R("imp_formatted_time_closure", "closure body of an import-time factory",
  "sqlglot/dialects/dialect.py", "            fmt = target_dialect.TIME_FORMAT if default is True else default or None\n",
  "            fmt = target_dialect.TIME_FORMAT if default else None\n")
R("mod_hive_lax_formats", "module dict read by functions",
  "sqlglot/generators/hive.py", '    "%m": "%-m",\n', '    "%m": "%m",\n')
R("imp_seq_get", "tiny helper used everywhere",
  "sqlglot/helper.py", "def seq_get(", "def seq_get_(", )

(OUT / "kinds.json").write_text(json.dumps(KINDS, indent=1))
print(f"{len(KINDS)} patches → {OUT}/commits")
