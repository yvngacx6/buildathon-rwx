# 05 — Robustness, generalization and rule compliance

Agent 05. Evidence comes from experiments against sqlglot @ eb11aec, cloned at `~/rwx-research-robust/sqlglot`. Scripts are in `research/agent-reports/05-robustness/`.

## Executive summary

1. **The sample patches are synthetic mutants, not real commits.** All 30 single-file patches are one hunk of exactly 13 lines that swaps one token: `and`/`or`, `==`/`!=`, `is`/`==`, `<`/`<=`, an int ±1, `True`/`False`, `seq_get(args,0→1)` or `copy=False→True`. Comment patches insert one of a few template comments: "Cheap check first", "Kept for backwards compatibility", "Keep in sync with the generator", "Intentionally not cached" and "Mirrors the behavior of the reference implementation". Fixture patches corrupt one token in a `tests/fixtures/optimizer/*.sql` line, as in `xa .b`, `y42 `, `x + 42 3` and `STRUCTINT>`. "multi" is two or three of these stacked: comment + code + fixture. None of the inserted strings appears anywhere in sqlglot's git history (`git log --all -S`), and the 65 upstream commits after eb11aec are ordinary feature or fix PRs. The hidden set is almost certainly the same generator, plus new operator families or file targets.
2. **A naive per-test coverage map misses 3 of 30 sample commits (2jrq, 3utz, 535z), plus the fixture and comment cases.** There are four distinct causes, each measured:
   - **(a) Work runs in subprocesses.** `TestOptimizer` and `TestExecutor` do their work in a `ProcessPoolExecutor(spawn)` subprocess, so coverage never attributes `test_optimize`, `test_tpcds`, `test_tpch` and others to the optimizer code they exercise. This is the biggest blind spot.
   - **(b) The pickle path is hidden.** The pool pickles Expressions through `serde.py`. 3utz (in serde) fails 37 tests, and only 13 are visible unless the map build keeps the pickle round-trip.
   - **(c) Class-body and module-level lines are attributed to the wrong test.** Their coverage goes to the `""` context or to whichever test first lazily imported the dialect. For 535z (`TSQL.WEEK_OFFSET = -1 → -2`), `test_simplify` fails via `tsql` in a fixture without executing any line of `tsql.py`.
   - **(d) Multi-line conditions hide short-circuited lines.** Under short-circuit evaluation the mutated continuation line is never executed. 2jrq at line level selects 5 of 8 failing tests.
3. **A conservative prototype gets 0 misses on all 30 sample commits.** Rules:
   - AST-identical → nothing.
   - Function-level coverage, with a build-time inline pool that keeps pickling.
   - "Definition-time" lines (module or class body) → every test that instantiates a Dialect, Parser, Generator or Tokenizer from that module (MRO included), plus the file's coverage.
   - Fixtures → a per-test `open()` read map.
   - Unknown, syntax error or apply failure → ALL.

   On the 30 sample commits it selects about 398 s of tests in total and misses none. On 30 extra private synthetic patches, including new kinds of change (syntax error, deleted module, test-helper edits, test edit, executor, tokenizer, base-class attribute, docstring, whitespace, pyproject, new file), it misses none only after one fix tuned on that same set: class-level use for Expression subclasses (§5). On an untuned held-out batch of 11 patches (seed 8, mostly class-body table edits) it also misses none, with nothing changed after seeing the labels.
4. **Running ALL tests costs only about 37 s per commit** (4 workers; `test_max_nodes` alone is 28.8 s). A miss costs 300 s. **Fall back to ALL whenever P(miss) > ~10%.** A safety net is cheap; a clever-but-wrong selector is not.
5. **Mutation augmentation and the rules: build no map from mutation outcomes.**
   - **Not compliant:** shipping any map derived from which tests kill which mutants. That covers (file, line, operator) → tests and also line → "tests that killed mutants here". The hidden commits come from the same mutation family, so that table is an answer key "from a kind of edit to the tests it breaks".
   - **Compliant:** mutants generated and labeled locally, used only as a private validation set to measure the recall of maps built from the pinned commit (coverage, use, read, import) and to tune generic rules such as "class body → module users". No mutant outcome is encoded in the image.
   - Even if it were allowed, it would not fit the build: about 100–300 mutants in 10 min × 4 cores, against roughly 5k functions.

