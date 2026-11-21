# Moonshot 02: warm fork-server test oracle

**Verdict: not a moonshot. Do not build it for the final.** The prototype works and is accurate. On
labeled commits it got every verdict right: **0 false passes and 0 false fails across 98 commits**.
It still cannot pay for itself. Running a test costs more CPU than the test's recorded seconds,
and the scorer runs about 2x slower than the laptop. On practice-style commits (set P) the best
gated variant comes out at **-0.5 to +0.8 s per 40 commits**. On the sample set it is +7 to +9 s.
That does not get practice from about 190 s toward 120 s.
Chance it helps the final measurably (more than 2 s): **about 10%**. Expected gain: **about 0 to +2 s per 40 commits**.

## Structural reason (this alone decides it)

- **CPU per test.** With sqlglot and the test modules already imported, running the tests directly
  through unittest (no pytest) costs about the same as the recorded time.
  - All 1615 cheap tests (under 3 s): 51-56 s of CPU against 38.7 s recorded.
  - Dialect tests: 1.26x recorded. Optimizer tests: 1.59x. The real suite uses a spawn pool, so
    its recorded wall time is lower than the CPU it burns.
  - Class fixtures come on top. `TestExecutor.setUpClass` (DuckDB TPC load) costs 1.6-4.4 s and is
    not part of any recorded time.
  - The scorer's CPU is about 2x slower than the laptop, so a run costs **about 2.6x the recorded
    seconds** in charged CPU.
- **What dropping a test saves.** At most its recorded seconds, and only when it sits on the busiest
  worker. That is at most 1 s saved per 2.6 s spent, so dropping individual tests always loses.
- **Where it can pay.** Removing a whole worker (2 s) or zeroing a whole commit (2 s per worker plus
  the load). That needs tiny candidate sets where every test passes.
  - Sample: such commits are about 5 of 40. Private: about 1. P: about 1.
  - On P, most small-selection commits break something, so at least one worker stays.
- **The ceiling.** Even with a perfect oracle on cheap tests, the scorer's floor is the cost of the
  failing tests. The board diagnostic puts our non-slow cost at about 128 s. The part the oracle
  could remove is paid for at 2.6x in CPU.

## Prototype: `research/moonshots/02-forkserver/forksrv.py` (about 420 lines, laptop, Python 3.13 venv)

1. **Warm-up (parent).** Import sqlglot and all 60 test modules once. `sys.monitoring` PY_START
   records which code objects ran during import. The parent wraps `open()` to record data files read
   during warm-up. It indexes every live function object by file, then `gc.freeze()`.
   - Warm-up CPU: **1.6-2.1 s on the laptop** (about 4 s on the scorer).
   - 1.1 s of that is `tests.test_optimizer`, which pulls in pandas and duckdb.
   - sqlglot plus unittest alone: 0.16 s. Bare interpreter: about 0 s.
2. **Per commit: fork.** The child applies the patch in one of two ways.
   - **Code swap** (348 of 370 commits run). Old and new sources are compiled with the module's
     filename, and both code trees are walked keyed by (qualname, occurrence).
     - The swap is refused if the tree shape changes, the module or class body changes, a changed
       code object ran during warm-up, or `co_freevars` changes.
     - Otherwise `f.__code__ = new` is set on every live function with that code. This covers
       closures and lambdas created at import, bound methods, partials and dispatch tables.
     - Modules not loaded in the parent are served from a meta-path finder holding the patched
       source.
     - Prep CPU: median 0.00 s, max 0.30 s.
   - **Reimport** (21 commits). Remove `sqlglot*` and `tests*` from `sys.modules`, then re-import
     through the patched finder. Prep CPU: median 0.17 s. Lazy dialect imports then happen inside
     the tests.
   - **Patched `.sql` fixtures** are served by the `open()` hook. If a fixture was read during
     warm-up, the child reimports instead.
3. **Run tests.** Selected test methods are called through `TestCase.run` with a custom
   `TestResult`.
   - `setUpClass` and `tearDownClass` run per class, with class cleanups.
   - `addSubTest` with an error counts as a failure. `addUnexpectedSuccess` also counts as a failure.
   - `ProcessPoolExecutor` (spawn) is replaced by an in-process executor that pickles arguments and
     results, as the real pool does. Spawned children would otherwise import the unpatched files on
     disk.
   - `subprocess.Popen` and `os.fork` inside a test mark the test **untrusted** (kept). Only
     `test_lazy_load` triggers this.
4. **Timeouts and hangs.** A per-test `ITIMER_PROF` (repeating) marks a timeout as a failure.
   - Private commit `q1zg` still hung: either a long C call, or a handler swallowed the signal.
   - Fix used: `RLIMIT_CPU` in the child (3 s + 3x the recorded time). The child dies, and the
     parent keeps all candidates.
   - In the final design a hang is expensive (the whole budget is burnt) but safe.

## Measurements (laptop CPU; scorer is about 2x)

Inputs:
- Selections: current main selector (4826380) run on sample (30), private (264) and P (600).
- Oracle runs: every sample commit; private and P commits whose cheap candidates total 1.5 s or less
  recorded (73 private, 253 P).

| | sample | private | P (practice-style) |
|---|---|---|---|
| Oracle runs (code swap / reimport / unsupported) | 30 | 73 (+8 hung or not run) | 253 |
| Fidelity vs labels: false passes / false fails | 0 / 0 (26 commits) | 0 / 0 (72 commits) | no labels |
| Earlier run, swap path only (15 sample commits) | exact match | | |

