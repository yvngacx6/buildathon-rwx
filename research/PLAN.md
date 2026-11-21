# Build plan: rank-1 method

Goal: 0 misses on the hidden final, then the lowest simulated seconds. Cutoff: **Sun 2026-11-22 08:00 PST**. Submissions: 1 per hour.

Method (see `FINDINGS.md` §2, rank 1): a dynamic test map from the pinned sqlglot commit at function level, a class-use map, a fixture read map, an AST no-op filter, a fallback table, and cost-aware packing.

---

## 0. Target repository layout

```
buildathon-rwx/
├── Dockerfile               # pinned base image; copies selector/ and maps/
├── Makefile                 # starter targets + our eval targets
├── README.md                # what we do + compliance note for the judges
├── selector/                # RUN-TIME code (stdlib only, Python 3.13)
│   ├── __main__.py          # entry: read /data, print one JSON line per commit
│   ├── diff.py              # unified-diff parser + pure-Python patch apply
│   ├── classify.py          # each changed region → change kind (§3 table)
│   ├── select.py            # change kind + maps → set of test ids
│   ├── risk.py              # decisions for the three slow tests
│   ├── pack.py              # LPT packing, choose worker count k
│   └── maps.py              # load maps; map test id → number from tests.json
├── maps/                    # GENERATED, committed, gzipped JSON
│   ├── functions.json.gz    # "file::qualname" → [test ids]
│   ├── lines.json.gz        # "file" → {line: [test ids]} (module-level code)
│   ├── classes.json.gz      # "module.Class" → [test ids] (instantiation, via MRO)
│   ├── reads.json.gz        # "tests/fixtures/x.sql" → [test ids]
│   ├── test_imports.json.gz # test-side module → [test files]
│   └── MANIFEST.json        # sqlglot sha, image digest, test count, build date
├── mapbuild/                # BUILD-TIME tools (run once on EC2, not in image)
│   ├── run_tracing.sh       # runs pytest in the pinned image with the plugins
│   ├── plugins/
│   │   ├── syncpool.py      # inline process pool + pickle round-trip
│   │   ├── functrace.py     # sys.monitoring: per-test function entries + lines
│   │   ├── classuse.py      # per-test class instantiation (with MRO)
│   │   └── reads.py         # audit hook on open(): per-test file reads
│   └── export.py            # raw traces → maps/*.json.gz + MANIFEST
├── eval/                    # OUR VALIDATION (never copied into the image)
│   ├── mutants.py           # generate private patches against pinned sqlglot
│   ├── label.sh             # run full suite per patch in pinned image on EC2
│   ├── evaluate.py          # run selector on a patch set; report misses + seconds
│   └── data/                # .gitignored: patches + labels
├── tests/                   # unit tests for selector/ (pytest)
└── research/                # this folder
```

Rules for the code:
- `selector/` uses the standard library only. No third-party import at run time.
- Every module has one job. Each function has a docstring that says what it does.
- `maps/` is generated. Nobody edits it by hand. `MANIFEST.json` records how it was made.
- `eval/data/` and large traces are never committed.

---

## 1. Phase A: walking skeleton (target: Fri night, about 2 h)

Purpose: prove the pipeline end to end and use one submission to check the build.

1. Make `selector/` with `__main__.py`, `diff.py`, `pack.py`. The first logic: parse patch, if all changed `.py` files have an equal AST → `[]`, else → all tests with optimal packing.
2. `pack.py`: for k = 1..8, pack with LPT; choose k with the smallest `max load + 2k`. Use integer milliseconds, the same as the scorer.
3. `diff.py`: pure-Python apply of unified diffs. Read the original from a copy of sqlglot at `eb11aec` in the image. If a hunk does not apply → mark the commit "unknown" → ALL.
4. Dockerfile: pinned base digest; `git clone` sqlglot at `eb11aec` during the build (source only, for the AST check); copy `selector/` and `maps/`; `chmod -R a+rX`.
5. `make sample` on EC2. Expected: about 37 s × 27 + 0 × 3 ≈ 1000 s, 0 misses.
6. Unit tests: `tests/test_diff.py` (apply, failed hunk, new file, deleted file), `tests/test_pack.py` (compare to a brute force on small sets).
7. **Ask the user** to make the GitHub repo public (it includes `research/`). Then submit once. Check: the build passes, the output format passes, 0 misses.

Exit criteria: board accepts the run; 0 misses; build time recorded.

---

