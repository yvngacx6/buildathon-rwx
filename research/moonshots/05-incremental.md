# Moonshot 05: divergence-point (checkpoint) re-execution of slow tests

## Verdict (numbers first)

- **Works for test_execute_tpcds only. It is a modest gain, not a moonshot:** about **+5 to +9 s net per 40
  practice-style commits** (sim gain −14 s, CPU +5 s at the scorer's 2x). It does not reach ~120 s.
- **test_max_nodes (28.8 s): not addressable.** Its 27 s is the P3 parse of 10,000 identical columns
  (2.02M tokens). The node limit trips only at the last column (`Col: 2020006`), so the parser runs the
  whole input with no prefix to skip, and any P3/P4 line runs on every iteration. slowverify (r18)
  already covers lines outside P3/P4. Checkpointing after tokenization would save only 4.4 s of 31 s.
  genmem and the TPC-H tests are too cheap to matter.
- **Prototype on branch `moon-05`** (`selector/subtest.py`, ~150 lines; `BR_SUBTEST_STAGE=0` turns it
  off). Results on 166 budget-refused commits from P+Q+private+practice_like (1,872 commits):
  - 39 tpcds drops, sim −586 s, child CPU 160 s (laptop at load ~15).
  - **0 misses** on the 17 labeled refused commits (all 11 real tpcds failures kept).
  - **0 misses** on sample and practice_like.
  - Ground truth for the unlabeled drops: see "Validation".
- **Recommendation:** merge as an r17 upgrade (abort-on-divergence mode) only if the ground-truth pass rate
  is 100 % and the "Soundness" guard below is added. Expected board effect ≈ −5…−9 s. Rule-level
  checkpoints inside `optimize` (phase 2) could raise the drop rate and cut CPU further, but that is
  another ~4 h.

## Where the slow-test seconds go now (current main, laptop runs)

| set | commits | commits keeping a slow test | of which **subtest refused on budget** (`subtest: keep (est …)`) |
|---|---|---|---|
| P (practice-style) | 600 | 113 | 61 |
| Q (practice-style) | 1000 | — | 88 |
| private (labeled) | 264 | 66 | 16 (11 truly fail tpcds, 5 pass) |
| practice_like | 8 | 2 | 1 (1k2u passes; 7zco is import-time, not eligible) |

The budget refusals look like this: an optimizer / scope / schema / serde function-body edit touches
57 of the tpcds queries, and re-running them takes 11.4 s laptop CPU. That is over the r17 cap of
min(12 s, 0.6 × gain) at 2x (≈6 s laptop). These refusals are ~9 % of commits, about 3.5 per 40, and
~30 % of them really pass tpcds.

## Why the divergence point helps here: the stage split of one tpcds query

Per-query CPU over the 99 tpcds queries (laptop, under load, ratios are what matter):

| stage | CPU s | share |
|---|---|---|
| exec: Join steps | 8.91 | 58 % |
| optimize | 3.07 | 20 % |
| exec: Scan steps | 1.21 | 8 % |
| parse | 0.79 | 5 % |
| duckdb transpile | 0.47 | 3 % |
| plan + debug .sql + prep + Aggregate/Sort | 0.82 | 5 % |

Of the 9,149 sqlglot lines tpcds runs, 8,431 run **only** in parse, optimize or plan. If a stage's
output is checkpointed and compared, re-running only the stages that run the touched line makes
**98 % of lines verifiable within budget, against 22 % today** (sim.py, per-line CPU). Mean verify CPU
drops from 11.6 s to 2.2 s.

## Mechanism (implemented)

The real process boundary is the test's pool. The main process sends `pickle(parse_one(sql))`, and the
worker runs `sqlglot.executor.execute`.

- **Build** (in the image, +~60 s). For each executed tpcds query, store:
  - the post-parse pickle (1 MB total);
  - a type-strict digest of the parse tree;
  - a type-strict digest of the optimized tree, taken after the eager `logger.debug(expression.sql(pretty=True))`;
  - the coverage of four windows of a mirror of `execute()`: prep (find_tables / ensure_tables /
    ensure_schema / table-args check), optb (optimize + debug sql), plan, post (PythonExecutor + result
    pickle);
  - the optb CPU.

  The mirror calls the very objects in `sqlglot.executor`'s namespace. Its result digest must equal the
  real `execute()`'s, else the query never uses stage mode. The digest
  (`strict_digest`) covers class, every arg key including None-valued ones, value types, `.type`,
  comments, `_meta`, parent / arg_key / index links and node sharing.
- **Run.** A query whose touched statements hit prep / plan / post, or `execute()`'s own lines → full
  re-run (today's r17). Otherwise:
  - parse hit → re-parse, compare the digest;
  - optb hit → unpickle the checkpoint, `optimize` + debug sql under the new code, compare the digest;
  - duckdb hit → transpile, compare.
  - Equal → plan and post get the same input and run unchanged code → the same result.

  Stage queries run first. Any divergence aborts the child → keep. This is `BR_SUBTEST_FALLBACK=0`, the
  default. Fallback=1 re-runs the whole query instead.
- **Cost model:** a stage query costs its pinned optb CPU + 0.02 s. The same r17 budget and spend
  rules apply.

## Measurements (166 refused commits, `R/`, laptop load 13–16)

| mode | drops | sim gain | child CPU (laptop, loaded) | CPU on failing / refused |
|---|---|---|---|---|
| fallback=1 (re-run diverged queries fully) | 52 | −780 s | 442 s | 244 s in 51 failed verifications |
| **fallback=0 (abort at first divergence)** | 39 (+1 tpch) | −586 s | **160 s** | — |

At fallback=1, the extra 13 drops cost +281 s of loaded CPU (≈ +375 s at the scorer) for +194 s of sim
gain, which is net negative. Abort mode wins.

**Per 40 commits:**
- 3.5 refused × 24 % drop rate × 14.7 s mean gain per drop ≈ **−14 s sim**.
- CPU 0.97 s loaded per refused commit ≈ 1.3–1.9 s at the scorer → **+5–7 s**.
- Wasted CPU on non-refused commits where stage mode now runs and fails (sample: selector CPU
  1.04 → 2.22 s, same selections; practice_like 1.08 → 2.76 s): **+1–3 s**.
- **Net ≈ −5 to −9 s per 40.**

Practice board: slow tests cost ~61 s. If practice holds one 1k2u-like commit (optimizer edit, tpcds
passes), this is worth ~−15 s there.

**Load sensitivity (risk):** 1k2u was dropped at load 15 (3.3 s child CPU) and kept at load 70 (CPU
timer). The scorer has 2 dedicated cores, but its CPU is 2x slower. With the budget
min(6, 2.5 × est + 1) laptop-s and est 2.2 s, a pass takes ~4.4–5 s scorer-equivalent, which is
tight. Rebuild the store on an idle machine before trusting the CPU numbers. They were calibrated
here from the main store's `cpu_exec` (`calib.py`).

## Soundness

- **Claim:** at a stage boundary, equal type-strict digest + plan / post run no touched statement + the
  same global state ⇒ plan / post behave exactly as pinned.
- **Gap (new compared with r17): global state written during optimize and read during plan / exec in the
  same query** (lru caches, dialect instances, class attributes). r17 already accepts the cross-query
  version of this gap (pool workers reuse the process; untouched queries are not re-run).
- **Guard to add before merging:** stage mode only when the changed function's new body has no
  `global` / `nonlocal`, and no attribute or subscript store whose base is not a local or parameter
  (otherwise full mode). Mutants in such functions cannot write globals themselves. Writes made by
  unchanged callees are keyed by their inputs (memo caches), so the value read in exec is the pure
  function value.
- **Still to do:** 3–5 adversarial mutants (an optimizer edit that pokes a dialect / `exp` class
  attribute read by `PythonExecutor`), labeled with the full suite.
- Hash-seed: pinned digests are built with PYTHONHASHSEED=0, while the run's seed is random. A
  set-order-dependent optimize then gives a "diverged" result → keep. That is safe, only lost gain.
  All 57 queries were digest-stable across two forks at build.

## Validation

- **Labeled:** private refused 16 + practice_like 1. All 11 true tpcds failures were kept (all
  diverge at a checkpoint). gbxa (tpcds passes) was dropped, and so were gqeb / q26m (pass). sample: 0
  misses, same cost. practice_like: 0 misses.
- **Ground truth for unlabeled drops** (patched sqlglot, the three TPC tests): **GT_RESULT**
- private264 full re-run with stage mode: **PRIV_RESULT**

## Phase 2 (not built)

1. **Rule-level checkpoints inside `optimize`** (14 rules: qualify, pushdown_projections, normalize, …,
   simplify). Pickle the tree + schema before each rule and re-run only the rules that run the touched
   line. This cuts stage CPU ~3–5x (most optimizer lines are in one rule), so the 2.5x budget margin
   stops being tight. It also lets a commit be dropped after 1–2 rules instead of all of optimize. ~3 h.
2. **Executor step checkpoints** (Join is 58 %). Pickle each step's input contexts; this applies to the
   32 refusals that touch executor / table / context / schema lines. Inputs are row tables, which are
   large but fine in the image. A Join-step change, though, re-runs the hot part anyway. Low value.
3. CRIU / fork snapshots stored in the image: impossible (no CRIU in the container, and a fork cannot
   be serialized). Pickled expression checkpoints are the practical form.

## Files

- `selector/subtest.py`: `strict_digest`, `exec_windows`, build windows / checkpoints, stage-mode
  plan / child.
- Scratch (not committed): `stageprof.py` (stage CPU / coverage), `sim.py` (per-line verify CPU),
  `mkset.py`, `delta.py`, `truth.sh`, `calib.py` under the session scratchpad `m05/`.