Per-commit cost examples (child CPU against recorded seconds of the cheap candidates):

| Commit | Child CPU | Recorded |
|---|---|---|
| 2jrq | 12.8 s | 6.8 s |
| 3utz (35 failing tests fail fast) | 11.5 s | 9.7 s |
| 66ks (5 executor tests: the DuckDB class fixture) | 4.7 s | 0.07 s |
| 535z (snowflake + optimizer) | 6.5 s | 1.6 s |
| 8y7j | 0.25 s | 0.03 s |

Totals over sample: 73.9 s child CPU against 49.7 s recorded.

**Economics per 40 commits.** Our selection is unchanged, except that cheap tests the oracle proved
passing are dropped. Charged at 2x the measured child CPU, plus warm-up once.

Gate (uses no labels): run the oracle only when
`cost(sel) - cost(slow part) > M * 2 * predicted_cpu`, where `predicted_cpu` = per-test CPU plus the
`setUpClass` CPU of the classes involved.

| Variant (M=2) | sample | private | P |
|---|---|---|---|
| Always run all cheap candidates | -61.7 | -307.5 | (similar) |
| Gated, any tests (warm-up 2.6 s) | +0.7 | -1.1 | -3.5 |
| Gated, excluding optimizer and executor tests (lazy warm-up 0.4 s) | **+8.7** | **+0.8** | **-0.2** |

Gated-light details:
- Commits oracled per 40: sample 6.7, private 4.1, P 5.3.
- Commits zeroed per 40: sample 5.3, private 1.1, P 0.9.
- An earlier fail-fast model (stop at the first failure, labels from answers) gave a similar
  ceiling: +2 to +8 s per 40, before warm-up and setUpClass costs.

Sample overstates the gain. It contains many fixture-only commits whose one candidate passes. On P,
the practice-style set, the net is zero within noise.

## Fidelity risks and fallbacks (all point toward "keep the test")

- **Stale state from code swap.** Guarded by "the changed code never ran during warm-up". Module and
  class bodies always run, so they fall back to reimport. Caches filled during warm-up only come from
  code that ran, so they are covered by the same guard. `lru_cache` on a swapped function starts
  empty in the child.
  - Not guarded: code that inspects source or bytecode (`inspect.getsource` reads the disk; the
    linecache still holds the old text). No sqlglot test does this as far as we saw. To be safe,
    reimport would need a linecache override.
- **Process pools and subprocesses.** Handled by the in-process pickling executor and the untrusted
  flag. Any other way of starting a process (multiprocessing `Process`, `posix_spawn`) should also
  set the flag. Not done yet.
- **Order effects and isolation.** Each commit runs in a fresh fork, in file order. The scorer runs
  pytest, possibly in a different order. Shared `cls.cache` dicts in Validator and TestExecutor could
  differ.
  - We saw 0 disagreements in 98 labeled commits. The baseline (no patch) ran all 1615 cheap tests
    with 0 failures.
- **Collection errors, import errors, unsupported diffs** (rename, delete, binary, non-sqlglot
  paths), **child crash or hang**: no oracle for that commit. The existing selection stands.
- **Scorer timeout semantics.** A test that is slow but finishes would pass for us and might time
  out for them. Treat any test that hits our per-test cap as failing (done).
- **Environment.** The image would need `requirements.lock` installed (pandas, duckdb). This is
  allowed (PyPI) and the image has no size limit. The 64 MB `/tmp` and 256-process limit are not an
  issue: one fork at a time, about 200 MB copy-on-write.

## What would make it worth it (and why each fails)

- **Cheaper tests.** Run only the assertions or subtests that reach the patched code. That is
  per-call replay, which is diffexec (I12), already shipped.
- **Make class fixtures warm** (run `setUpClass` in the parent). This removes about 1.6-4 s per
  executor commit. Those commits still have to zero out, and on P they rarely do.
- **Faster warm-up.** Already lazy: 0.4 s scorer for dialect-only gates. Not the bottleneck.

## Side finding worth taking (small, low risk)

The fork-server pattern (warm parent, fork per job, `f.__code__` swap) could replace the existing
**import-state-diff subprocess**, which costs about 1.2 s scorer CPU each, mostly re-importing
patched sqlglot. Code swap does not apply there: the state diff is exactly the case where a module
or class body changed. But a warm fork that reimports only the patched module and its dependents
would cost less than a cold `import sqlglot`. Estimate: 0.3-0.6 s scorer saved per state diff. That
is 1-2 diffs per 40 on practice, so about 1 s per 40. Not worth the risk this late.

## Files

- `02-forkserver/forksrv.py`: the prototype.
  - Run: `venv/bin/python forksrv.py SQLGLOT DATA SELECTOR_STDOUT OUT.json`
  - Options: `[--maxrec 1.5] [--shard i/n] [--only c1,c2] [--baseline --keep-times]`
- `02-forkserver/econ2.py`: fail-fast model.
- `02-forkserver/econ3.py`: measured CPU, gate.
- `02-forkserver/econ3_light.py`: gate excluding optimizer and executor tests.
- `02-forkserver/cold.py`: child CPU against warm baseline.
- `02-forkserver/partial.py`: import CPU.
- Raw outputs (scratch, not committed):
  `/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m02/`