## 2. Phase B: build the maps (target: Sat morning, about 4 h)

All map work runs on EC2 inside the pinned image `python:3.13-slim@sha256:e48f…` with `pip install -r requirements.lock`, `PYTHONPATH=<sqlglot>` and `SKIP_INTEGRATION=0`. Do not use the laptop: its SQLite is newer and changes which tests run.

1. **syncpool plugin.** Replace `ProcessPoolExecutor` in the test modules with an inline executor. It must pickle the arguments and the results (`pickle.loads(pickle.dumps(x))`), because 3utz breaks only through this path. Port from `agent-reports/05-robustness/syncpool_plugin.py`.
2. **functrace plugin.** Use `sys.monitoring` (PY_START and LINE events) for each test. Record:
   - the functions entered, keyed by `file::qualname` (resolve qualname from the code object);
   - the lines executed outside any function (module and class bodies), for `lines.json`.
   Run tests serially per worker, and give each test file its own process (`pytest -p no:randomly`, xdist `--dist loadfile`), so `setUpClass` is credited to all tests in its class. Credit `setUpClass` work to **every** test in the class.
3. **classuse plugin.** Wrap `__init__` of every class in `sqlglot` that is a subclass of `Dialect`, `Parser`, `Generator`, `Tokenizer`, `Expression`. Record `module.Class` for the class and for each class in its MRO. Call `super(cls, self).__init__` correctly when the class has no own `__init__` (pitfall from report 05).
4. **reads plugin.** `sys.addaudithook` on `open`: record each path under `tests/` and `sqlglot/` per test. Reads in `setUpClass` go to every test in the class.
5. **export.py.** Convert the traces to `maps/*.json.gz`, keyed by **test id** (not number). Write `MANIFEST.json`.
6. **Checks (must all pass):**
   - The set of test ids in the trace equals the ids in `sample/tests.json` (1620). Stop if they differ.
   - All tests pass on the unpatched commit.
   - Total size of `maps/` < 5 MB.
   - The map build is deterministic: run it twice and compare.

Exit criteria: maps committed; checks pass; `mapbuild/run_tracing.sh` rebuilds them with one command.

---

## 3. Phase C: the selector (target: Sat midday, about 4 h)

`classify.py` turns each changed file into one or more change regions. `select.py` maps each region to tests. The commit selection is the union. Any "ALL" region makes the commit ALL.

| Changed file / region | Detection | Selection |
|---|---|---|
| `.py`, AST equal before and after (parse raw bytes; respect a coding cookie) | `ast.dump(old) == ast.dump(new)` | nothing |
| `.py`, docstring-only change | AST equal after removing docstrings | nothing (no doctests run under pytest; check that no test reads `__doc__`) |
| `.py`, inside a function body | innermost enclosing `def` in the **old** file (or new file for insertions) | `functions[file::qualname]` |
| `.py`, function signature or decorator line | — | function map ∪ class map of the enclosing class |
| `.py`, class body (attribute, dict, `arg_types`) | enclosing class, not in a `def` | `classes[module.Class]` (includes subclasses by MRO) ∪ `lines[file]` |
| `.py`, module level (constant, import) | not in a class or `def` | all tests that use any function or class of the module ∪ `lines[file]` |
| `.py` that does not compile after the patch | `compile()` raises | ALL |
| New `.py` file under `sqlglot/` that nothing imports | file absent at pinned commit | nothing |
| Deleted `.py` file | — | ALL |
| `tests/*.py` test file | changed test functions | those tests; `setUpClass`/module level → whole file |
| `tests/helpers.py`, `tests/dialects/test_dialect.py`, `tests/__init__.py`, conftest | — | `test_imports` → all tests in importing files |
| `tests/fixtures/**` | — | `reads[path]`; if nobody reads it → nothing. **Never** apply the AST/comment filter here: `#` lines in fixtures are metadata. |
| Base class body: `expressions/core.py`, `dialects/dialect.py`, `tokens.py`, `parser.py`, `generator.py` | — | ALL (map gives about 1550 tests anyway) |
| `pyproject.toml`, `setup.cfg` | — | nothing, unless a `[tool.pytest*]` / `[pytest]` section changes → ALL |
| Docs, `*.md`, `benchmarks/`, `posts/` | — | nothing |
| Anything else, or a hunk that does not apply | — | ALL |

