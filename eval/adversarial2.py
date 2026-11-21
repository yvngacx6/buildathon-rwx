#!/usr/bin/env python3
"""Write the ADVERSARIAL-2 private patch set: probes aimed at the selector's newer narrowing.

usage: python3 eval/adversarial2.py <sqlglot checkout @ eb11aec> <outdir>

Round 2 of eval/adversarial.py. Each probe is a realistic one-token or small edit (like
sample/commits) placed where one narrowing assumption could be wrong:

  * name narrowing (selector/names.py, `_by_names`): reads through import aliases, f-strings,
    lambdas in class-body tables, functools.partial, default arguments, copies into other
    classes / modules, values consumed at import by other modules;
  * import-state diff (`_by_state`, selector/state.py): class attributes consumed by the
    metaclass / `__init_subclass__`, closures built at import, held reads of Expression classes;
  * shared mutable state: a class body that aliases (instead of copying) another class's
    dict / set and then mutates it, so importing one dialect changes what every later test in
    the same worker sees;
  * slow-test statement rule (`classify.touched_statements`), import bound (`_import_bound`),
    lambda handling, and multi-file patches mixing these kinds.

Writes <outdir>/commits/<name>.patch and <outdir>/meta.json ({name: {"kind": ..., "what": ...}}).
Labels come from running the full suite in the pinned image (eval/label on branch valset);
patches and labels are never committed and nothing derived from them is read by the selector.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import sys
from pathlib import Path

SG = Path(sys.argv[1])
OUT = Path(sys.argv[2])
(OUT / "commits").mkdir(parents=True, exist_ok=True)
META: dict[str, dict[str, str]] = {}


def read(path: str) -> str:
    return (SG / path).read_text()


def _blob(text: str | None) -> str:
    if text is None:
        return "0000000"
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()[:7]


def file_diff(path: str, old: str | None, new: str | None) -> str:
    a = "a/" + path if old is not None else "/dev/null"
    b = "b/" + path if new is not None else "/dev/null"
    head = f"diff --git a/{path} b/{path}\n"
    if old is None:
        head += "new file mode 100644\n"
    if new is None:
        head += "deleted file mode 100644\n"
    head += f"index {_blob(old)}..{_blob(new)}" + (" 100644\n" if old is not None and new is not None else "\n")
    body = "".join(difflib.unified_diff((old or "").splitlines(True), (new or "").splitlines(True), a, b))
    return head + body


def replaced(path: str, old: str, new: str, text: str | None = None) -> str:
    text = read(path) if text is None else text
    n = text.count(old)
    assert n == 1, f"{path}: {old!r} found {n} times"
    return text.replace(old, new, 1)


def emit(name: str, kind: str, what: str, parts: list[str]) -> None:
    assert name not in META, name
    META[name] = {"kind": kind, "what": what}
    (OUT / "commits" / f"{name}.patch").write_text("".join(parts))


def R(name: str, kind: str, what: str, path: str, old: str, new: str) -> None:
    emit(name, kind, what, [file_diff(path, read(path), replaced(path, old, new))])


def C(name: str, kind: str, what: str, path: str, anchor: str, old: str, new: str) -> None:
    """Replace `old` by `new` in the first line containing it after the (unique) `anchor` line."""
    text = read(path)
    assert text.count(anchor) == 1, f"{path}: anchor {anchor!r}"
    start = text.index(anchor)
    at = text.index(old, start)
    assert text.count("\n", start, at) < 40, f"{path}: {old!r} too far from the anchor"
    emit(name, kind, what, [file_diff(path, text, text[:at] + new + text[at + len(old):])])


def M(name: str, kind: str, what: str, edits: list[tuple[str, str, str]]) -> None:
    """Several edits; edits to the same file are applied in order."""
    files: dict[str, str] = {}
    for p, o, n in edits:
        files[p] = replaced(p, o, n, files.get(p))
    emit(name, kind, what, [file_diff(p, read(p), t) for p, t in files.items()])


# ── shared mutable state: a class body aliases another class's container, then mutates it ──
R("a2_alias_teradata_single", "shared-state alias", "Teradata SINGLE_TOKENS aliases the base "
  "Tokenizer dict; the following .pop('%') then removes % from every later tokenizer",
  "sqlglot/dialects/teradata.py",
  "        SINGLE_TOKENS = {**tokens.Tokenizer.SINGLE_TOKENS}\n",
  "        SINGLE_TOKENS = tokens.Tokenizer.SINGLE_TOKENS\n")
R("a2_alias_redshift_single", "shared-state alias", "Redshift drops .copy() of Postgres SINGLE_TOKENS"
  " (then pops '#')", "sqlglot/dialects/redshift.py",
  "SINGLE_TOKENS = Postgres.Tokenizer.SINGLE_TOKENS.copy()",
  "SINGLE_TOKENS = Postgres.Tokenizer.SINGLE_TOKENS")
R("a2_alias_databricks_coerce", "shared-state alias", "Databricks COERCES_TO without deepcopy: "
  "the |= loop grows TypeAnnotator's shared sets", "sqlglot/dialects/databricks.py",
  "COERCES_TO = defaultdict(set, deepcopy(TypeAnnotator.COERCES_TO))",
  "COERCES_TO = defaultdict(set, TypeAnnotator.COERCES_TO)")
R("a2_alias_hive_coerce", "shared-state alias", "Hive COERCES_TO without deepcopy",
  "sqlglot/dialects/hive.py",
  "COERCES_TO = defaultdict(set, deepcopy(TypeAnnotator.COERCES_TO))",
  "COERCES_TO = defaultdict(set, TypeAnnotator.COERCES_TO)")
R("a2_shared_bq_coerce_key", "shared-state alias", "BigQuery `COERCES_TO[k] |= {...}` on a shallow "
  "copy mutates TypeAnnotator's set: change the key", "sqlglot/dialects/bigquery.py",
  "    COERCES_TO[exp.DType.BIGINT] |= {exp.DType.BIGDECIMAL}\n",
  "    COERCES_TO[exp.DType.INT] |= {exp.DType.BIGDECIMAL}\n")
R("a2_shared_bq_coerce_member", "shared-state alias", "BigQuery VARCHAR |= set loses a member",
  "sqlglot/dialects/bigquery.py",
  "    COERCES_TO[exp.DType.VARCHAR] |= {\n        exp.DType.DATE,\n",
  "    COERCES_TO[exp.DType.VARCHAR] |= {\n")
R("a2_shared_databricks_member", "shared-state alias", "Databricks TEXT |= set loses BOOLEAN (own "
  "deepcopy: control)", "sqlglot/dialects/databricks.py",
  "            exp.DType.BINARY,\n            exp.DType.BOOLEAN,\n",
  "            exp.DType.BINARY,\n")
R("a2_pop_duckdb_datepart", "class-body mutation", "DuckDB pops another DATE_PART_MAPPING key",
  "sqlglot/dialects/duckdb.py", 'DATE_PART_MAPPING.pop("WEEKDAY")', 'DATE_PART_MAPPING.pop("DOW")')
R("a2_pop_starrocks_kw", "class-body mutation", "StarRocks tokenizer keeps IGNORE",
  "sqlglot/dialects/starrocks.py", '        KEYWORDS.pop("IGNORE")\n', "")

# ── name narrowing: aliases, f-strings, partial, defaults, copies, import-time consumers ──────
R("a2_fstr_hive_dateint", "name: f-string read", "module constant read only inside f-strings of "
  "class-body TRANSFORMS lambdas", "sqlglot/generators/hive.py",
  "HIVE_DATEINT_FORMAT = \"'yyyyMMdd'\"", "HIVE_DATEINT_FORMAT = \"'yyyyMMDD'\"")
R("a2_fstr_snowflake_precision", "name: f-string read", "MAX_PRECISION read in f-strings and "
  "comparisons", "sqlglot/typing/snowflake.py", "MAX_PRECISION = 38\n", "MAX_PRECISION = 37\n")
R("a2_alias_exasol_units", "name: import alias", "DATE_UNITS re-exported `as DATE_UNITS` and read "
  "in another module's functions and in a class-body comprehension", "sqlglot/parsers/exasol.py",
  'DATE_UNITS = {"DAY", "WEEK", "MONTH", "YEAR", "HOUR", "MINUTE", "SECOND"}',
  'DATE_UNITS = {"DAY", "WEEK", "MONTH", "YEAR", "HOUR", "MINUTE"}')
R("a2_partial_hive_unnest", "name: functools.partial", "keyword of a partial inside a class-body "
  "preprocess list", "sqlglot/generators/hive.py",
  "partial(transforms.unnest_to_explode, unnest_using_arrays_zip=False)",
  "partial(transforms.unnest_to_explode, unnest_using_arrays_zip=True)")
R("a2_default_duckdb_series", "name: default argument", "default of a builder factory evaluated "
  "at import, captured by the closure", "sqlglot/parsers/duckdb.py",
  "def _build_generate_series(end_exclusive: bool = False)",
  "def _build_generate_series(end_exclusive: bool = True)")
R("a2_closure_duckdb_sort", "name: closure argument", "argument of a builder factory called in a "
  "class-body table", "sqlglot/parsers/duckdb.py",
  '"ARRAY_REVERSE_SORT": _build_list_sort("ARRAY_REVERSE_SORT", desc=True)',
  '"ARRAY_REVERSE_SORT": _build_list_sort("ARRAY_REVERSE_SORT", desc=False)')
R("a2_env_lambda_mod", "lambda: module table", "executor ENV lambda body (operands swapped)",
  "sqlglot/executor/env.py", '"MOD": null_if_any(lambda e, this: e % this)',
  '"MOD": null_if_any(lambda e, this: this % e)')
R("a2_env_filter_nulls", "name: call in module table", "argument of a wrapper call in ENV",
  "sqlglot/executor/env.py", '"COUNT": filter_nulls(lambda acc: sum(1 for _ in acc), False)',
  '"COUNT": filter_nulls(lambda acc: sum(1 for _ in acc), True)')
R("a2_env_lambda_default", "lambda: default argument", "default of a lambda in ENV",
  "sqlglot/executor/env.py", '"TRIM": null_if_any(lambda this, e=None: this.strip(e))',
  '"TRIM": null_if_any(lambda this, e=" ": this.strip(e))')
R("a2_typing_databricks_member", "name: module comprehension", "typing table built by a "
  "comprehension, re-exported through dialects", "sqlglot/typing/databricks.py",
  "            exp.RegrSyy,\n            exp.Rint,\n", "            exp.RegrSyy,\n")
R("a2_hive_lambda_dateto", "lambda: class-body table", "TRANSFORMS lambda body edit (f-string)",
  "sqlglot/generators/hive.py",
  "            f\"CAST(DATE_FORMAT({self.sql(e, 'this')}, {HIVE_DATEINT_FORMAT}) AS INT)\"",
  "            f\"CAST(DATE_FORMAT({self.sql(e, 'this')}, {HIVE_DATEINT_FORMAT}) AS BIGINT)\"")

# ── lambdas ─────────────────────────────────────────────────────────────────────────────────
R("a2_lambda_trino_arraysum", "lambda: class-body table", "multi-line TRANSFORMS lambda in a "
  "lazily loaded generator", "sqlglot/generators/trino.py",
  'f"REDUCE({self.sql(e, \'this\')}, 0, (acc, x) -> acc + x, acc -> acc)"',
  'f"REDUCE({self.sql(e, \'this\')}, 1, (acc, x) -> acc + x, acc -> acc)"')
R("a2_lambda_trino_groupconcat", "lambda: class-body table", "keyword inside a one-line lambda",
  "sqlglot/generators/trino.py",
  "exp.GroupConcat: lambda self, e: groupconcat_sql(self, e, on_overflow=True)",
  "exp.GroupConcat: lambda self, e: groupconcat_sql(self, e, on_overflow=False)")
R("a2_lambda_typing_databricks", "lambda: module table", "annotator lambda in a module-level "
  "typing table", "sqlglot/typing/databricks.py", '"ARRAY<STRING>", dialect="databricks"',
  '"ARRAY<VARCHAR>", dialect="databricks"')
R("a2_lambda_typing_comprehension", "lambda: module comprehension", "annotator lambda shared by "
  "every entry of a dict comprehension", "sqlglot/typing/tsql.py",
  'expr_type: {"annotator": lambda self, e: self._annotate_by_args(e, "this")}',
  'expr_type: {"annotator": lambda self, e: self._annotate_by_args(e, "expression")}')
R("a2_lambda_tsql_count", "lambda: class-body table", "parser FUNCTIONS lambda keyword",
  "sqlglot/parsers/tsql.py",
  '"COUNT": lambda args: exp.Count(this=seq_get(args, 0), expressions=args[1:], big_int=False)',
  '"COUNT": lambda args: exp.Count(this=seq_get(args, 0), expressions=args[1:], big_int=True)')
R("a2_lambda_tsql_charindex", "lambda: class-body table", "multi-line parser lambda, swapped "
  "arguments", "sqlglot/parsers/tsql.py",
  "            this=seq_get(args, 1),\n            substr=seq_get(args, 0),\n",
  "            this=seq_get(args, 0),\n            substr=seq_get(args, 1),\n")

# ── import bound: lazily loaded modules that stop importing ────────────────────────────────
R("a2_import_redshift_keyerror", "import bound", "class body raises KeyError at import of a lazy "
  "dialect", "sqlglot/dialects/redshift.py", '        KEYWORDS.pop("VALUES")\n',
  '        KEYWORDS.pop("VALUE")\n')
R("a2_import_presto_syntax", "import bound", "dropped colon in a lazy generator module (imported "
  "by trino / athena)", "sqlglot/generators/presto.py",
  "def _array_sort_sql(self: PrestoGenerator, expression: exp.ArraySort) -> str:",
  "def _array_sort_sql(self: PrestoGenerator, expression: exp.ArraySort) -> str")
R("a2_import_presto_rename", "import bound", "renamed helper imported by another lazy module",
  "sqlglot/parsers/presto.py", "def _build_approx_percentile(", "def _build_approx_percentile_(")
R("a2_import_typing_redshift", "import bound", "typing module imports a missing name",
  "sqlglot/typing/redshift.py", "from sqlglot.typing.postgres import EXPRESSION_METADATA",
  "from sqlglot.typing.postgres import EXPRESSION_METADATA, TIMESTAMP_EXPRESSIONS")

# ── __getattr__, decorators, super() chains, shadowing, values other modules read at import ──
R("a2_getattr_lazy_attrs", "name: module __getattr__", "lazy attribute table of a package "
  "__getattr__ (run while other modules import it)", "sqlglot/optimizer/__init__.py",
  '    "find_in_scope": ("sqlglot.optimizer.scope", "find_in_scope"),\n',
  '    "find_in_scope": ("sqlglot.optimizer.scope", "find_all_in_scope"),\n')
R("a2_deco_sqlite_unsupported", "decorator argument", "decorator factory argument on a lazy "
  "generator method", "sqlglot/generators/sqlite.py",
  '    @unsupported_args("decimals")\n    def trunc_sql',
  '    @unsupported_args("this")\n    def trunc_sql')
R("a2_super_presto_jsonextract", "super() chain", "parent method reached through super() from a "
  "subclass generator", "sqlglot/generators/presto.py",
  '        if not expression.args.get("variant_extract") or is_json_extract:',
  '        if not expression.args.get("variant_extract") and is_json_extract:')
R("a2_shadow_hive_queryhints", "name: shadowed in subclasses", "parent attribute that Spark2 "
  "overrides", "sqlglot/generators/hive.py", "    QUERY_HINTS = False\n", "    QUERY_HINTS = True\n")
R("a2_shadow_hive_decode", "name: shadowed in subclasses", "parent attribute that Spark overrides",
  "sqlglot/generators/hive.py", "    SUPPORTS_DECODE_CASE = False\n",
  "    SUPPORTS_DECODE_CASE = True\n")
R("a2_import_text_types", "name: read at import by other modules", "DataType.TEXT_TYPES member "
  "(read by Hive / Databricks class bodies and run-time code)", "sqlglot/expressions/datatypes.py",
  "        DType.VARCHAR,\n        DType.NAME,\n    }\n\n    BINARY_TYPES",
  "        DType.VARCHAR,\n    }\n\n    BINARY_TYPES")

# ── multi-file patches mixing kinds ──────────────────────────────────────────────────────────
M("a2_multi_alias_fixture", "multi", "shared-state alias + identity fixture typo", [
  ("sqlglot/dialects/redshift.py", "SINGLE_TOKENS = Postgres.Tokenizer.SINGLE_TOKENS.copy()",
   "SINGLE_TOKENS = Postgres.Tokenizer.SINGLE_TOKENS"),
  ("tests/fixtures/identity.sql", "SELECT 1 FROM test\n", "SELECT 1 FROM tset\n")])
M("a2_multi_parent_child_attr", "multi", "parent generator attr + child generator attr",
  [("sqlglot/generators/hive.py", "    SUPPORTS_DECODE_CASE = False\n",
    "    SUPPORTS_DECODE_CASE = True\n"),
   ("sqlglot/generators/spark.py", "    SUPPORTS_DECODE_CASE = True\n",
    "    SUPPORTS_DECODE_CASE = False\n")])
M("a2_multi_const_and_reader", "multi", "module constant + the function in another module that "
  "reads it", [("sqlglot/parsers/exasol.py",
                'DATE_UNITS = {"DAY", "WEEK", "MONTH", "YEAR", "HOUR", "MINUTE", "SECOND"}',
                'DATE_UNITS = {"DAY", "WEEK", "MONTH", "YEAR", "HOUR", "MINUTE", "SECOND", "QUARTER"}'),
               ("sqlglot/generators/exasol.py",
                '        return self.function_fallback_sql(expression)\n\n'
                '    return self.func(f"{unit}S_BETWEEN"',
                '        return self.function_fallback_sql(expression)\n\n'
                '    return self.func(f"{unit}_BETWEEN"')])
M("a2_multi_lambda_comment", "multi", "lambda body + comment in another module", [
  ("sqlglot/generators/trino.py",
   "exp.ArrayUniqueAgg: lambda self, e: f\"ARRAY_AGG(DISTINCT {self.sql(e, 'this')})\"",
   "exp.ArrayUniqueAgg: lambda self, e: f\"ARRAY_AGG({self.sql(e, 'this')})\""),
  ("sqlglot/optimizer/scope.py", "\nclass Scope:\n", "\n# TODO: split into smaller classes\nclass Scope:\n")])
M("a2_multi_typing_alias", "multi", "typing table + the module that imports it under an alias",
  [("sqlglot/typing/hive.py", "            exp.Corr,\n            exp.MonthsBetween,\n",
    "            exp.MonthsBetween,\n"),
   ("sqlglot/typing/spark2.py", "    **HIVE_EXPRESSION_METADATA,\n",
    "    **HIVE_EXPRESSION_METADATA,\n    exp.Corr: {\"returns\": exp.DType.FLOAT},\n")])
M("a2_multi_teradata_alias_test", "multi", "shared-state alias + a test method signature edit", [
  ("sqlglot/dialects/teradata.py", "        SINGLE_TOKENS = {**tokens.Tokenizer.SINGLE_TOKENS}\n",
   "        SINGLE_TOKENS = tokens.Tokenizer.SINGLE_TOKENS\n"),
  ("tests/dialects/test_teradata.py", "def test_teradata(self):", "def test_teradata(self) -> None:")])

# ── values consumed by the metaclass / class creation, enums, typing tables ──────────────────
R("a2_meta_oracle_time", "state: metaclass-derived", "TIME_MAPPING entry (tries and inverse "
  "mappings derived by the Dialect metaclass)", "sqlglot/dialects/oracle.py",
  '        "DDD": "%j",  # day of year (1-366)\n', '        "DDD": "%J",  # day of year (1-366)\n')
R("a2_meta_teradata_kw", "state: metaclass-derived", "tokenizer KEYWORDS entry (keyword trie "
  "built at class creation)", "sqlglot/dialects/teradata.py",
  '            "MINUS": TokenType.EXCEPT,\n', '            "MINUS": TokenType.INTERSECT,\n')
R("a2_typemap_oracle", "name: class-body table", "generator TYPE_MAPPING entry",
  "sqlglot/generators/oracle.py", '        exp.DType.TINYINT: "SMALLINT",\n',
  '        exp.DType.TINYINT: "TINYINT",\n')
R("a2_enum_dtype_value", "state: enum value", "DType member value (generator falls back to "
  ".value)", "sqlglot/expressions/datatypes.py", "    BPCHAR = auto()\n", '    BPCHAR = "BPCHAR "\n')
R("a2_enum_errorlevel", "state: enum value", "ErrorLevel member value",
  "sqlglot/errors.py", "    WARN = auto()\n", '    WARN = "WARNING"\n')
R("a2_env_avg_call", "name: call in module table", "ENV entry built by a call at import",
  "sqlglot/executor/env.py",
  '"AVG": filter_nulls(statistics.fmean if PYTHON_VERSION >= (3, 8) else statistics.mean)',
  '"AVG": filter_nulls(statistics.median if PYTHON_VERSION >= (3, 8) else statistics.mean)')

# ── one-token class / module attribute edits that name or state narrowing cut hardest ───────
# (picked from a mechanical sweep: one mutation per class-body / module assignment, run
# through the selector; these are the ones narrowed to the fewest tests)
R("a2_n_dremio_values_default", "name: dialect flag", "narrowed to 0 tests",
  "sqlglot/dialects/dremio.py", "    SUPPORTS_VALUES_DEFAULT = False\n",
  "    SUPPORTS_VALUES_DEFAULT = True\n")
R("a2_n_redshift_array_nulls", "name: dialect flag", "read through self.dialect",
  "sqlglot/dialects/redshift.py", "    ARRAY_FUNCS_PROPAGATES_NULLS = True\n",
  "    ARRAY_FUNCS_PROPAGATES_NULLS = False\n")
R("a2_n_duckdb_strict_jsonpath", "name: dialect flag", "read by the JSON path parser",
  "sqlglot/dialects/duckdb.py", "    STRICT_JSON_PATH_SYNTAX = False\n",
  "    STRICT_JSON_PATH_SYNTAX = True\n")
R("a2_n_spark_null_type", "name: dialect flag", "inherited by Databricks",
  "sqlglot/dialects/spark.py", "    SUPPORTS_NULL_TYPE = True\n", "    SUPPORTS_NULL_TYPE = False\n")
R("a2_n_postgres_ascii_norm", "name: dialect flag", "identifier normalization (optimizer)",
  "sqlglot/dialects/postgres.py", "    ASCII_ONLY_NORMALIZATION = True\n",
  "    ASCII_ONLY_NORMALIZATION = False\n")
R("a2_n_clickhouse_norm_funcs", "name: annotated dialect flag", "AnnAssign class attribute",
  "sqlglot/dialects/clickhouse.py", "    NORMALIZE_FUNCTIONS: bool | str = False\n",
  "    NORMALIZE_FUNCTIONS: bool | str = True\n")
C("a2_s_macro_overload_args", "state: held reads (arg_types)", "required arg dropped",
  "sqlglot/expressions/ddl.py", "class MacroOverload(Expression):",
  '{"this": True, "expressions": False, "is_table": False}',
  '{"expressions": False, "is_table": False}')
C("a2_s_mappick_args", "state: held reads (arg_types)", "required arg dropped",
  "sqlglot/expressions/array.py", "class MapPick(Expression, Func):",
  '{"this": True, "expressions": True}', '{"expressions": True}')
C("a2_s_stddev_names", "state: _sql_names", "function name alias dropped",
  "sqlglot/expressions/aggregate.py", "class Stddev(Expression, AggFunc):",
  '["STDDEV", "STDEV"]', '["STDEV"]')
C("a2_s_snowflake_inverse_time", "state: metaclass-merged mapping", "INVERSE_TIME_MAPPING entry "
  "(merged into the derived inverse mapping by the metaclass)", "sqlglot/dialects/snowflake.py",
  "    INVERSE_TIME_MAPPING = {", '        "%d": "DD",\n', "")
R("a2_n_executor_subquery_nodes", "name: module tuple", "module tuple read by isinstance checks",
  "sqlglot/executor/python.py", "SUBQUERY_NODES = (exp.Subquery, exp.Exists, exp.All, exp.Any)",
  "SUBQUERY_NODES = (exp.Exists, exp.All, exp.Any)")

# ── slow-test statement rule: a statement the slow tests (tpcds, max_nodes) never reached ───
C("a2_slow_exec_subqueries", "slow: off touched statement", "loop header in a function tpcds runs",
  "sqlglot/executor/python.py", "    def _replace_subqueries(self, expression):",
  "        while True:\n", "        while False:\n")
C("a2_slow_exec_setop", "slow: off touched statement", "branch condition tpcds never reaches",
  "sqlglot/executor/python.py", "    def set_operation(self, step, context):",
  "(not step.distinct or row not in seen)", "(not step.distinct and row not in seen)")
C("a2_slow_eliminate_ctes", "slow: off touched statement", "journal branch",
  "sqlglot/optimizer/eliminate_ctes.py", "def eliminate_ctes(expression: E, journal: Journal | None = None) -> E:",
  "if journal is not None and with_node:", "if journal is not None or with_node:")
C("a2_slow_merge_derived", "slow: off touched statement", "negated guard",
  "sqlglot/optimizer/merge_subqueries.py", "def merge_derived_tables(",
  "if not isinstance(from_or_join, (exp.From, exp.Join)):",
  "if isinstance(from_or_join, (exp.From, exp.Join)):")

# ── run-time writes to class-level tables ────────────────────────────────────────────────────
R("a2_runtime_prior_leak", "shared state at run time", "the temporary PRIOR parser is no longer "
  "removed from the class-level NO_PAREN_FUNCTION_PARSERS after CONNECT BY",
  "sqlglot/parser.py", '        connect = self._parse_disjunction()\n'
  '        self.NO_PAREN_FUNCTION_PARSERS.pop("PRIOR")\n',
  '        connect = self._parse_disjunction()\n')
R("a2_runtime_prior_key", "shared state at run time", "the temporary parser is registered under "
  "another key (the pop then raises)", "sqlglot/parser.py",
  'self.NO_PAREN_FUNCTION_PARSERS["PRIOR"] = lambda self',
  'self.NO_PAREN_FUNCTION_PARSERS["PRIOR "] = lambda self')

(OUT / "meta.json").write_text(json.dumps(META, indent=1) + "\n")
print(f"{len(META)} probes -> {OUT}", file=sys.stderr)
