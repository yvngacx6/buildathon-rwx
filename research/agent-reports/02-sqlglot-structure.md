# 02 — sqlglot structure, fixture→test mapping, per-test coverage map

Agent angle: how sqlglot @ `eb11aec4` is laid out, how fixtures map to tests, and how well a
per-test line-coverage map built from the pinned commit predicts the 30 sample answers.
Scratch: `~/rwx-research/` (clone, venv, maps). Scripts: `research/agent-reports/02-structure/`.

## Executive summary

| Selector (sample, 30 commits) | misses | simulated s | tests selected | selected s |
|---|---|---|---|---|
| Starter (all tests, 3 workers) | 0 | 1210.5 | – | – |
| A. Raw line coverage, naive build (spawn pools not captured) | 3 | 1031.9 | 206 | 92.6 |
| B. Raw line coverage, pickle-faithful build | 2 | 753.8 | 231 | 126.0 |
| C. B + statement expansion + pure-comment skip | 1 | 454.2 (154.2 + 300) | 255 | 129.9 |
| D. C + import-time fallback = dialect family (all tests touching dialects/parsers/generators/typing `<name>.py`) | 0 | **171.4** | 790 | 148.4 |
| E. D, but family ∩ tests touching lines that name the changed UPPER_CASE attr | 0 | **166.7** | 616 | 141.9 |

(simulated = scorer formula, best of 1..8 workers via LPT; CPU time not included.)

Key facts:
- **Build cost:** full suite under `pytest-cov --cov-context=test -n 4` + inline-executor plugin:
  **128 s wall, 307 s CPU, ~1.9 GB peak RSS** on the laptop (plain `-n 8` run, no coverage: 47 s).
  All 1621 tests pass under the plugin (1620 + the sqlite test the labeling image skips; local
  SQLite is newer — harmless env diff).
- **Map size:** coverage SQLite `.coverage` = **7.4 MB**; exported `{file:{line:[test idx]}}` JSON
  = 12.4 MB raw / **0.68 MB gzipped**. Committable (20 MB limit), so the Docker build need not rebuild it.
  Export takes 4 s. (Laptop numbers; build box is 4 cores — expect maybe 2–3 min if rebuilt.)
- **Headline finding 1 — process boundary.** `TestOptimizer.check_file` (every `*.sql` optimizer
  fixture test) and `TestExecutor._mp_execute` run their work in a `ProcessPoolExecutor(spawn)`.
  Coverage in the parent sees *none* of it. My plugin (`covplugin.py`) swaps it for an inline executor —
  but that alone missed 24/37 failures of 3utz, because Expr crosses the process boundary via
  `Expr.__reduce__ → sqlglot.serde.dump/load`. Adding a `pickle.loads(pickle.dumps(...))` round-trip on
  args and results fixes it (0 misses on 3utz).
- **Headline finding 2 — short-circuit.** rszg flips `and`→`or` inside a multi-line `if (...)`.
  test_optimize never reaches line 101 (earlier `and` operand false), so raw line coverage misses it.
  Expanding the changed line to its whole statement/compound-header (AST) catches it.
- **Headline finding 3 — import-time code.** 31% of covered sqlglot lines (16,290 / 53,027) are
  covered *only* at import time (module/class bodies, `def` lines, class-level dicts). pytest-cov
  attributes them to the empty context or to whichever test happened to import the module first
  on that xdist worker (tsql.py:17 → 4 random tests). 535z (`TSQL.WEEK_OFFSET = -2`) fails
  test_simplify, which executes **zero** lines of `dialects/tsql.py` but does execute
  `parsers/tsql.py` + `generators/tsql.py`. Hence the "dialect family" fallback.
- **Modes D/E are heuristics tuned on n=30** (fallbacks exist because of 535z). C is the honest
  answer to "does a line map predict them": yes for 29/30 with statement expansion.
- **Remaining cost is equivalent mutants**: 2haz (`is`→`==` on an Enum) and 66ks (`is None`→`== None`)
  each select test_execute_tpcds (21.5 s) for zero failures (~25 s each, ~50 s of the 167).
  2jrq and 3utz legitimately need tpcds. Those 4 commits ≈ 101 s of 167 s.

