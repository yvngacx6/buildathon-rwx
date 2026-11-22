# Moonshot 05: divergence-point (checkpoint) re-execution of slow tests

## Verdict (numbers first)

- **It works for test_execute_tpcds: about −15 to −20 s net per 40 practice-style commits, with 0 misses.**
  This is a solid r17 upgrade, not a ~70 s moonshot.
- **Labeled practice_like (120 commits, EC2 container):**
  - sim 989.6 → 898.5 s (**−91.0 s**), misses 0 → 0;
  - tpcds drops 6 → 12;
  - subtest child CPU +17.5 s on a loaded EC2 box. Charged ×2 for the scorer, that is ≈ +35 s, so the
    net is ≈ **−56 s / 120 ≈ −18.7 s per 40**.
- **private (264):** −29.1 s sim, 0 misses. **sample (30):** unchanged, 0 misses.
- **The 166 commits that today's r17 refuses on budget** (from P+Q+private+practice_like, 1,872 commits):
  - 46 drops, sim −687 s, child CPU 194 s;
  - all 11 labeled true tpcds failures were kept.
- **Ground truth:** 38 dropped commits re-run with the real TPC tests on patched sqlglot (34 unlabeled
  from P/Q plus gbxa, gqeb, q26m, 1k2u). **test_execute_tpcds passes in all 38.** In Q_9n1e, 2
  test_optimized_tpch subtests fail, but that test was kept: only tpcds was dropped.
- **test_max_nodes (28.8 s) cannot be addressed this way.**
  - Its 27 s is the P3 parse of 10,000 identical columns (2.02M tokens). The node limit trips at the
    last column (`Col: 2020006`), so the whole input is parsed and there is no prefix to skip.
  - Every P3/P4 line runs on every iteration.
  - Tokenization is only 4.4 s.
  - slowverify (r18) already covers lines outside P3/P4.
  - genmem and the TPC-H tests are too cheap to matter.
- **Code:** branch `moon-05`, `selector/subtest.py` (+~200 lines, `BR_SUBTEST_STAGE=0` turns it off).
  The store build grows by ~60 s.
- **Recommendation: merge (P(works on board) ≈ 0.7).** Expect a practice board effect of about −10 to
  −20 s (it hits optimizer-edit commits such as 1k2u).

## Where the slow-test seconds go today

Run on current main (laptop):
- P: 113 of 600 commits keep a slow test. 61 of those are the subtest budget refusal
  (`subtest: keep (est 11.4s CPU for 57 queries)`).
- Q: 88 of 1,000 are refusals.
- private: 16 of 264 are refusals; 11 of them truly fail tpcds.

The typical refused commit is a function-body edit in optimizer, scope, schema, serde or generator code.
It touches 57 tpcds queries, and re-running those queries whole costs 11.4 s laptop CPU. That is over the
r17 cap of min(12 s, 0.6 × gain) at 2x.

## Why a divergence point helps

Per-query stage CPU over the 99 tpcds queries:

| stage | share of CPU |
|---|---|
| executor Join | 58 % |
| optimize | 20 % |
| Scan | 8 % |
| parse | 5 % |
| duckdb transpile | 3 % |
| rest | 5 % |

8,431 of the 9,149 sqlglot lines tpcds runs execute only in parse, optimize or plan. With a checkpoint at
the optimize→plan boundary, re-running only the stages that run the touched line makes 98 % of lines
verifiable within the budget, against 22 % today. Mean per-line verify CPU drops from 11.6 s to 2.2 s.
Over the tpcds queries, optimize-only re-runs cost 1.97 s, against 11.43 s for whole queries.

## Mechanism (implemented)

The real process boundary is the test's pool. The main process sends `pickle(parse_one(sql))`, and the
worker runs `sqlglot.executor.execute`.

**Build** (each query in its own fork). For each executed tpcds query, store:
- the post-parse pickle (1 MB total);
- `strict_digest` of the parse tree;
- `strict_digest` of the optimized tree, taken after `execute()`'s eager `expression.sql(pretty=True)`;
- coverage of four windows of a mirror of `execute()`:
  - prep: find_tables, ensure_tables, ensure_schema, the table-args check;
  - optb: optimize plus the debug sql;
  - plan;
  - post: PythonExecutor plus the result pickle;
