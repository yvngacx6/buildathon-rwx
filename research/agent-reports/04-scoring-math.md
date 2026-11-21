# 04 — Scoring mathematics & decision theory

Scripts: `agent-reports/04-scoring/` (`lib.py` reuses `score.py`; `strategies.py`, `expected.py`). All numbers are
exact outputs of those scripts on the 30 sample commits unless marked "est.".

## Executive summary

- **Practice set = 40 commits** (1614.1 / 40.349 = 40.003; leftover ~0.14 s = the starter's CPU). **Final ≈ 60 commits.**
- **Oracle on sample = 94.4 s (3.15 s/commit)**, 0 misses. Scaled to 40 commits ≈ 126 s (approx: depends on kind mix,
  esp. how many commits break `test_execute_tpcds`, which alone costs 25.5 s each). Leader 169 s ⇒ ~4.2 s/commit ⇒
  only **~43 s total slack over oracle for over-selection + CPU** across 40 commits.
- **Leaders have exactly 0 misses**: one miss (300 s) exceeds their whole score. In the final (~60 commits, oracle
  ≈ 190 s), one miss ≈ 1.6× the oracle total. **Recall is everything; precision is a tiebreaker.**
- Packing is trivial: LPT is already optimal on every set tested (all-tests k=1..8, all oracle sets). Choose k by
  `argmin_k max(maxd, S/k) + 2k`; add a worker only if it cuts makespan by > 2 s.
- **Break-even rule**: include a test iff `P(it catches an otherwise-missed failure) > ΔC/300`. ΔC = 0 for a tiny
  test fitting into worker slack (always include), 2 s for opening a worker on an empty commit (p* = 0.67%),
  21 s for tpcds on a light commit (p* = 7%), 28 s for max_nodes (p* = 9.4%).
- **Free-fill**: once a heavy test sets the makespan, other workers' slack is free. Selecting tpcds ⇒ "all tests except
  max_nodes" costs only +6.0 s more (29.5 vs 23.5). Selecting max_nodes ⇒ ALL tests costs +6.0 s (36.8 vs 30.8).
- **Language/CPU**: Python is fine (startup + tests.json parse ≈ 0.02 s). CPU only matters if you import sqlglot or run
  pytest at runtime; running tests locally costs Σ(durations) of CPU vs max-load for selection, so only do it for
  small, cheap sets (see §6). Budget: total CPU must stay ≲ 30–40 s across 40 commits to beat 169.

## 1. Cost model (from SCORING.md / score.py)

Per commit: `C(S) = 0` if nothing selected, else `max_w load_w + 2 s·k` (+300 s if any failing test ∉ S).
Durations in ms (round-half-up). Total = Σ C + CPU seconds; median of 3 runs.

Data shape: 1620 tests, 103.047 s total; 1288 tests ≤ 10 ms; 94 are 0 ms. Heavy tail: `test_max_nodes` 28.8,
`test_execute_tpcds` 21.5, `test_generator_memory` 6.7, then 3.9, 3.5, 2.5, 2.2, 2.0, 1.4 …

## 2. Optimal worker count & packing

For a set with total S and largest item m: `C*(S) ≈ min_k max(m, ⌈S/k⌉) + 2k`.
Ignoring m, k→k+1 pays iff `S/k − S/(k+1) > 2` ⇔ **S > 2k(k+1)**: 1 worker up to 4 s, 2 up to 12 s, 3 up to 24 s,
4 up to 40 s, 5 up to 60 s … 8 needs > 112 s (never reached; all tests = 103 s). Once `S/k ≤ m`, extra workers are
pure waste — the heavy test sets the makespan.

All tests (exact B&B = LPT, proven):

| k | 1 | 2 | 3 | **4** | 5 | 8 |
|---|---|---|---|---|---|---|
| makespan | 103.0 | 51.5 | 34.3 | **28.8** | 28.8 | 28.8 |
| cost | 105.0 | 55.5 | 40.3 (starter) | **36.8** | 38.8 | 44.8 |

So even "run everything" improves from 1210.5 → **1104.5 s** (k=4) for free. LPT matched the exact branch-and-bound
on every set we tried; use LPT + `k` sweep, optionally verify against lower bound `max(m, ⌈S/k⌉)`
(when LPT hits the bound it is provably optimal — this was the case everywhere).

Useful set costs (optimal packing):

| set | cost s |
|---|---|
| tpcds alone / + all except max_nodes | 23.45 / 29.45 |
| max_nodes alone / + everything | 30.82 / 36.82 |
| everything except tpcds & max_nodes | 20.56 |
| all tests < 1 s (1606 tests) | 14.14 |
| all `tests/dialects/` (1020 tests) | 9.23 |
| all dialects + light half of test_optimizer | 12.26 |
| test_optimizer.py whole (11.5 s heavy + 8.3 s light) / light only | 12.61 / 8.17 |
| test_dialect.py / test_snowflake.py / test_expressions.py / test_serde.py | 3.82 / 3.39 / 2.90 / 3.39 |
| test_parser.py light only / test_executor.py light only | 2.33 / 3.35 |

Rule of thumb: **any non-empty selection costs ≥ 2 s**; whole dialect files are ~2.0–3.8 s; the big jumps come only
from the ≥ 1 s tests (concentrated in test_optimizer, test_executor, test_parser, test_generator).

## 3. Oracle & simple strategies (30 sample commits)

| strategy | simulated s | misses | s/commit |
|---|---|---|---|
| run nothing | 5400.0 | 18 | 180 |
| starter: all tests, 3 workers | 1210.5 | 0 | 40.35 |
| all tests, k=4 | 1104.5 | 0 | 36.82 |
| H1: `<dir>/<d>.py`→`tests/dialects/test_<d>.py`, fixture→name-matched optimizer tests, optimizer/*→optimizer+executor, other core→ALL | 1222.1 | 3 (535z, ary6, j6uz) | 40.7 |
| H2: H1 + whole test_optimizer.py on dialect/fixture edits | 846.3 | 1 (ary6) | 28.2 |
| H3: H1 but core→nothing | 1338.0 | 4 | 44.6 |
| **file-level oracle** (whole files containing a failure) | 256.5 | 0 | 8.55 |
| **test-level oracle** | **94.4** | 0 | **3.15** |

Oracle breakdown: 12 commits cost 0 (nothing fails — incl. all 3 comment-only); 14 cost 2.0–3.0 s (one cheap test);
ary6/j6uz/rszg 4.4–5.8 s; **2jrq and 3utz cost 25.45 s each (tpcds fails) = 54% of the oracle total.**
File-level granularity alone costs 2.7× oracle → test-level selection inside test_optimizer / test_executor /
test_parser is essential (those files carry the heavy tests).

Failure clustering: ary6 (bigquery parser) breaks 4 files incl. test_parser and test_optimizer; 3utz (serde) breaks 6;
535z (tsql) and j6uz (bigquery) break test_optimizer tests. Dialect→own-file mapping misses these: optimizer tests
(`test_optimize`, `test_simplify`, `test_annotate_funcs`, `test_qualify_columns`) are the common cross-file casualties.
Heavy tests that failed: tpcds 2/30, `test_tpcds` (2.2 s) 2/30, `test_qualify_columns` (1.4 s) 3/30;
**max_nodes and generator_memory 0/30**.

## 4. Leaderboard interpretation

- Baseline 1614.1 = 40 × 40.349 + ~0.14 CPU ⇒ **40 practice commits**; starter CPU ≈ 0.1–0.2 s.
- Best 169 s ⇒ 4.2 s/commit. Est. oracle on practice ≈ 126 s (±25 s per tpcds-breaking commit difference).
- ⇒ Leaders spend ≈ 43 s on everything beyond oracle: ≈ 1 s/commit of over-selection + CPU, **0 misses**.
  E.g. "always run one worker of high-recall cheap tests" (≈ 2–4 s each on the ~40% empty commits) already eats
  ~30–60 s; the leaders are near that regime, meaning they confidently return `[]` on many no-op commits, or pay ~2 s.
- Final (~60 commits, new kinds of change): a spend of x s/commit on safety costs 60x; worth it if it removes
  ≥ 60x/300 = **x/5 expected misses**. A 3 s/commit safety net (180 s) is justified if it prevents ≥ 0.6 expected
  misses — very plausible given unseen change kinds. Be more conservative in the final than practice rankings suggest.

## 5. Risk trade-off: expected-cost selection

Per commit minimise `E(S) = C(S) + 300·P(miss|S)`. Marginal rule: add test t iff `p_t(catch) > ΔC_t/300`, where
`ΔC_t = min(max(0, minload + d_t − M), 2 + max(0, d_t − M))` (least-loaded worker vs new worker; M = makespan).

| situation | ΔC | p* |
|---|---|---|
| tiny test into slack | 0 | 0 |
| 0.6 s test, 1 worker busy 0.1 s | 0.5 | 0.17% |
| first test on an otherwise-empty commit | 2.0 | 0.67% |
| generator_memory 6.7 s on a 2.6 s commit | 6.1 | 2.0% |
| tpcds on light commit | 20.9 | 7.0% |
| max_nodes on light commit | 28.2 | 9.4% |
| max_nodes when tpcds already runs (2nd worker) | 9.4 | 3.1% |

Note tpcds's sample base rate (2/30 = 6.7%) sits right at its threshold ⇒ it must be decided by patch features
(executor/optimizer/serde/core touched → include), never by default.

Algorithm (`expected.py::optimise`, ~0.1 s per commit):
1. Enumerate subsets of heavy candidates (≥ 1 s; ≤ ~8 of them ⇒ ≤ 256 combos).
2. Sort light candidates by `p/d`; sweep prefixes with O(1) cost estimate `min_k max(m, ⌈S/k⌉)+2k` and log-survival.
3. Pack the best set exactly (LPT/B&B), then **free-fill** remaining positive-p tests into slack (cost 0).
4. Compare against `[]` (cost 300·P(any failure)).

**Caveat — independence is wrong.** Failures cluster (one root cause breaks many tests). Σ of tiny independent p's
over 1000 tests inflates P(miss): toy demo with p=0.0005 on each other-dialect test selects 1149 tests (9.2 s) where
a correlated model would not. Model risk per **cluster** (file / module / "optimizer-fixture" group):
`P(miss) ≈ Σ_clusters P(cluster breaks) · P(not covered | breaks)`; a cluster is covered if its likely-failing tests
are in S. Use the per-test framework only within a cluster.

Empty-commit decision: return `[]` only if `P(anything fails) < 0.67%` (else at least a cheap 1-worker net). On the
sample, 12/30 commits were empty incl. 3/3 comment-only, but comments are explicitly not promised harmless.

## 6. CPU component & implementation

- Measured (laptop, Python 3.13): `python -c pass` ≈ 0.01 s; parsing tests.json (269 KB) + all patches ≈ 0.02 s.
  Starter's board CPU ≈ 0.14 s total. **Static selection in Python costs < 0.5 s total — negligible.** No need for
  Go/Rust; do avoid heavy imports (numpy/pandas/pytest) in the selector and use one process for the whole batch.
- **Not measured** (sandbox denied running sqlglot here; see 03-exec agent): `import sqlglot`, pytest startup/collection.
  Expect ~0.3–1 s per pytest process (est.) — multiply by 40–60 commits.
- **Run-locally vs select**: running set C costs CPU ≈ Σd(C) + overhead (+ copying/patching sqlglot into the 64 MB
  /tmp); selecting C costs ≈ max-load + 2k. For large C selection wins (CPU pays the sum, workers pay the max).
  Local execution wins only to (a) **certify an empty commit** (save the 2 s worker; requires the run to cover
  ≥ 99.3% of failure probability), or (b) resolve cheap borderline candidates and then output only those that failed.
  Never run tpcds / max_nodes / generator_memory locally; decide them statically.
  Break-even per commit: local run worthwhile iff `overhead + Σd(C) < 2 s + expected saved over-selection`.
  With ~0.5–1 s overhead that leaves ~1 s of test time — only small targeted sets.
- Use pytest-timeout if anything runs (a hanging patch burns wall + CPU; timeouts count as failures in labels).
- Median of 3 runs: CPU variance matters only if CPU is large; keep it small and deterministic.

## 7. Recommendations

1. Always pack with `k* = argmin` (LPT); never fixed k. Free-fill slack with next-most-likely tests.
2. Treat heavy tests (tpcds, max_nodes, generator_memory, test_tpcds, test_qualify_columns, …) as explicit binary
   decisions driven by patch features; everything else is cheap — be generous within one worker (~2–4 s).
3. Default safety net for code edits in dialect/parser/generator files: own dialect test file + light half of
   test_optimizer (~2 s + makespan ≈ 4–8 s) — H2-style recall fixed most misses; ary6-type (parser core) needs
   test_parser light tests and test_expressions too.
4. Spend the miss budget on recall: in the final, a miss ≈ 100 commits' worth of a 3 s safety net.