## 1. Package layout and loading

- `sqlglot/__init__.py` imports core eagerly: `import sqlglot` loads 36 sqlglot modules
  (expressions/*, tokens, tokenizer_core, parser, generator, parsers.base, dialects.dialect, typing,
  helper, errors, schema, ...). Dialect-specific modules are **lazy**.
- Each dialect is split in 4 parallel trees by lowercase name: `dialects/X.py` (the `Dialect`
  subclass: class-level settings like `WEEK_OFFSET`, `NUMERIC_ESCAPES`, nested `Tokenizer`),
  `parsers/X.py` (`XParser`), `generators/X.py` (`XGenerator`), `typing/X.py`
  (`EXPRESSION_METADATA` for annotate_types; only 14 dialects have one). `dialects/X.py` imports
  the other three.
- Loading: `sqlglot.dialects.__getattr__` (PEP 562) lazily `importlib.import_module`s
  `sqlglot.dialects.<name>` under an RLock; `_Dialect` metaclass registers every subclass in
  `_Dialect._classes` and `Dialect.get_or_raise("tsql")` triggers `_try_load`. The metaclass
  `__new__` does heavy **import-time** derivation: TIME/FORMAT tries, inverse mappings,
  `ESCAPED_SEQUENCES` from `Tokenizer.NUMERIC_ESCAPES`, strips unsupported JSONPath transforms from
  `generator_class.TRANSFORMS`, etc. So class-body edits act at import, not at a traceable line.
- Inheritance chains (edits propagate to subclasses): Hive→Spark2→Spark→Databricks;
  MySQL→Doris/StarRocks/SingleStore; Postgres→Redshift/Materialize/RisingWave; Presto→Trino→Athena;
  TSQL→Fabric. Parsers/generators mirror this (e.g. `SingleStoreParser(MySQLParser)`).
- Test files: 1020 of 1620 tests in `tests/dialects/test_X.py`, each importing its dialect via
  `Validator` (`tests/dialects/test_dialect.py`). Per test file, sqlglot modules loaded at import:
  12 files load only the 36 core modules, 25 load 59, max 79 (test_dialect / test_transpile style).
- `sqlglotc/` is a mypyc build shim; tests run pure-Python via `PYTHONPATH=<checkout>` (prints
  "Unable to set __version__", harmless). `sqlglot-integration-tests` is an un-fetched submodule.
- `tests/sqlglot/__init__.py` exists (shadowing guard for leakcheck) — not collected.

## 2. Fixtures → tests (why a fixture edit fails exactly one test number)

Loader (`tests/helpers.py`): `load_sql_fixture_pairs(file)` strips `--` comment lines, splits on
`;`, takes statements in (input, expected) pairs, and `#key: value` header lines become meta
(dialect, schema, execute, title...). `load_sql_fixtures(file)` yields one statement per line.

`TestOptimizer.check_file(name, func, ...)` loads `optimizer/<name>.sql`, submits every pair to the
spawn pool, then asserts each inside `self.subTest(title)`. **One fixture file = one test function;
each pair is a subtest**, and the README counts a test with subtests as one test. So any edited
expected/input line fails exactly that one test number. Edits that change `;` pairing shift all
later pairs but still fail the same single test.

Dynamic map (audit hook on `open`, per test; `fixlog2/`), 62 fixture files incl. csv.gz:

| fixture | test(s) |
|---|---|
| `optimizer/<f>.sql` (21 files) | `test_optimizer::test_<f>` (names differ: `pushdown_projections`→`test_pushdown_projection`, `annotate_functions`→`test_annotate_funcs`) |
| `optimizer/qualify_columns.sql` + `qualify_columns_ddl.sql` | both → `test_qualify_columns` (#1382) |
| `optimizer/qualify_columns__invalid.sql` | `test_qualify_columns__invalid` (uses `load_sql_fixtures`, each line must raise) |
| `optimizer/annotate_types.sql` | `test_annotate_types` (own loop, not check_file) |
| `optimizer/tpc-h/tpc-h.sql`, `tpc-ds/tpc-ds.sql` | `test_tpch` / `test_tpcds` **and** `TestExecutor.setUpClass` (used by test_optimized_tpch, test_execute_tpch, test_execute_tpcds) |
| `tpc-h/*.csv.gz`, `tpc-ds/*.csv.gz` | `TestExecutor.setUpClass` → whole class (a broken csv errors every executor test) |
| `identity.sql` | `test_transpile::test_identity` **and** `test_serde::test_serde` |
| `partial.sql`, `pretty.sql` | `test_transpile::test_partial`, `test_pretty` |
| `jsonpath/cts.json` | `test_jsonpath::test_cts_file` |

Caveat: setUpClass opens are attributed to whichever test ran first in the class on that xdist
worker (here test_agg_order, test_execute_tpch). My eval widens TestExecutor opens to the whole
class: tpc-h.sql → 61 tests, 31.9 s. A precise static rule: only tests referencing
`self.tpch_sqls` / `self.tpcds_sqls` (+ test_tpch/test_tpcds).
Fixture comment lines (`--`) are stripped before parsing → a pure `--` comment edit is harmless;
a `#` line at a statement start is meta (dialect/schema/execute), and changes behaviour.

## 3. Coverage-map build recipe (what worked)

```
uv venv -p 3.13 venv && uv pip install -r requirements.lock coverage pytest-cov   # coverage 7.17.4
cd sqlglot  # @eb11aec
PYTHONPATH=$PWD:<dir of covplugin.py> SKIP_INTEGRATION=0 \
  python -m pytest tests -p no:cacheprovider -p covplugin -n 4 \
  --cov=sqlglot --cov=tests --cov-context=test --cov-report=
python export_map.py .coverage $PWD covmap.json.gz
```
- `covplugin.py`: replaces `concurrent.futures.ProcessPoolExecutor` with an inline executor that
  pickle-round-trips initargs/args/results; optional `FIXLOG` audit hook logs fixture opens.
- Contexts are `nodeid|run` (setup contexts too when present); merge by nodeid.
- `--cov-context=test` works under xdist; contexts survive the combine.
- Not captured: `test_dialect.py::test_lazy_load` runs `python -c "import sqlglot; assert
  len(Dialect._classes)==1"` in a subprocess — any edit adding an eager dialect import breaks it with
  nothing in the map. `test_docs` runs doctests: docstring edits are invisible to line coverage.

Map stats: 53,027 covered sqlglot lines; median 4 tests/line, p90 137, max 1584 (core lines hit by
nearly every test). test_max_nodes (28.8 s) covers 867 lines: parser.py 512, tokenizer_core.py 210,
expressions/core.py 46, errors.py 38, tokens.py 38 — any edit there costs ≥30.8 s.
test_execute_tpcds (21.5 s) covers 7,149 lines (parser 1420, generator 643, simplify 526, core 448,
scope 392, qualify_columns 376...) — the dominant cost for optimizer/generator edits.

## 4. Selector logic (eval_v2.py)

1. Parse unified diff → old-side removed line numbers; pure insertions → neighbour lines.
2. Fixture path → dynamic fixture map (above).
3. Pure-comment insertion (all added lines `#...`, insertion point not inside a multi-line string
   token, no coding cookie) → select nothing.
4. Changed line → innermost AST statement; simple stmt → its full line span; compound stmt header
   (if/while/def...) → header lines up to body start. Union of tests covering any of those lines.
5. If the statement is not inside a function/lambda (module or class level):
   - in `dialects|parsers|generators|typing/<X>.py`: add family tests (any run-context coverage in
     the 4 `<X>.py` files). Mode E intersects with tests covering lines that mention the assigned
     UPPER_CASE name (WEEK_OFFSET → 15 tests instead of 165), falling back to family if empty.
   - elsewhere: tests covering lines that mention the enclosing class name (ktz3 TriggerProperties → 3).

## 5. Per-commit analysis (mode C = line map + stmt expansion; mode E = final)

| id | kind | fail | why it fails / doesn't | C sel (s) | E sel / cost | map predicted? |
|---|---|---|---|---|---|---|
| 2haz | code | 0 | `is`→`==` on Enum in Dialect.normalize_identifier: equivalent | 86 (30.8) | 86 / 25.5 | over-select (tpcds) |
| 2jrq | code | 8 | eliminate_joins `and not on`→`or`: drops joins → optimizer fixtures + executor results (1104, 1106 tpcds) | 29 (28.3) | 29 / 25.5 | yes, +21 extra |
| 2qn5 | code | 0 | ClickHouse `version < (23,12)`→`<=`: boundary untested | 6 | 6 / 2.5 | yes |
| 2vdj | fixture | 1 | pushdown_predicates.sql expected line → test_pushdown_predicates | 1 | 1 / 2.1 | fixture map |
| 3utz | code | 37 | serde.dump `and`→`or`: breaks Expr pickling → every spawn-pool test (optimizer fixtures, executor) + test_serde + dialect tests that dump | 37 (35.0) | 37 / 25.5 | yes only with pickle round-trip (naive: 24 missed) |
| 4d63 | code | 0 | Snowflake Tokenizer `NUMERIC_ESCAPES` "u" base 16→17; consumed by metaclass at import; no test notices | 0 | 346 / 9.5 | C correct by luck; E pays fallback |
| 535z | code | 1 | TSQL `WEEK_OFFSET -1→-2` read at runtime by simplify (tsql date-trunc in simplify.sql) → test_simplify | 4 | 15 / 3.4 | **no** (class attr); fallback needed |
| 5y72 | code | 0 | `copy=False`→`True`: equivalent | 3 | 3 / 2.5 | yes |
| 66ks | code | 0 | executor Context `is None`→`== None`: equivalent | 51 (22.8) | 51 / 24.8 | over-select (tpcds) |
| 6vvp | fixture | 1 | qualify_columns__invalid.sql line no longer raises | 1 | 1 / 2.0 | fixture map |
| 6ze8 | code | 1 | Dremio REGEXP_SPLIT lambda arg 0→1 → test_regexp_split (lambda body in class dict is run-context covered) | 5 | 5 / 2.0 | yes |
| 8y7j | code | 0 | ClickHouse UnixToTime MICROS `==`→`!=`: line covered by no test | 0 | 0 / 0 | yes (uncovered ⇒ safe) |
| 986c | multi | 1 | 2 comment lines (pure) + unnest_subqueries.sql → test_unnest_subqueries | 1 | 1 / 2.3 | yes |
| 9u5k, bap8, f4xa | comment | 0 | single `#` line inserted | 0 | 0 / 0 | yes (pure-comment rule) |
| ary6 | code | 10 | BigQuery parser `and`→`or` → bigquery tests + optimizer fixtures with `# dialect: bigquery` + test_ref_type | 10 | 10 / 4.4 | yes, exact |
| bj7g | code | 1 | ClickHouse `values_as_table` → test_clickhouse_values | 2 | 2 / 2.2 | yes |
| bvw2 | multi | 1 | comment + annotate_types.sql → test_annotate_types | 1 | 1 / 2.1 | yes |
| egys f4bn gbhk mthh nwnx st8f | fixture | 1 each | expected/input line of one optimizer fixture | 1 | 1 / 2.0–3.0 | fixture map |
| fryt | code | 0 | Dremio date-delta `==2`→`!=2`: covered by 3 tests, none fail | 3 | 3 / 2.2 | over-select (cheap) |
| gbav | code | 0 | SingleStore SHOW parser `target=False`, class-level dict | 4 | 5 / 2.4 | fallback (cheap) |
| j6uz | code | 2 | BigQuery `if not value`→`if value` → test_bigquery + test_annotate_funcs | 2 | 2 / 4.4 | yes, exact |
| ktz3 | code | 0 | `TriggerProperties.arg_types` constraint required: class body | 0 | 3 / 2.1 | fallback (cheap) |
| rszg | multi | 3 | scope.py comment (pure); typing/snowflake WITHIN GROUP `and`→`or` → NameError on `order_expr` for non-percentile → test_optimize, test_annotate_funcs; qualify_columns_ddl.sql → test_qualify_columns | 3 | 3 / 5.8 | yes only with stmt expansion |

Totals: mode C 454.2 (1 miss: 535z); mode E 166.7, 0 misses, 616 tests / 141.9 s selected.
Without the 4 tpcds-carrying commits, everything else costs ~66 s.

## 6. Hard cases for unseen commits (recall holes)

1. **Module-level edits outside a class and outside the dialect tree** (constant in `helper.py`,
   `optimizer/*.py`, `tokens.py` tables, `tests/helpers.py` `TPCH_SCHEMA`) select nothing in E.
   An import-time `NameError` fails *every test file importing the module* (import error = fail).
   Safe fallback: all tests whose file transitively imports the module (per-test-file
   `sys.modules` snapshot; for core modules that is everything ≈ the 1210 s starter, for lazy
   dialect modules far less). Better: detect name-level usage (grep the assigned name, take tests
   covering usage lines), as done for UPPER_CASE attrs.
2. **Class-body / metaclass-consumed settings** (`NUMERIC_ESCAPES`, `TIME_MAPPING`, `TRANSFORMS`,
   `FUNCTIONS`, `KEYWORDS`): only family fallback works; inheritance means subclasses must be
   included (Hive edit → Spark/Databricks tests). Family fallback currently ignores subclasses.
   The attr-name regex only matches UPPER_CASE; lowercase class attrs (`arg_types`, `is_var_len_args`)
   fall through to the class-name usage rule.
3. **`def` lines / default args / decorators** execute at import; covered only in "" context.
   Statement expansion maps a `def` header to the header lines, which carry no run contexts —
   treat def-line edits as "tests covering any line of the function body".
4. **Short-circuit and multi-line expressions**: handled by statement expansion; elif chains
   (`elif` is nested If) and comprehensions spanning lines are covered by the same rule.
5. **Comment edits that are not harmless**: `#` inside a multi-line string or docstring (doctests via
   test_docs; SQL inside `"""..."""` in tests). On Python 3.12+ tokenize emits FSTRING_* for f-strings,
   which my string-span check ignores — must add. Fixture `#` lines are meta directives.
6. **Executor tests**: skipped unless `SKIP_INTEGRATION=0` (labels used it). They run work in spawn
   pools → need the pickle-faithful inline plugin. test_execute_tpcds 21.5 s dominates cost of any
   edit to parser/generator/optimizer/executor/serde core.
7. **Import-time-only edits hit by every test** (`expressions/core.py` class Expr body,
   `parser.py` class tables): any real break fails ~everything; selection ≈ full suite.
8. **Subprocess tests** (`test_lazy_load`): eager-import edits invisible to coverage.
9. **setUpClass attribution** under xdist is per-worker-first-test; rebuild with `--dist loadfile`
   or widen class-setup coverage to the whole class.
10. **Test-file edits** (not in sample): map includes `--cov=tests` so test-source lines map to tests;
    edit to a `def test_*` body → that test; to helpers/imports → whole file(s).

## Artifacts

- `research/agent-reports/02-structure/covplugin.py` — inline pickle-faithful pool + fixture logger.
- `research/agent-reports/02-structure/export_map.py` — .coverage → gz JSON map.
- `research/agent-reports/02-structure/eval_cov.py` — raw line-map evaluation (modes A/B).
- `research/agent-reports/02-structure/eval_v2.py` — modes C/D/E:
  `python eval_v2.py covmap2.json.gz sample/ sqlglot/ fixlog2/`.
- Scratch (not in repo): `~/rwx-research/covmap2.json.gz` (0.68 MB), `cov_v2.sqlite` (7.4 MB),
  `fixlog2/` (dynamic fixture map), `cov_v1.sqlite` (naive build, for comparison).