Then:
1. **Unknown tests.** A test id in `/data/tests.json` that is not in our maps → add it to every non-empty selection (safe default).
2. **risk.py.** For the three slow tests, include each one only if the map selects it. Do not drop a mapped slow test. (Report 04: the risk threshold for tpcds is 7%; dropping it on a guess is not safe.)
3. **pack.py.** Pack the final set. Fill spare worker time for free only where it costs 0 s.
4. Log the reason for each commit to stderr (kind, regions, test count, cost).

Unit tests: one test per row of the table above, with a small hand-made patch.

Exit criteria: `make sample` → 0 misses, score ≤ 200 s; all unit tests pass; total run CPU < 5 s.

---

## 4. Phase D: private validation (target: Sat afternoon, runs in background)

The 30 samples are our tuning set. We need a separate measure of recall.

1. `eval/mutants.py`: generate about 200 patches against pinned sqlglot. Use seeds and log them.
   - The same operators as the sample (and/or, ==/!=, is/==, </<=, int ±1, True/False, template comments, fixture token corruption, stacked multi).
   - Kinds that are not in the sample: test-file edits, helper edits, import errors, syntax errors, new and deleted files, class-body constant changes, docstring edits, `pyproject.toml` edits.
2. `eval/label.sh`: on EC2, in the pinned image, apply each patch and run the full suite. Use 4 cores in parallel. Record failing test ids, with the same "fails" rules as SCORING.md (failure, error, timeout, import error, failed subtest).
3. `eval/evaluate.py`: run the selector on the set. Report misses (with the patch and the missed test), simulated seconds, and cost per kind.
4. **Rule:** we fix *general* mechanisms only (for example a better trace). We never add a rule that names an edit kind or a specific test. Nothing from `eval/` goes into the image.

Exit criteria: 0 misses on the private set, or every miss is understood and fixed by a general mechanism.

---

## 5. Phase E: cost reduction (Sat evening, only if Phase D is clean)

Do these one at a time. After each, rerun the sample and the private set. Keep a change only if misses stay at 0.

1. **Narrow class-body regions by attribute name.** For `X.WEEK_OFFSET = …`, select tests that read the attribute `WEEK_OFFSET` on class `X` or a subclass (add attribute-read tracing to the class-use plugin). Report 02 cut 535z from 165 to 15 tests with this idea.
2. **Per-test line map inside large functions.** For long functions (for example `Parser._parse_*`), use the line map with statement expansion instead of the function map. Only where the function map selects a slow test and the line map does not.
3. **Optional run-time execution (report 03).** Only if the candidate set costs < 2 CPU s and has no slow test. Use a symlink overlay in `/tmp`, an in-process pool, a per-commit kill limit, and a batch deadline guard (150 s practice, 240 s final). Off by default. Turn it on only if the private set shows 0 mismatches.

---

## 6. Phase F: submission and freeze (Sat night → Sun 06:00 PST)

1. Submit after each phase that improves the private-set score with 0 misses. Record each submission in `SUBMISSIONS.md` (commit hash, sample score, private score, board score, board misses).
2. The board reports the miss count on 40 hidden commits. **Any board miss → stop and investigate before the next submission.**
3. Freeze by Sun 06:00 PST. Choose the best submission with 0 board misses. Prefer the more conservative one if the scores are within about 10 s: the final has new kinds of change.
4. Keep the repository public with all submitted commits until results are published.

---

## 7. Compliance note for the README (for the judges)

> The selector uses only maps built from sqlglot at the pinned commit: per-test function coverage, per-test class use, per-test fixture reads, and the test import graph. `mapbuild/` rebuilds every map with one command. We generated private mutant patches only to measure recall. No data from them is in the image, and no rule names an edit kind.

---

## 8. Risks and responses

| Risk | Response |
|---|---|
| Final has new kinds of change | Fallback table defaults to ALL for anything unknown. ALL costs about 37 s; a miss costs 300 s. |
| Final `tests.json` numbers differ from the sample | Maps use test ids. Unknown ids are always selected. |
| Map built in a different environment from the labelers | Build only in the pinned image; check the test-id set. |
| Build > 10 min | Maps are committed; the build only clones sqlglot and copies files (about 30 s). |
| Scorer CPU is slower | Run time is pure Python with gzipped JSON: about 1–2 s for 60 commits. |
| Coverage credit for cached work goes to the first test only | One process per test file; `setUpClass` credit to the full class; Phase D measures the effect. |
| Judges read the code | Clean layout, docstrings, compliance note, reproducible map build. |