## 1. Change kinds beyond the sample, and the safe response for each

"Map" means the function-level coverage map, the use map and the read map, all from the pinned commit. "ALL" costs about 37 s.

| Kind | What a coverage selector gets wrong | Safe handling |
|---|---|---|
| Comment, blank line, whitespace (.py) | Selects covered tests needlessly | Parse before and after; if `ast.dump` is equal → `[]`. Verified on 9u5k, bap8 and f4xa, and on the comment hunks in 986c, bvw2 and rszg. A `#` inside a string literal changes the AST, so it is never wrongly skipped. |
| Docstring-only | The AST differs, so the function's tests get selected | Harmless for doctests: `tests/test_docs.py` uses `load_tests`, which pytest ignores, and no test_docs item is in tests.json. A docstring diff can be treated as a no-op if the rest of the AST is equal. Medium confidence: check that no test reads `__doc__`. |
| Operator or constant inside a function | Line level misses short-circuited continuation lines (2jrq) | Function level: every test that executed any line of the innermost enclosing function. |
| Class-body or module-level constant, dict entry, `arg_types` | Line coverage attributes it to `""` or the first importer, so 535z is missed | Definition-time rule: the use map (instantiations of classes defined in the module, MRO, so subclasses such as MySQL → Doris and StarRocks are included) ∪ the file's coverage. Base `Dialect`, `Parser`, `Generator`, `Tokenizer` or `Expression` class bodies → nearly ALL. |
| Syntax error or import-time exception | Map says few tests; actually every test whose file transitively imports the module fails (the error is at collection) | Compile the patched file. On SyntaxError → ALL. Better: at run time, import the patched module in /tmp (≈0.3–1 s CPU) to catch NameErrors or ImportErrors at import time too. |
| New import, removed import | Import error → collection error, as above | Same import probe. A removed import used in a function → function-level rule. |
| `sqlglot/__init__.py`, `tokens.py`, `parser.py`, `generator.py`, `expressions/core` | Huge blast radius; maps are correct but large | Just take the map (it will be near ALL), or go straight to ALL above a size threshold. |
| `sqlglot/executor/*` | Executor tests are skipped unless SKIP_INTEGRATION=0 (set when labeling); TestExecutor uses a process pool | Build the map with `SKIP_INTEGRATION=0` and the inline pool (see §2). |
| Test-file edit (`tests/test_x.py`) | Coverage maps say nothing about test code | Tests in that file whose function body changed; simplest is the whole file. A `setUpClass` or module-level change → whole file. |
| Test helper (`tests/helpers.py`, `tests/dialects/test_dialect.py` `Validator`, `tests/__init__.py`, `leakcheck.py`) | Not in the coverage source; `Validator` is imported by about 60 dialect test files (~1020 tests) | A test-side import graph: every test file that imports the module → those files' tests. |
| Fixture `.sql` (`tests/fixtures/**`) | Coverage does not track `open()` | Read map (files `open()`ed per test). All 8 sample fixture commits fail exactly the one `test_optimizer` test that reads that file. Caveat: reads in `setUpClass` (e.g. tpc-ds data for TestExecutor) are attributed to the first test of the class, so widen to the class only for files read in setup. |
| Fixture read by nobody | — | `[]` |
| New file (`.py` added, unused) | Nothing imports it | `[]`. Exception: `tests/test_*.py` adds new tests that are not numbered, so it is irrelevant. A new `sqlglot/dialects/x.py` can matter through `pkgutil` walks (test_docs is not run) → module-users rule, which gives nothing. |
| Deleted module | Import errors in every importer | Import probe or test-side import graph → ALL importers. A deleted dialect breaks its own test file plus dialect-registry tests. |
| Renamed or deleted test | The ID disappears and the old number "fails" (cannot be collected) | Select every test whose function was removed or renamed. It is cheap. |
| `pyproject.toml`, `setup.py`, `setup.cfg`, `Makefile`, `*.md` | No pytest config lives there (no `[tool.pytest]`, no `filterwarnings`), so these are harmless | `[]` for docs and Makefile. For `pyproject.toml`/`setup.cfg`, `[]` unless a `[tool.pytest*]`/`[pytest]` section is touched → ALL. `sqlglotc/*` → `tests/test_sqlglotc.py` (3 tests; it reads `sqlglotc/setup.py`). |
| Binary or unknown non-.py file | Unknown | Read map if some test opened it; otherwise ALL if under `tests/`, `[]` if under `docs/`, `posts/` or `benchmarks/`. |
| Patch that fails to apply or parse | — | ALL |

