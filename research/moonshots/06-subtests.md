# Moonshot 06: sub-test decomposition for all expensive tests

**Verdict.** The full generic mechanism is not worth building: split every 1–30 s test into
phases at build time, record frontier calls, then at run time re-run the phases that reach the
change and replay the rest. Its zero-CPU special case is a quick win, and we measured it.

That special case is `SLOW_SECONDS → 0` for the *touched-statement* rule, applied to every test
instead of only tests ≥ 5 s. Results:

- Misses: 0 on 440 labeled patches (private 264, private41, adversarial, adversarial2, practice_like, sample).
- Cost per 40 commits, counting only commits whose statement-rule outcome changed:
  −13.4 s on private, −9.0 on P, −14.4 on Q.
- Sample: −1.5 s once diffexec timing noise is taken out.
- Run-time CPU: not higher. Selector process CPU on sample, laptop: 1.68 s on main, 1.0–1.2 s
  on this change. Forked children are not counted.
- **Already implemented on this branch:** `selector/select.py` `STMT_SECONDS = 0.0`. Unit tests
  are updated (169 pass). The scripts are in `research/moonshots/06/`.
- Hash seed: across 3 hash seeds, no dropped (commit, test) pair runs a changed line (9135 pairs).

After that rule, the work left for real phase re-run or frontier replay is ≤ ~2 s/40 for mid-tier
tests whose edit is inside a function body, and ~5 s/40 for class/module-state edits, where the
replay would cost ~0.7 s of scorer CPU per commit. That is roughly break-even.

The bigger remaining prize is still the hand-handled `test_execute_tpcds`, where subtest refuses
on CPU. That needs a different fix (§4).

**Expected practice gain:** 3–8 s per 40 commits, P(works, no new misses) ≈ 0.7. A moonshot it is not.

## 1. Seconds at stake (current main selections; scripts in scratchpad `m06/`)

Tests ≥ 1 s besides the 5 handled ones:

| Test | Seconds |
|---|---|
| test_scope | 2.5 |
| test_tpcds (optimizer) | 2.2 |
| test_annotate_funcs | 2.0 |
| test_qualify_columns | 1.4 |
| test_agg_order | 1.4 |
| test_optimize_in_nulls | 1.3 |
| test_cache_does_not_retain_queries | 1.3 |
| test_merge_subqueries | 1.0 |
| test_canonicalize | 1.0 |

Another six tests run 0.5–1 s.

Savings below are per 40 commits, with each commit re-packed using pack.py after dropping tests.

| Set | Cost /40 | Drop all mid (1–3 s), ignore misses | Oracle: drop *passing* mid / mid+0.5–1 s | Oracle: passing handled-5 |
|---|---|---|---|---|
| P (600, unlabeled) | 327.6 | 25.8 | – | (113.7 if all passed) |
| Q (1000, unlabeled) | 314.0 | 25.8 | – | (94.1) |
| private (264) | 367.7 | – | 11.1 / 15.7 | 42.1 |
| sample (30) | 186.9 | 17.3 | 4.4 / 7.1 | 28.6 |
| practice_like (24) | 397.9 | – | 42 / 52 (outlier: 2x board cost) | 72 |

How to read the table:

- The P/Q mid-tier figure is an upper bound. On labeled sets, 40–50 % of selected
  qualify_columns, annotate_funcs and tpcds actually fail.
- Every fixture-edit commit in sample fails its reader test (8/8), so fixture stakes cannot be dropped.
- Applying the private and sample ratios (~4 % of cost) to practice: the mid-tier tests are worth
  ≈ 5–10 s/40 even with a perfect oracle.

## 2. Reachability (build-time trace)

`m06/frontier_plugin.py` is a pytest plugin. It uses sys.monitoring, with the process pools made
synchronous. Per test, it records every frontier call (a tests/ frame calling a sqlglot function)
with its line set and CPU.

Applied to the changed lines of each commit, the *passing* selected mid-tier tests reach a mean of:

| Reached share of the test's sqlglot CPU | Tests |
|---|---|
| 0–7 % | annotate_funcs, qualify_columns, snowflake, scope, simplify, serde, merge_subqueries |
| 14–45 % | tpcds-optimizer, optimize_in_nulls, agg_order, correlated_all |

About half of the selections reach **no** changed line at all.

Breakdown of the passing mid-tier saving on private:

| Selection reason | Per 40 commits |
|---|---|
| Function-body edits (diffexec ran) | 7.1 s |
| Class/module-state edits (Tokenizer settings and similar; no code swap possible) | 4.9 s |
| Other | 1.3 s |

## 3. The zero-CPU form: statement-level reach for every test (measured)

The selector already keeps a ≥ 5 s test only if the lines map shows it ran a *touched statement*
(`_slow_tests_off_statements`).

Variant `v05b` gives that rule its own threshold (`STMT_SECONDS`). `self._slow` stays at 5 s,
because it also feeds the import-state `slack` gate. Lowering both together was a confound: +11 s
on private.

| Set | Main | Threshold 0.5 s | Threshold 0 | Misses |
|---|---|---|---|---|
| private (264) | 2427.1 | 2410.2 | **2338.4** (−13.4/40) | 0 / 0 |
| private41 | 388.9 | 380.4 | **373.2** (−15.3/40) | 0 / 0 |
| adversarial (46) | 1063.2 | 1063.2 | 1063.2 | 0 / 0 |
| adversarial2 (65) | 937.6 | 932.0 | **922.7** (−9.2/40) | 0 / 0 |
| practice_like (24) | 238.8 | 214.1 | **171.7** (−112/40) | 0 / 0 |
| sample (30) | 140.2 | 117.3* | 117.2* (real gain −1.5 s) | 0 / 0 |
| P (600) | 4913.4 | 4859.1 | **4777.0** (rule −9.0/40, noise 0.0) | n/a |
| Q (1000) | 7849.5 | 7715.5 | **7510.4** (rule −14.4/40, noise +0.8) | n/a |

