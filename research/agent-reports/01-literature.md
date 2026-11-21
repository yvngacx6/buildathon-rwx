# 01 — Literature and industry practice on RTS, applied to sqlglot Blast Radius

## Executive summary (10 lines)
1. Cost asymmetry decides everything. One miss costs 300 s. Packed well, the full suite costs only about 37 s per commit: test_max_nodes 28.8 s alone, tpcds 21.5 s alone, the other ~53 s on 2 workers, plus 4 × 2 s. So **recall comes first**.
2. The other ~1617 tests sum to about 46 s, so recall on cheap tests is almost free. Precision work pays only where it keeps **test_max_nodes (28.8 s), test_execute_tpcds (21.5 s) and test_generator_memory (6.7 s)** out of a selection.
3. RTS selects tests that are *affected*, not tests that *fail*. 12 of the 30 sample commits fail nothing. 2haz changes `is` to `==` on an enum, an equivalent mutant that any coverage-based RTS still flags. Only equivalence proofs (AST equality) or running the tests at runtime get such commits to `[]`.
4. The best fit from the literature is **dynamic, function-level RTS** of the pytest-testmon / Ekstazi kind. Build a per-test map of which Python functions (code objects) each test runs, at build time on the pinned commit. This is explicitly allowed ("coverage maps").
5. Add **per-test file-read tracing**, in the spirit of RTSLinux/Ekstazi tracking external files. Use a `sys.addaudithook` on `open`. Do not match fixtures to tests by name: nwnx edits `pushdown_projections.sql` but `test_pushdown_projection` fails, and rszg edits `qualify_columns_ddl.sql` but `test_qualify_columns` fails.
6. **AST-equivalence filter** (Ekstazi's "smart checksum" idea): if `ast.dump` of the old and new `.py` file is identical, nothing can change. That clears the 3 sample comment commits. `#` lines in `.sql` fixtures are **metadata** (`_extract_meta`: execute, schema, dialect), so they are not comments. Only lines starting with `--` are filtered out.
7. Coverage under-reports **import-time code**: class bodies (TRANSFORMS, FUNCTIONS, KEYWORDS dicts), metaclass work, and module constants. sqlglot loads dialects lazily (`sqlglot/dialects/__init__.py` `__getattr__` + importlib), so import-time lines get charged to whichever test imported the dialect first, or to the empty context. Static import graphs (STARTS, pytest-incremental) also miss string-based dialect loading.
8. Industry predictive selection (Facebook PTS, Launchable, Google TAP heuristics) learns from patch→failure history. Here that history is the **banned table**, and there are only 30 samples, so use those for validation only. A build-time mutation kill matrix is essentially an "edit-kind → failing tests" table: high compliance risk. The sample patches are literally textbook mutation operators (and→or, ==, index 0→1, `not` removal).
9. **Scheduling:** try every k from 1 to 8 and minimise max-load + 2k, using LPT (a 4/3-approximation) or exact packing for small sets. Usually k = 1–2. Always isolate the 28.8 s and 21.5 s tests.
10. For unseen kinds of change, use conservative fallbacks: edits to test files, conftest/helpers, new or deleted files, non-`.py`/`.sql` files, packaging, or AST parse failures run the full suite with optimal packing (about 37 s, still far below 300 s).

## Problem framing in RTS terms
- The literature calls a selector **safe** when it selects every test that can change outcome ("safe RTS", Rothermel & Harrold 1997). The scorer charges a miss on *failing* tests only, and the selector is charged for everything it selects. So we want *safe for failing tests* and *precise on expensive tests*.
- Expected-cost rule: include test t when P(t fails | patch) × 300 > marginal makespan cost of t. That cost is about seconds(t)/k for cheap tests, and the full seconds(t) (plus maybe a worker) for the three slow ones. A 0.05 s test should be included if P > ~0.0002, so **always include the whole test file/class around any hit**. test_max_nodes only needs P > ~10%.
- Sample facts that matter: code 16, fixture 8, multi 3, comment 3. All 8 fixture commits fail exactly 1 optimizer test. 3utz (serde.py) fails 37 tests across many dialect files, a cross-cutting utility. 2jrq (eliminate_joins.py) fails test_execute_tpcds, so the slow tests do sometimes fail.

## Ranked candidate methods

### 1. Dynamic function-level RTS (testmon/Ekstazi-style), built at image build time — RECOMMENDED CORE
- **How it works:** run the full suite once at build (4 cores, xdist). For each test, record the set of code objects (file, qualname, first line) it executes. Python 3.13 option: `sys.monitoring` PY_START callback, return `DISABLE` after the first hit, call `sys.monitoring.restart_events()` at each test start (in a pytest `runtest_setup` hook). The overhead is near zero, much cheaper than line tracing. Alternative: coverage.py with `--cov-context=test` (pytest-cov) and `dynamic_context=test_function`, then map lines to the enclosing def yourself. Check coverage's changelog for whether contexts work with the `sysmon` core before relying on that combination. Ekstazi works at class/file level, testmon at block level with checksums. Function level is the right middle ground here.
- **Diff mapping:** parse the **old** file with `ast` and key every lookup on old-side line numbers, so inserted lines cannot shift anything. Map each changed or deleted old line, and each pure insertion point, to the **innermost enclosing `def`/lambda**. Then select every test that ran that function.
  - Continuation lines: 2jrq changes line 2 of a multi-line `and` chain. coverage.py only records a statement's first line, so line-level lookup can miss it. Def-level lookup cannot.
  - Insertions: use the def containing the insertion point.
- **Catches:** 2jrq, 535z, 6ze8, j6uz, bj7g, ary6 and other in-function logic changes, including via dynamic dispatch (`Generator.sql` → `getattr(self, f"{name}_sql")`) and inheritance overrides. The real call happens at runtime, so dispatch is resolved for free. This is the big advantage of dynamic over static RTS in a metaclass/registry-heavy codebase (Legunsen et al. 2016; reflection is the main way static RTS goes unsafe).
- **Misses / pitfalls:**
  - (a) **Import-time code** (class bodies, decorators, default args, module constants, metaclass `__new__`/`__init__` of `_Dialect`, `_Expression` registration). Fallback: a change at module or class-body level selects every test that executes *any* function of that class (and its subclasses), or of that module. For dialect modules, also select the whole `tests/dialects/test_<name>.py` file.
  - (b) **Caches/memoisation.** Dialect instances, tokenizer tries and settings are built once, so later tests never re-execute the builder. Because PY_START is tracked per test with `restart_events`, a function run only by the first test is attributed only to that test. Mitigate by widening selection to the test *file/class* of every hit (cheap), and by also recording which tests touch the class at all.
  - (c) **Reflection over registries:** `helper.subclasses` uses `inspect.getmembers`, `_Expression` registers subclasses, and `expressions/core.py` sets `__doc__`. Adding or removing a class can affect tests that iterate all classes without running the new code. Treat class add/remove as module-level (fallback a).
  - (d) **Data flow without control flow:** a changed constant or dict entry read at runtime by another function is not "executed" by the reader. Module-level fallback covers it.
  - (e) Unexecuted branches: a changed `else` that no test runs cannot fail any test (testmon's assumption). That is safe for *failing* tests.
- **Expected:** recall ~99%+ on code commits with the fallbacks. Precision is moderate: core functions like `Generator.sql` and `Parser._parse_*` are run by hundreds of tests. For sample code commits, selected sets are mostly the dialect file(s) plus optimizer tests.
- **Biggest precision risk:** test_max_nodes and tpcds execute huge swaths of the parser/optimizer, so they get selected often (fine for recall, costs ~30 s). Refinements: (i) function-level instead of file-level keeps tpcds out of tokenizer/generator-only edits; (ii) runtime execution (method 5) of just the slow test when it is the only expensive candidate.
- **Build cost:** one full run is ~103 s serial; with tracing and 4 workers, under 2 min. The map is a few MB (1620 tests × a few hundred code objects; store as bitsets per function). **Compliance:** clean, it is a coverage map from the pinned commit.

### 2. Per-test file-dependency tracing (fixtures / external files) — REQUIRED COMPANION
- **How it works:** `sys.addaudithook` for the `open` event (Python ≥3.8) records `(current test, path)` during the build run. RTSLinux (Celik et al. FSE'17) and Ekstazi track non-code file dependencies the same way. Attribute reads in `setUpClass`/module level to the whole class or file.
- **sqlglot detail:** `tests/helpers.py` `load_sql_fixture_pairs` splits on `;`, drops lines starting with `--`, and reads leading `#` lines as **metadata** (`execute`, `schema`, `dialect`). A `#` edit in a fixture therefore changes semantics. A `--` full-line edit is a no-op, *unless* it removes a `;` or the line does not start with `--`.
- **Better than file-level:** sub-file precision is possible. If a fixture is consumed by several tests, still select all of them (cheap, all ms-level except tpcds, which reads tpc-ds files).
- **Catches:** all 8 sample fixture commits plus the fixture part of the multi commits (986c, bvw2, rszg). Name matching would miss nwnx and rszg.

### 3. AST-equivalence ("smart checksum") filter — HIGH VALUE, CHEAP
- **How it works:** Ekstazi hashes class files with debug info stripped, so comment and line-number changes don't count. In Python, compare `ast.dump(ast.parse(old))` with `ast.dump(ast.parse(new))`, which omits line attributes by default. Do it per function to drop whole hunks that are no-ops.
- **Catches:** 9u5k, bap8 and f4xa (comment-only) → `[]`. Also catches the comment parts of multi commits (986c adds `# Intentionally not cached` at line 1 of singlestore.py).
- **Residual risks:**
  - A `# -*- coding:` cookie on lines 1–2 changes decoding. Check for it explicitly.
  - `# type:` and `# noqa` comments have no runtime effect.
  - Source introspection: I found no `getsource`/`co_firstlineno` use in tests at this SHA. `inspect.getfullargspec` (optimizer.py) is AST-visible.
  - Tests asserting line numbers in tracebacks or warnings: not seen, but keep in mind.
  - Docstring changes *are* AST changes and can matter (`__doc__` is used).
  - A "comment" that comments out code is a real AST change, so it is correctly not filtered.
- **Equivalent mutants (2haz):** `is` → `==` on an Enum differs in AST, so it is not filtered. That is the RTS ceiling; only runtime execution resolves it.

### 4. Optimal makespan packing — REQUIRED, trivial
- **How it works:** cost = max load + 2k. For each k in 1..8, run LPT (Graham, 4/3 − 1/(3k) bound), or exact DP/branch-and-bound when there are fewer than ~40 tests. Lower bound: max(longest test, sum/k). Pick the k that minimises the cost.
- **Practical results:** ≤ ~4 s of cheap tests gives k = 1. With test_max_nodes, use k = 2 (it alone, the rest on the other worker) unless the rest exceeds 28.8 s. Full-suite fallback: k ≈ 4, ~37 s.
- **Pitfall:** recorded seconds are what count, not real runtime. Use `tests.json` values exactly (ms rounding).

### 5. Runtime verification (apply the patch and run the candidates) — OPTIONAL PRECISION BOOST
- **How it works:** allowed by the rules, with CPU charged. Copy or overlay only the changed sqlglot files into /tmp (64 MB; sqlglot/ is 3.5 MB, tests/ 8.8 MB, so overlay just the patched files with a path shim). Run the candidate tests and emit only the failures, plus a safety margin.
- **Value:** turns "affected but passing" commits (12/30 fail nothing, including 2haz and 4d63) into `[]`, saving ~2–4 s each. It also lets you drop test_max_nodes (28.8 s of simulated cost) at the price of ~28.8 s of CPU, which is break-even, so do it only for cheap candidate sets.
- **Costs and risks:**
  - pytest startup plus sqlglot import is ~1–2 s CPU per invocation. Batch everything into one process per commit, or one long-lived process that reloads modules.
  - 2 cores, 3-minute wall budget.
  - Environment must match labeling: `SKIP_INTEGRATION=0`, `requirements.lock`, timeouts.
  - Flakiness or timeouts in your own environment would yield false "passes" and 300 s misses.
  - Never drop a test because it *errored in your harness*.

### 6. Static RTS (STARTS-style class/module dependency graph; pytest-incremental import graph) — FALLBACK ONLY
- **How it works:** STARTS builds a type-dependency graph statically. pytest-incremental builds a module import DAG and reruns tests whose transitive imports changed.
- **On sqlglot:** almost every test imports `sqlglot`, which imports the core, so module-level static RTS selects ~everything for core changes. It also **misses** dialect modules loaded via `__getattr__` and strings (`Dialect.get_or_raise("dremio")`), and dispatch via `getattr(self, f"{name}_sql")`.
- **Useful bits:**
  - A cheap static rule: a change in `sqlglot/{dialects,parsers,generators,tokenizers,typing}/<name>.py` → `tests/dialects/test_<name>.py` plus dialects that inherit from it (Redshift → Postgres, Databricks → Spark → Hive, Materialize/RisingWave → Postgres, etc.). This is a static map from the pinned commit and is allowed. Use it as a *union* with method 1, never alone.
  - Literature: STARTS and Ekstazi show similar safety violations, and STARTS selects more tests (ASE'22 comparison).

### 7. Information-retrieval / lexical test impact (REPiR, Saha et al. ICSE'15) — SUPPLEMENT
- **How it works:** match identifiers in the diff (e.g. `jsonextractscalar_sql` → `JSON_EXTRACT_SCALAR`, `DremioParser` → `dremio`) against test names and SQL strings in tests.
- **Role:** a cheap recall safety net for import-time and registry changes (method 1's weakness (a)). Precision is low. It is allowed because it is built from the pinned repo, not from answers.

### 8. Predictive / ML test selection (Facebook PTS, Launchable, Google TAP) — NOT APPLICABLE AS DESIGNED
- **How it works:** Facebook (Machalica et al. ICSE-SEIP'19) trains gradient-boosted trees on change × test features (file extensions, distance in the dependency graph, historical failure rates) from **history**. It reports catching >99.9% of faulty changes (≥1 failing test) and ~95% of individual failures, while running about 1/3 of the transitively dependent tests. Launchable is similar (vendor claims "80–90% of value with 10–20% of tests"). Google TAP (Memon et al. '17) found failures cluster in tests "close" to the changed code in the build graph, and that distant targets rarely fail.
- **Why not here:**
  - Training on patch→failure pairs is exactly the banned table.
  - 30 samples is far too few.
  - Note: 95% per-failure recall would be catastrophic under a 300 s miss penalty.
- **Transferable idea:** *dependency distance* as a feature. Use it only to decide about the 3 expensive tests, never to drop cheap ones.

### 9. Mutation-based test impact analysis (build-time kill matrix) — HIGH COMPLIANCE RISK
- **How it works:** at build time, mutate each function or line (operators like and↔or, ==/is, constant ±1, negate condition) and run its covering tests. Record which tests kill each mutant, then predict failures for a patch by locating its mutant site. The literature uses mutants mostly to *evaluate* RTS and test-suite reduction (Shi et al. ISSTA'18), and Predictive Mutation Testing (Zhang et al. ISSTA'16) predicts kills from features.
- **Why risky:** the sample patches *are* such mutants. A kill matrix is in effect "edit-kind at location → failing tests", which judges reading the top entries may treat as the banned table.
- **Other problems:**
  - Cost: thousands of sites × covering tests won't fit in 10 min.
  - It is also unsafe: a different mutation at the same site kills different tests.
- **Acceptable use:** at most, offline local *evaluation* of your selector on self-made mutants. Ship nothing derived from it.

### 10. Test-suite minimisation (greedy set cover, HGS) — NOT USEFUL
- **How it works:** minimisation removes tests that are redundant by coverage (Yoo & Harman survey). Redundant coverage ≠ redundant fault detection: each sqlglot test asserts different SQL strings, so dropping "redundant" tests causes misses.
- **Role:** irrelevant beyond packing (method 4).

## Fallback table for unseen kinds of change (judged by final)
| Patch touches | Action |
| --- | --- |
| `sqlglot/**/*.py`, inside a def, AST differs | Method 1 hits ∪ file/class widening ∪ static dialect map (method 6) |
| `sqlglot/**/*.py`, module/class level, or a class added/removed | Every test running any function of that module/class hierarchy ∪ dialect test file(s); if the module is core (`expressions/`, `parser.py`, `generator.py`, `tokens.py`, `helper.py`, `dialect.py` metaclass), the full suite |
| `sqlglot/**/*.py`, AST identical, no coding cookie | Nothing |
| `tests/fixtures/**` | Tests that opened that file (method 2); `#` lines are semantic, `--` lines are not |
| `tests/test_*.py` / `tests/dialects/test_*.py` | The whole file (an import error fails the whole file), plus any test that imports it |
| `tests/helpers.py`, conftest, `sqlglot/__init__.py`, new/deleted/renamed `.py`, setup/pyproject, Rust (`sqlglotrs`, not in requirements.lock, so likely inert, but stay conservative), unparsable | Full suite, optimally packed (~37 s) |

## Concrete pitfalls checklist
- Use **old-file** line numbers; hunks are standalone diffs against the pinned SHA.
- Multi-line statements: use the enclosing def or statement, not coverage.py's first-line-only records (2jrq).
- Decorator, default-argument and `def` lines run at import time. A changed default arg changes runtime behaviour, but the coverage record of that line sits in the import context. Treat a def *signature* change as "function changed" and select its callers' tests.
- Lambdas in class-body dicts (TRANSFORMS/FUNCTIONS): the body is a separate code object and runs at test time, so it is traced. The dict key/value line itself is import-time, so treat it as class level.
- Lazy dialect import: run the build with every dialect pre-imported, or record class-level usage. Otherwise import-time lines get attributed to the first test only.
- Per-test map caveat with caches: union with file-level ownership of hits.
- xdist at build: each worker records its own map. Merge them, and make sure `restart_events()` runs per test in each worker.
- Map files must be readable by `nobody` (chmod 644). They are built in the Dockerfile, so the 20 MB repo limit doesn't apply.
- Timeouts count as failures. Patches that create infinite loops in core functions fail *many* tests, but with timeouts that is a recall problem, not a cost one.

## References
- Rothermel & Harrold, "A safe, efficient regression test selection technique", TOSEM 1997 — https://dl.acm.org/doi/10.1145/248233.248262
- Yoo & Harman, "Regression testing minimization, selection and prioritization: a survey", STVR 2012 — https://onlinelibrary.wiley.com/doi/10.1002/stv.430
- Gligoric, Eloussi, Marinov, "Practical regression test selection with dynamic file dependencies" (Ekstazi), ISSTA 2015 — https://dl.acm.org/doi/10.1145/2771783.2771784 ; tool: https://www.ekstazi.org/
- Legunsen et al., "An Extensive Study of Static Regression Test Selection in Modern Software Evolution" (STARTS), FSE 2016 — https://mir.cs.illinois.edu/~marinov/publications/LegunsenETAL16StaticRTS.pdf ; https://github.com/TestingResearchIllinois/starts
- Empirical comparison of four Java RTS techniques (Ekstazi, STARTS, HyRTS…), ASE 2022 JF — https://conf.researchr.org/details/ase-2022/ase-2022-journal-first-papers/18/An-empirical-comparison-of-four-Java-based-regression-test-selection-techniques
- Zhang, "Hybrid regression test selection" (HyRTS), ICSE 2018 — https://dl.acm.org/doi/10.1145/3180155.3180198
- Celik et al., "Regression test selection across JVM boundaries" (RTSLinux, tracks file I/O), FSE 2017 — https://dl.acm.org/doi/10.1145/3106237.3106297
- Kauhanen et al., coverage-based RTS for Python (pytest + coverage.py + git) — https://core.ac.uk/download/479167931.pdf
- pytest-testmon, "Determining affected tests" — https://testmon.org/blog/determining-affected-tests/ ; https://pypi.org/project/pytest-testmon
- pytest-incremental — https://pypi.org/project/pytest-incremental/
- coverage.py dynamic contexts — https://coverage.readthedocs.io/en/latest/contexts.html
- Python `sys.monitoring` (PEP 669) — https://docs.python.org/3/library/sys.monitoring.html ; audit hooks (PEP 578) — https://docs.python.org/3/library/sys.html#sys.addaudithook
- Machalica et al., "Predictive Test Selection", ICSE-SEIP 2019 — https://arxiv.org/abs/1810.05286 ; https://engineering.fb.com/2018/11/21/developer-tools/predictive-test-selection/
- Memon et al., "Taming Google-Scale Continuous Testing", ICSE-SEIP 2017 — https://research.google/pubs/taming-google-scale-continuous-testing/
- Leong et al., "Assessing Transition-based Test Selection Algorithms at Google", ICSE-SEIP 2019 — https://research.google/pubs/pub48335/
- Launchable, "How Launchable selects tests" — https://help.launchableinc.com/features/predictive-test-selection/how-launchable-selects-tests/
- Saha et al., "An information retrieval approach for regression test prioritization based on program changes" (REPiR), ICSE 2015 — https://ieeexplore.ieee.org/document/7194580
- Zhang et al., "Predictive Mutation Testing", ISSTA 2016 — https://dl.acm.org/doi/10.1145/2931037.2931038
- Shi et al., "Evaluating test-suite reduction in real software evolution", ISSTA 2018 — https://dl.acm.org/doi/10.1145/3213846.3213875
- Graham, "Bounds on multiprocessing timing anomalies" (LPT 4/3 bound), SIAM J. Appl. Math 1969 — https://epubs.siam.org/doi/10.1137/0117039
- sqlglot at the pinned SHA: `tests/helpers.py` (fixture loading, `#` meta), `sqlglot/dialects/__init__.py` (lazy `__getattr__` import) — https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa
