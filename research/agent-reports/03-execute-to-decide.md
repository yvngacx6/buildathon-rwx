# 03 — Execute to decide (run the tests at run time, output only what fails)

## Executive summary

- **Running every candidate test is not feasible.** Two full-exec prototypes broke the 3-minute batch limit on the 30 sample commits: 218 s wall with subprocess pytest, and 195 s with a fork server. A third version, with AST-equality pruning and a 170 s budget guard, got in at 176 s wall but used 177 CPU s, for a total of about 340 s. That is far worse than outputting the same candidates statically.
- **The economics are against it.** Simulated cost is the busiest of up to 8 parallel workers, so a candidate set of T recorded seconds costs about `max(giant, T/k) + 2k`. Running it costs about 1.0–1.5 × T in CPU, all charged. Execution only pays when the candidate set is cheap (under about 2 CPU s) and the commit probably breaks nothing. Then it turns `2 s + t` into roughly `0.7 s` of CPU and `[]`.
- **Measured result for the best hybrid on the sample:**

  | Approach | Simulated | CPU | Total | Misses |
  |---|---|---|---|---|
  | Static coverage selector (no exec) | 194.0 s | ~2 s | **~196 s** | 0 |
  | Hybrid (run only 5 cheap commits) | 182.7 s | 9.4 s | **~192 s** | 0 |
  | Exec everything except tests over 5 s | 163.3 s | 177 s | ~340 s | 0 (176 s wall, at the limit) |
  | Oracle (exactly the failing tests) | 94.4 s | 0 | 94.4 s | 0 |

  In the hybrid row, the container's CPU was 2.6 s for the parent (startup, map loading) plus 6.8 s for test children.
- **The hybrid gains about 4 s, roughly 2%, and one miss costs 300 s.** All of the gain comes from 2qn5, 5y72, 8y7j, fryt and ktz3. Exec is worth it only if the chance of a pass here / fail for the labelers mismatch is well under 1–2% per batch. The evidence is good but small, see Fidelity. **Recommendation:** ship the static coverage selector. Keep exec as an opt-in flag (default off) for candidate sets under about 2 CPU s, with the deadline guard.
- **Exec can only prune.** It never adds a test outside the candidate set, so it does nothing about the final's risk from unseen kinds of change. Recall comes entirely from the static selector.
- **Side result worth more than exec: the static selector built for this study.** It is per-test line coverage plus pool-aware tracing, class/dialect usage and per-file AST equality. It gets **0 misses at 194 s simulated**. Build it in the Dockerfile: the coverage pass takes 66–79 s wall with `-n 4`.

## Measured numbers

All runs were on EC2 (`~/agent-exec`), using the Makefile's exact `docker run` flags, the pinned python:3.13-slim digest, `requirements.lock`, sqlglot @ eb11aec from the GitHub tarball, `SKIP_INTEGRATION=0` and precompiled `.pyc`.

| What | Wall | CPU |
|---|---|---|
| `python -c pass` | 0.01 | 0.01 |
| `import pytest` / `import sqlglot` | 0.15 | 0.15 |
| `import duckdb, pandas, numpy` | 0.43 | 0.60 |
| `import tests.test_optimizer` | 0.65 | 0.79 |
| pytest collect-only, whole suite (1621 items) | 1.95 | 2.15 |
| Full suite, 1 pytest process, real spawn pool (4 host cores visible) | 118.5 | **148.0** |
| Full suite, 1 process, in-process SyncPool | 118.0 | **118.7** |
| 1 optimizer fixture test, fresh pytest, spawn pool, 4 cores visible | 3.3 | **5.8** |
| same, `taskset -c 0` (pool = 1 worker) | 1.9 | 1.8 |
| same, fork server + SyncPool (below) | 0.7–0.9 | **0.7–0.9** |
| 1 dialect test (clickhouse), fresh pytest | 0.7 | 0.7 |
| Coverage-map build (`-n 4`, sys.monitoring plugin) | 66–79 | 176–247 |

Per-test CPU under SyncPool, compared with the recorded seconds in tests.json:

| Test | CPU | Recorded |
|---|---|---|
| test_execute_tpcds | 32.0 s | 21.45 s (its pool ran in parallel for them) |
| test_max_nodes | 29.4 s | 28.8 s |
| test_generator_memory | 8.3 s | 6.7 s |
| test_optimizer::test_tpcds | 5.6 s | 2.2 s |
| tests/dialects/* (1020 tests) | 9.3 s total | 10.45 s |
| test_optimizer.py | 24.7 s | 19.8 s |
| test_executor.py | 38.0 s | 31.6 s |

The suite without its three giant tests is about 49 CPU s. Running even "everything cheap" costs about 2.5× what 8 static workers would charge for it (about 22 s).

### Prototype runs (30 sample commits, scorer flags)

| Version | Wall | CPU | Simulated | Notes |
|---|---|---|---|---|
| v1: subprocess pytest per commit, real spawn pool | 217.9 | 247 | 189.7 | **over 180 s** |
| v2: fork server + SyncPool, run all candidates ≤5 s | 194.7 | 195 | 191.7 | **over 180 s**. 3utz and rszg hit the 40 s timeout |
| v3: v2 + per-file AST-equal skip + budget guard | 176.3 | 177 | 163.3 | inside the limit only because of the guard |
| v4: hybrid `plan()` | **9.3** | **9.4** | **182.7** | 0 misses, 5 commits executed |

At 4.5 min for about 45 commits, v3 would also be marginal. Exec-all is not viable at either size.

## How to run patched sqlglot under the run limits (works)

- **Overlay with a symlink farm.** Do not copy the tree. Create `/tmp/w/<cid>/` with symlinks to `/opt/sqlglot/{sqlglot,tests,setup.cfg,pyproject.toml}`. Only the directories on the path of a patched file are made real, filled with symlinks to their siblings, plus the patched file itself. Disk use is a few KB per commit, and it is removed after each run.
  - Unchanged modules reuse the build-time `.pyc`, which is validated by mtime and size, not path.
  - The patched module compiles in memory, because `PYTHONDONTWRITEBYTECODE` is set and `__pycache__` is read-only.
  - `tests/helpers.FIXTURES_DIR` follows `__file__` through the symlink, so patched `.sql` fixtures are picked up.
- **Patch applier.** About 15 lines of Python. The image has no `patch` or `git`; the diff is applied against old line numbers.
- **Fork server.** The parent pre-imports `pytest`, `duckdb`, `pandas`, `numpy`, `pandas.testing`, `unittest.mock` and `multiprocessing`, but **never `sqlglot`** (asserted). Each commit `fork()`s; the child does `chdir` into the overlay, sets `sys.path = [overlay, /app, …]`, then calls `pytest.main([... node ids ...])` with `-p no:cacheprovider`, `--timeout 20` and a 10-line `failrec` plugin.
  - `failrec` records `report.failed` from `pytest_runtest_logreport`, which catches SUBFAILED subtests. It also records collection errors, which fail every selected test in that file.
  - The parent `waitpid`s with a 60 s kill, and per-commit CPU comes from `RUSAGE_CHILDREN` deltas. Per-run overhead is about 0.7 s.
- **Process-pool hazard. This is a must-fix for anyone running test_optimizer or test_executor at run time.**
  - `TestOptimizer.setUpClass` and `test_execute_tp*` create `ProcessPoolExecutor(mp_context=spawn)` with default `max_workers = os.process_cpu_count()`. That follows **CPU affinity, not `--cpus 2`**, so on the scorer's host it is that host's core count.
  - Each spawned worker re-imports sqlglot, duckdb and pandas: about 1 CPU s and about 150 MB.
  - On a 32–64-core scorer host, one optimizer test could spawn dozens of workers. The CPU bill would explode, and it risks the 4 GB OOM and the 256-pid limit.
  - Fixes: `os.sched_setaffinity` to 1–2 CPUs, or replace the pool with an in-process `SyncPool`. SyncPool still pickles args and results, so serde/pickle breakage still shows up (3utz). It cut CPU per small run from 5.8 s to about 0.8 s.
- **Timeouts.** 3utz's serde bug makes the optimizer and executor tests hang or crawl, both under spawn and under SyncPool. `--timeout 20` did not fire, so the 60 s kill did, and every run test was marked failing. That matches the labels, where all 37 fail. A timeout must count as fail, which is also the safe direction: a false fail costs only seconds.

## Candidate selection used (static, build-time maps from the pinned commit)

1. **Per-test line coverage.** sys.monitoring LINE events with DISABLE, plus `restart_events()` per test phase. Setup-phase lines are attributed to every test in the class, because of `setUpClass`.
2. **Pool-aware coverage.** During mapping, the spawn pools are replaced by SyncPool, with pickle round-trips and the `initializer` honoured.
   - Without this, the coverage map missed 22 failing tests on 4 commits: 2jrq, 3utz, ary6 and 535z. Optimizer fixture work runs in spawn children, and serde runs during result pickling.
3. **Data files.** An `open` audit hook records which tests read each fixture.
4. **AST scoping of each changed line** in the old file:
   - Executable line inside a function: the tests that cover that line.
   - Def header, adjacent, or uncovered line inside a function: the tests covering any line of that function.
   - Class-body line: the tests that instantiated that class or a subclass (recorded by wrapping `__init__` of Expr, Tokenizer, Parser, Generator, Dialect and TypeAnnotator), plus the tests covering lines of the class.
   - Module-level line: tests covering the file, plus users of its classes.
   - For dialect, parser, generator, typing or tokenizer files, also the users of the same-named dialect. This catches `TSQL.WEEK_OFFSET` → test_simplify.
5. **Per-file AST equality.** A `.py` whose `ast.dump` is unchanged contributes nothing. This is a static rule, not exec. It cleared 9u5k, bap8 and f4xa, and the comment-only hunks of 986c, bvw2 and rszg.
   - Blind spot: tests that read source text or line numbers. Low risk for sqlglot, but the task warns that comment-only changes are not promised harmless.

Result: candidates cover all 45 failing tests on all 30 commits. The sum of candidates' recorded seconds is 258 s before AST pruning. This was developed while looking at sample misses, so treat 0 misses as optimistic for the final.

## Hybrid policy (what v4 does)

For each commit with candidate set C, let `P(X)` be the optimal ≤8-worker cost of outputting X, and `cpu[]` the per-test CPU measured at build. Try every prefix R of C ordered by CPU:

    predicted(R) = 0.7 + Σcpu[R] + (1-p)·P(C\R) + p·P(C)     vs     P(C)

- p is the prior that a run test fails: 0.9 if the patch touches a non-.py file (fixtures nearly always fail), else 0.4.
- If no prefix beats `P(C)`, output C statically.
- Otherwise run R, and output (observed failures) ∪ (C\R).

A global deadline guard (150 s, or about 240 s in the final) turns exec off for the rest of the batch.

On the sample this runs 5 commits (2qn5, 5y72, 8y7j, fryt, ktz3, 0.7–3.0 CPU s each), and each outputs `[]`. A partial run (R a strict prefix) never won, because one giant in C makes everything else free to include statically.

Sweeping the priors in the simulator (`sim.py`, measured CPU and sample outcomes) gives hybrid simulated+exec-CPU of 187.6–194.0, against 194.0 for static. The best was p=0.4 with OH=0.7. Measured in the container: 182.7 + 9.4 CPU. The simulator assumed 5.0 CPU, but 5y72 really cost 3.0 s against 1.5 s predicted, because the optimizer module and setUpClass cost more than assumed. The `plan()` thresholds rest on EC2 per-test CPU; the scorer's hardware may differ.

## Where the ~100 s gap to the oracle sits, and why exec can't close it

| Commit | Static | Oracle | Why |
|---|---|---|---|
| 2haz | 25.45 | 0 | `test_execute_tpcds` in C, nothing fails |
| 66ks | 24.79 | 0 | `test_execute_tpcds` in C, nothing fails |
| 2jrq, 3utz | 25.45 | 25.45 | tpcds really fails |
| 4d63 | 12.7 | 0 | class-level change pulls 359 tests + test_generator_memory |
| 535z | 11.2 | 2.6 | class-level change pulls 171 tests + test_generator_memory |
| 6ze8 | 9.2 | 2.0 | |
| ary6 | 9.0 | 4.4 | |

- **Executing tpcds to drop it costs 32 CPU s**, more than the 23.5 s it adds statically.
  - A test-internal partial run (only the queries covering the changed lines) costs 0.9 s import + 1.3 s setUpClass + up to 15.1 s of sqlglot execution for 57 queries, **plus** the duckdb and pandas comparison half (about 15 s).
  - `cached_execute` transpiles the duckdb reference through the *patched* sqlglot, so reference results cannot be precomputed at build.
  - Best case it saves about 6 s on 66ks and nothing on 2haz. Not worth it.
- **"Drop the giant if all cheap candidates pass" (proxy rule).** It was right on 4/4 sample commits: 2jrq and 3utz had cheap failures, 2haz and 66ks had none. Net about +5 s on the sample, at a 300 s risk whenever tpcds fails alone. It is a gamble, not a decision rule.
- **The big class-level sets (4d63, 535z) cost more to run than to output:** 14.8 and 8.4 CPU s against 12.7 and 11.2 static.

## Fidelity and risks

- **Evidence.** In v3, observed failures matched `answers.json` **exactly** on all 30 commits: every failing test seen, no false fails. The only extras were giants kept statically.
  - This held for the SyncPool runs (3utz serde, 535z test_simplify) and for fixture tests.
  - It is about 45 failing tests and about 1,100 candidate test-runs. At a 1–2% miss threshold that is weak evidence.
- **Ways our run can pass where theirs failed:**
  1. **Subset in isolation versus their (unknown) grouping.** Leaked state such as `sqlglot.schema` set in `setUpClass`, the leak/memory tests, and the `cache` dicts on classes.
  2. **SyncPool versus fresh spawn processes.** No re-import, shared module globals. The spawn pool is affordable only if affinity is pinned (1.8 s vs 0.8 s per small run).
  3. **Their pytest-timeout value and the scorer's CPU speed are unknown.** A slow-but-passing test in our env may be a timeout in theirs, and the reverse.
  4. **Overlay differences:** a patch that adds or deletes files, renames, or touches `tests/conftest`-like files. Handled, but untested on the sample.
- **Operational risks.** The process-pool host-core blow-up (above); wall time (exec-all is at or over the limit); `/tmp` 64 MB is fine with symlinks and is not with copies (sqlglot/ is 8 MB and tests/ is 12 MB).
- **Portability.**
  - `map.pkl` is 10.8 MB, half the 20 MB repo limit. Generate it in the Dockerfile instead: the coverage pass takes 66–79 s wall with `-n 4` and fits easily in the 10-minute build.
  - `map.pkl` is keyed by sample test numbers. Key it by nodeid and remap at load, in case the final `tests.json` numbers differ.

## Files

`research/agent-reports/03-exec/`:

| File | Purpose |
|---|---|
| `Dockerfile` | prototype image: lock install, tarball clone, compileall |
| `run.py` | runtime: overlay, fork server, plan, failrec, deadline guard. `MODE=hybrid\|all\|static`, `SYNC`, `PFAIL`, `PFIX`, `BUDGET`, `TMO` |
| `select_cands.py` | patch parser and AST-scoped selector |
| `covplug.py` + `buildmap.py` | coverage-map build |
| `cputime.py` + `cpu.json` | per-test CPU under SyncPool |
| `syncpool.py` | in-process stand-in for the spawn pool |
| `failrec.py` | records failing tests |
| `sim.py`, `analyze.py` | offline policy simulator and recall check |
| `tpcds_probe.py` | tpcds cost breakdown |
| `flags.sh`, `timeit.py` | scorer flags; CPU measurement wrapper |

`map.pkl` (10.8 MB) is not in the repo. It is in the session scratchpad, and you can regenerate it with covplug + buildmap in about 70 s on EC2.