- the optb CPU.

The mirror calls the same objects through `sqlglot.executor`'s namespace. Its result digest must equal
the real `execute()`'s, else the query never uses stage mode. `strict_digest` covers class, every arg key
(None-valued ones too), value types, `.type`, comments, `_meta`, parent / arg_key / index links and node
sharing.

**Run.** A query whose touched statements hit prep, plan, post or `execute()`'s own lines gets a full
re-run, which is today's r17. Otherwise:
- **parse hit:** re-parse and compare the digest.
- **optb hit:** unpickle the checkpoint, run optimize and the debug sql under the new code, and compare
  the digest.
- **duckdb hit:** transpile and compare.

Equal digests mean plan and post get an identical input and run untouched code, so they behave as
pinned.

On divergence, re-run the whole query while the diverged queries' pinned CPU is ≤ 1.5 s
(`BR_SUBTEST_FALLBACK_CAP`), else keep the test. The cap matters in both directions:
- Pure abort lost two r17 drops on private (8osi, k9tf: a different tree, same result).
- Unlimited fallback cost +281 s CPU for +194 s of sim gain on the refused set (net negative).

Cost model: a stage query costs its pinned optb CPU + 0.02 s. The r17 budget rules are unchanged; the
verify budget gets +1.5 s for the fallback.

## Soundness and remaining risks

**Argument.** Two conditions give the same plan and post behaviour:
- a stage-boundary digest that is equal and type-strict, and
- unchanged code from that boundary on, starting from the same global state.

**New gap compared with r17.** Global state written during optimize and read by plan / post in the
same query (lru caches, dialect instances, class attributes) is not compared. r17 already accepts the
cross-query version of this gap: pool workers reuse the process, and untouched queries are not re-run.

**Guard to add before merging** (cheap, not built). Use stage mode only when the changed function's
new body has no `global` / `nonlocal` and no attribute or subscript store on a non-local base; otherwise
use full mode. Also add 3–5 adversarial mutants (an optimizer edit that pokes a dialect or `exp` class
attribute read by PythonExecutor), labeled with the full suite.

**Load sensitivity.**
- The store's CPU numbers come from the build machine. On EC2 at load ~20 they were inflated 2.4x, and
  the drops fell from 46 to 9 because of it. The numbers above use a store calibrated to the laptop's
  main-store `cpu_exec` (`calib.py`).
- In the scorer image the build runs on idle cores, so this should be fine. Still check the store's
  tpcds `cpu_exec` sum: it should be ≈ 11 s.
- The child CPU timer (3 × pinned + 0.8 s per query) also turns passes into keeps under heavy load. That
  is safe, only lost gain.

**Hash seed.** The pinned digests are built with PYTHONHASHSEED=0, and the run's seed is random. A
set-order-dependent optimize then reads as "diverged": keep, or fall back. That is safe.

## Phase 2 (not built, ranked)

1. **Rule-level checkpoints inside `optimize`** (14 rules). Pickle the tree and the schema before each
   rule, and re-run only the rules that run the touched line. This should cut stage CPU 3–5x, turn more
   of the 63 remaining `est` refusals into verifications, and lower CPU on the refusals that really
   fail. About 3 h.
2. **Executor step checkpoints** for the 32 refusals that touch executor, table, context or schema
   lines. A Join change re-runs the hot part anyway, so the value is low.
3. **CRIU / storable fork snapshots:** impossible (no CRIU in the container). Pickled expression
   checkpoints are the practical form.

## Validation runs (reproduce)

- EC2 `~/work/moon05/job.sh` and `job2.sh`, through `~/shared/drun.sh`, 2 CPUs: build the index, then
  run `BR_SUBTEST_STAGE=0/1` on R (the 166 refusals), practice_like, sample and private.
  `job2.sh` calibrates the store first.
- Ground truth: rwx-big `~/work/moon05/truth_big.sh` (pure-Python patch apply plus the three TPC tests).
- Scripts: `research/moonshots/05/` (stageprof, sim, mkset, calib, ec2an, job*, truth_big, apply).