\* Sample 66ks: the 21.45 s drop of test_execute_tpcds is **diffexec noise**, not this rule.

- Main runs: "dropped 46, subtest keep".
- Variant runs: "dropped 47". The rule is the same; only the timing-sensitive replay result differs.
- So diffexec/subtest decisions vary from run to run. That is worth checking on its own.

Where the threshold-0 gain comes from: core functions with many callers, where the edit sits in a
rarely-taken branch. For example, practice_like ehpl edits `TokenizerCore._add`, which is behind
`if self._comments and token_type == SEMICOLON`. Its selection goes from 1542 tests to 2.

**Soundness evidence:**

1. 0 misses on all six labeled sets.
2. Independent re-trace (`m06/target_plugin.py`) of the full suite under PYTHONHASHSEED 0, 1 and 2:
   - It covers the changed lines of the 55 labeled commits where threshold 0 drops tests.
   - None of the 9135 dropped (commit, test) pairs runs a changed line under any seed.
   - The set of tests hitting each line is identical across seeds.

3. Scoping edits are already guarded. `classify.touched_statements` returns None, which falls
   back to the whole-function rule, in these cases:
   - a touched statement binds different names (`x=` → `exp=` makes `exp` local everywhere,
     including `:=`);
   - it adds `global` or `nonlocal`;
   - it adds or removes a `yield`;
   - statements are added or removed;
   - a nested def header changes;
   - the def writes state that outlives the call (caches).

   Unit test `test_slow_test_kept_when_the_edit_changes_name_bindings` covers the first case.

**Residual risks:**

- Branches that depend on worker order or cache state. The map comes from one suite-order run;
  memoization is already a shared risk of the function rule.
- A multi-line statement whose changed line emits no LINE event. `touched_statements` takes
  whole-statement spans, which mitigates this.
- Why main kept cheap tests at the function rule ("absorb line-level noise in the map"): this
  rationale was never measured. These numbers are its first measurement.

**CPU:** no new work. It uses the same map shards that are already loaded for the function
span, and diffexec gets fewer tests to consider. Measured above: not higher.

## 4. What is left for real phase re-run / frontier replay

After threshold 0, private's passing mid+semi stake is mostly class/module-state edits (32.5 s of
59.7 s → ~5/40).

- **Function-body edits:** the remainder is ~2/40, mostly "not recorded (budget)".
  - A generic phase verifier would re-run each *fixture entry / subTest* that reaches the change,
    under swapped code, and compare that entry's outputs to the pinned values.
  - The unit has to be the entry, not the frontier call: `.sql()` and `walk()` consume the
    reached `optimize` output.
  - Cost would be ≤ 0.2 s scorer CPU per test (reach ≤ 7 %), but we would have to build pinned
    per-entry outputs for ~15 tests, which is a lot of work for ~2 s.
- **Class/module-state edits:** each one needs a patched-import child (genmem's `_PatchedFinder`).
  - The child costs ~0.35 s on EC2, which is ~0.7 s on the scorer, plus the reached entries.
  - Typical saving per commit after re-packing is 0.4–1.2 s, so this nets roughly 0.
- **Handled tests:** the biggest stake that remains.
  - test_execute_tpcds "subtest: keep (est ~13.6 s CPU for 57 queries)": 62 of 600 P, 87 of
    1000 Q, 5/264 private passing, 1/30 sample (66ks).
  - The changed function (Context.table, annotate_expression, optimize …) runs in every executed
    query, so re-running the reached queries *is* re-running the test.
  - Generic decomposition cannot help. What would help: a **build-time, uncapped** diffexec
    recording of the functions on the hot path of the TPC tests, made in the Docker build.
    The image has no size limit, so this lifts the 13 MB repo-store "not recorded (budget)" gate.
  - That is a separate moonshot. Its build-time cost (recording overhead on a 21 s test, 4
    cores, 10 min limit) is unmeasured.

## 5. Soundness gates (if anyone builds the phase verifier anyway)

1. Only clean function-body edits use code swapping. Any import-time or class-state effect needs
   a patched-import child.
2. The unit is a fixture entry or subTest, and all of its outputs are compared to pinned values
   (type-strict), as in subtest.py.
3. At build time, digest sqlglot module and class state before and after each entry. Any write
   that crosses entries marks the test not decomposable (pool memos, lru_cache, `@patch` mocks).
4. Phases outside the entries (setUpClass, assertions after loops) are run for real or covered
   by line reach.
5. CPU is charged at 2x and capped at a share of the saving; replays go inside the existing
   diffexec fork.

## 6. Recommendation / smallest validation

Merge the moon-06 `selector/select.py` change: `STMT_SECONDS = 0` as a separate threshold for
`_slow_tests_off_statements`, with `SLOW_SECONDS = 5` kept for `slack`, plus the two updated
unit tests.

To re-run the seed check:

1. `research/moonshots/06/mktargets.py` writes the changed lines of the commits whose selection drops tests.
2. `target_plugin.py` re-traces the full suite (`-p target_plugin`, `PYTHONHASHSEED=0/1/2`).
3. `checkseeds.py` reports any dropped test that runs a changed line under any seed.

Before merging:

- Run `eval/evaluate.py` on every labeled set in the pinned image.
- Run `eval/ab.py` on P/Q to confirm.
- Optionally rebuild `lines` with a second hash seed and union it in.

On practice-style data, expect −3 to −8 s/40, at no CPU cost and no added miss risk that we can
measure.