## 2. How the map must be built: the pitfalls that matter most

- **Inline the process pool during the map build.** Use a pytest plugin that replaces `concurrent.futures.ProcessPoolExecutor` with a synchronous executor. It must **pickle-round-trip args, results and initargs** so `serde`/`__reduce__` stays on the path (`05-robustness/syncpool_plugin.py`). Without the round-trip, 3utz is missed (13 of 37 failing tests visible); with it, 37 of 37.
- **Use `dynamic_context = test_function`**, sharded per test file with `--data-file` and combined (`covrun.sh`, `buildmap.py`). Runtime is about 1.5 min wall at 16 jobs locally plus about 1 min for the optimizer and executor files inline. It fits a 4-core, 10-minute build, but building offline and committing the map is simpler and deterministic. Compact function → test-number map: 4,985 functions, 1.8 MB raw JSON, 181 KB gzipped.
- **The use map** (`usemap_plugin.py`) wraps `__init__` of Dialect, Parser, Generator and Tokenizer and records the modules in the instance's MRO per test. It catches 535z, which coverage alone cannot.
- **The read map** (`readmap_plugin.py`) wraps `builtins.open` per test. It is exact for fixture files read inside test bodies.
- Five tests have no coverage context: `test_lazy_load`, `test_extract_meta_schema_directive` and the three `test_sqlglotc` tests. Handle them with the test-side and import rules, not the map.
- Locally, 1621 tests pass against the 1620 numbered. `test_variadic_iif_execution` is in tests.json's `skipped` (SQLite version), so ignore it.

## 3. Rule compliance: where the line is

Rule text: *"No answers worked out ahead of time and looked up by patch: no table from a patch, or from a kind of edit, to the tests it breaks. Maps built from the pinned commit (coverage, imports, fixtures) are allowed. Top entries are read."*

| Approach | Verdict | Why |
|---|---|---|
| Coverage, import-graph, use and read maps from running the **unmodified** pinned suite | Allowed | Explicitly named |
| Generic rules hand-written from reading sample patches ("AST-equal → nothing", "class body → module users", "syntax error → ALL") | Allowed | Program logic about code structure, not a table to failing tests. Write these as code with comments, not as data. |
| Mutants generated locally, labeled with the full suite, used to **measure** recall and tune rules / thresholds | Allowed, with care | Generating your own test data is normal engineering. Keep it out of the image and say so in the README. |
| Running sqlglot tests or import probes **during the run** on the actual patch | Allowed | Explicitly allowed; CPU is charged |
| Map keyed by (file, line or function, operator) → tests that killed the mutant | **Banned** | "From a kind of edit to the tests it breaks" |
| Map keyed by line or function → "tests that killed any mutant here", shipped in the image | **Banned in practice** | It is a kill matrix keyed by location. Against a mutant-generated hidden set it is a precomputed answer key, and a judge reading the repo will see `mutmut`/`cosmic-ray` output feeding a lookup. |
| ML model trained on mutant→failing pairs | **Banned** (same spirit) | A compressed table from kinds of edits to tests |
| Organizer-like patches in the repo (`val/*.patch`) with labels | Keep out of the shipped image; local only, or clearly marked as test data in a non-copied folder | Avoids any appearance of lookup |

Recommended README wording for judges:

> "All selection data is derived from the unmodified pinned sqlglot commit: per-test function coverage, per-test dialect/parser/generator usage, per-test file reads, and the test-side import graph. The selector applies generic rules to the diff (AST-equivalence, enclosing-function lookup, definition-time scope, import probe). We evaluated it on locally generated synthetic patches; no outcome of those patches is stored in or read by the submission."

Also, a dissenting note: **even "tuning thresholds on our own mutant set" carries a little risk** if the thresholds are many and per-file. Keep the tuned knobs few and global (e.g. "ALL if more than X% of tests are already selected").

## 4. Safety-net design

The cost asymmetry is a miss at 300 s against ALL at about 37 s and a typical good selection at 2–10 s. Choose ALL when `P(miss | rule) × 300 > 37 − cost(selection)`, i.e. when P(miss) is above roughly 10%. Tiers, by confidence:

1. **Exact no-op** (AST-equal, docs, Makefile, unread fixture) → `[]`. Highest confidence.
2. **Function-level map** for a change inside a function body whose coverage contexts exist → selected tests. Add the whole test class for any test whose class has `setUpClass` side effects.
3. **Definition-time** (class or module body) → use map ∪ file coverage. If the module is a base (`dialect.py`, `parser.py`, `generator.py`, `tokens.py`, `expressions/core*`, `helper.py`, `__init__.py`), go straight to ALL.
4. **Unknown, odd or failed analysis** (apply failure, SyntaxError, file absent from all maps, deleted module, pytest config, conftest, unknown non-.py under `tests/`) → ALL.
5. **Optional run-time probe:** copy only the touched module(s) into a `/tmp` overlay (the sqlglot package is 8 MB and `/tmp` is 64 MB) and `python -c "import sqlglot.<mod>"`. An import failure → ALL importers. Cost is about 0.5 s CPU per commit, which is cheap relative to 300 s.

**Verifying on held-out data without breaking the rules:**
- Use `gen_validation.py` + `label.sh` + `eval_val.py`.
- Generate patches with the same operator families plus new kinds, label each one with the full suite (≈1–2 min each at `-n 6`), and report misses and selected seconds.
- Re-seed and regenerate before the final so tuning cannot overfit a fixed set.
- Score with a script that simulates the cost formula; reuse `score.py` by writing an answers.json for the private set, and keep it outside the image.

## 5. Private validation results (30 synthetic patches, labeled with the full suite)

There are 30 patches, all labeled by running the full suite at the pinned commit (`labels/*.txt`). 17 are operator mutants in the same style as the sample, over random modules: one each in `executor/python.py`, `tokens.py` and a `Dialect` class body, plus 14 others. The other 13 are new kinds of change.

**Result: 0/30 misses, but only after one fix, so this is a tuning-set number, not a held-out one.** The first run missed 2/30:
- `op01`: `JSONArrayAppend.is_var_len_args`
- `op09`: `TriggerProperties.arg_types`

Both were **Expression-subclass class-body** edits. Module-level use is useless here because every test uses the `expressions/*` modules, and coverage never runs those lines. The fix: the use map also records `module:Class` for every Expression instantiated per test. Definition-time lines in `sqlglot/expressions/*` then select the users of that class plus the file's coverage. Wrapper pitfall: an `__init__` wrapper put on `Expression` shadows `TimeUnit.__init__` later in the MRO (`Week → Expression → Func → … TimeUnit`) and broke 364 tests. Defer with `super(cls, self).__init__` when the class does not own `__init__`.

| patch | failing | selected (tests / s) | note |
|---|---|---|---|
| syntaxerr (duckdb.py class line) | 1111 | ALL | Collection errors in every importer, so ALL is right |
| init_import (bogus import in `sqlglot/__init__`) | 1617 | ALL | Only 3 tests survive |
| helper_validator (`Validator.validate_identity` default) | 451 | 1020 / 10.5 | Test-side import graph |
| helper_fixtures (`tests/helpers.py`) | 22 | 470 / 91 | Importers of helpers |
| testedit (assert added to `test_union`) | 1 | 81 / 29 | Whole file; function-level would be 1 |
| delete_dialect (druid.py deleted) | 7 | 7 / 6.8 | Exact |
| baseclass_attr (new MySQL class attr) | 9 | 309 / 20 | Subclasses come in through the MRO |
| fixture_identity (identity.sql) | 2 | 33 / 1.9 | Class-widening of read map; exact would be 2 |
| exec1 (executor) | 14 | 24 / 22 | Needs the inline-pool map |
| docstring | 0 | 121 / 34 | Waste: treat docstring-only as a no-op |
| whitespace | 0 | 0 | AST-identical |
| pyproject, newfile | 0 | ALL / 103 | Waste: prototype bugs (new file → treat as no-op; pyproject without a pytest section → no-op) |
| tok1 (`tokens.py` module constant), cls1/op03 (`Dialect` class body) | 0 / 2 / 11 | ~1580 / ~102 | Correct but costly; ALL with 4 workers (≈37 s) is cheaper than this selection packed badly |
| other 13 operator mutants | 0–14 | 1–1030 | All covered |

Totals for the prototype:
- Sample: 0/30 missed, 398 s of selected test time in total.
- Validation: 0/30 missed.

The validation set is small, so read "0 misses" as an indication that no structural blind spot remains, not as proof. The remaining risk is mainly class-body edits in non-Expression helper classes, and indirect data flows such as dict tables read by other modules (e.g. `typing/*.py` EXPRESSION_METADATA, which `open()` maps show being read at import time). For those, keep the safety rule "definition-time line outside a function in a module imported by many tests → module users ∪ file coverage, and ALL if that exceeds ~50% of tests".

**Untuned held-out set** (`gen_heldout.py`, seed 8, 11 patches biased to `arg_types`, class attributes, typing tables and parser/generator dicts). All 11 are now labeled with the full suite: **0 misses**. Only 2 of the 11 had failing tests (h_expr4 fails 2, h_ptab1 fails 4), and both were caught, with 233 and 12 tests selected. The other 9 fail nothing, so they show cost, not recall. h_ptab0 and h_ptab2 were never generated because no line matched the filter. `expressions/core.py` class bodies select about 1545 tests, so send those straight to ALL.

**Leaderboard-comparable score on the sample.** Selections packed by LPT with the best worker count ≤8 (`pack_score.py`), then scored with `score.py`:

| Fixture handling | Simulated | Misses |
|---|---|---|
| Fixture class-widening on | 309.2 s | 0 |
| Exact read map, no widening (`WIDEN_FIXTURE_CLASS=0`) | **198.6 s** | 0 |

Practice best is about 169 s. Validation stays at 0 misses without widening: fixture_identity becomes exact, 2 of 2. Widen only for files read in `setUpClass`. Base-class bodies selecting ~1580 tests cost about the same as ALL after packing, so sending them straight to ALL loses nothing.

**Implementation constraints for the real selector:**
- The run image `python:3.13-slim` has **no `git` and no `patch`** (checked on the EC2 box), so diffs must be applied in pure Python. A hunk that fails to apply → ALL.
- The prototype's `newfile` and `pyproject` → ALL results are prototype bugs (`cp` on a `/dev/null` old side; no special case for config files). Follow the §1 table, not the prototype.
- For the optional import probe: `/tmp` is 64 MB and the disk is read-only. The 8.1 MB package fits once per commit, so clean it up between commits.
- The `typing/*.py` entries in the read map are unexplained. They may be import-time source reads, but that was not established.

## 6. Files

- `05-robustness/covrun.sh`, `buildmap.py`: per-test-file coverage with test contexts → line map
- `05-robustness/syncpool_plugin.py`: inline pool with pickle round-trip (build-time only)
- `05-robustness/usemap_plugin.py`, `readmap_plugin.py`: per-test module-use and file-read maps
- `05-robustness/proto_select.py`, `eval_proto.py`: conservative prototype and sample evaluation (0/30 misses)
- `05-robustness/gen_heldout.py`, `pack_score.py`: held-out generator; LPT packing plus score.py input
- `05-robustness/gen_validation.py`, `label.sh`, `eval_val.py`: private validation set generation, labeling and evaluation
- Data (not in repo): `~/rwx-research-robust/{linemap,usemap,readmap}.json`, `val/`, `labels/`
