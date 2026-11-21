# Moonshot 06: sub-test decomposition for all expensive tests

**Verdict.** The full generic mechanism (split every 1–30 s test into phases at build time,
record frontier calls, re-run reached phases and replay the rest at run time) is not worth
building. Its zero-CPU special case is a quick win, measured and **implemented on this branch**:
the *touched-statement* rule for every test (`STMT_SECONDS = 0`), not only tests ≥ 5 s.

- Misses: 0 on 440 labeled patches (private 264, private41, adversarial, adversarial2,
  practice_like, sample).
- Cost per 40 commits (only commits whose statement-rule outcome changed): private −13.4 s,
  P −9.0, Q −14.4; sample −1.5 s total once diffexec timing noise is removed.
- Hash seeds 0/1/2 re-trace: none of 9135 dropped (commit, test) pairs runs a changed line.
- Run-time CPU not higher (selector process on sample, laptop: 1.68 s main, 1.0–1.2 s new;
  forked children not counted). Unit tests updated, 169 pass. Scripts: `research/moonshots/06/`.

What remains for real phase re-run / frontier replay after that: ≤ ~2 s/40 (function-body
edits) + ~5 s/40 (class/module-state edits, where a patched-import child costs ~0.7 s scorer CPU
per commit ≈ break-even). The bigger prize is still `test_execute_tpcds` refused by subtest on
CPU; that needs a different fix (§4).

**Expected practice gain:** 3–8 s/40; P(works, no new misses) ≈ 0.7. Not a moonshot.

## 1. Seconds at stake (main selections, re-packed with pack.py after dropping)

Tests ≥ 1 s besides the 5 handled: test_scope 2.5, test_tpcds (optimizer) 2.2,
test_annotate_funcs 2.0, test_qualify_columns 1.4, test_agg_order 1.4, test_optimize_in_nulls
1.3, test_cache_does_not_retain_queries 1.3, test_merge_subqueries 1.0, test_canonicalize 1.0;
six more at 0.5–1 s.

| Set | Cost /40 | Drop all mid (1–3 s), ignore misses | Oracle: drop *passing* mid / mid+0.5–1 s | Oracle: passing handled-5 |
|---|---|---|---|---|
| P (600, unlabeled) | 327.6 | 25.8 | – | (113.7 if all passed) |
| Q (1000, unlabeled) | 314.0 | 25.8 | – | (94.1) |
| private (264) | 367.7 | – | 11.1 / 15.7 | 42.1 |
| sample (30) | 186.9 | 17.3 | 4.4 / 7.1 | 28.6 |
| practice_like (24) | 397.9 | – | 42 / 52 (outlier: 2x board cost) | 72 |

- P/Q mid-tier is an upper bound: on labeled sets 40–50 % of selected qualify_columns /
  annotate_funcs / tpcds actually fail.
- Fixture-edit commits always fail their reader test (sample 8/8): not droppable.
- At the private/sample ratio (~4 % of cost), mid-tier is worth ≈ 5–10 s/40 on practice even
  with a perfect oracle.

## 2. Reachability (build-time trace)

`06/frontier_plugin.py` (pytest plugin, sys.monitoring, process pools made synchronous) records
per test every frontier call (tests/ frame → sqlglot function) with its line set and CPU.
Against each commit's changed lines, the *passing* selected mid-tier tests reach on average
0–7 % of their sqlglot CPU (annotate_funcs, qualify_columns, snowflake, scope, simplify, serde,
merge_subqueries) or 14–45 % (tpcds-optimizer, optimize_in_nulls, agg_order, correlated_all).
About half reach **no** changed line at all. Passing mid-tier saving on private by reason:
function-body edits 7.1 s/40, class/module-state edits (Tokenizer settings etc.; no code swap)
4.9 s/40, other 1.3 s/40.

## 3. The zero-CPU form: statement-level reach for every test (measured)

The selector already kept a ≥ 5 s test only if the lines map shows it ran a *touched statement*
(`_slow_tests_off_statements`). The change gives that rule its own threshold `STMT_SECONDS`;
`self._slow` stays at 5 s because it also feeds the import-state `slack` gate (lowering both
was a confound: +11 s on private).

| Set | Main | Threshold 0.5 s | Threshold 0 | Misses |
|---|---|---|---|---|
| private (264) | 2427.1 | 2410.2 | **2338.4** (−13.4/40) | 0 / 0 |
| private41 | 388.9 | 380.4 | **373.2** (−15.3/40) | 0 / 0 |
| adversarial (46) | 1063.2 | 1063.2 | 1063.2 | 0 / 0 |
| adversarial2 (65) | 937.6 | 932.0 | **922.7** (−9.2/40) | 0 / 0 |
| practice_like (24) | 238.8 | 214.1 | **171.7** (−112/40) | 0 / 0 |
| sample (30) | 140.2 | 117.3* | 117.2* (rule: −1.5 s) | 0 / 0 |
| P (600) | 4913.4 | 4859.1 | **4777.0** (rule −9.0/40, noise 0.0) | n/a |
| Q (1000) | 7849.5 | 7715.5 | **7510.4** (rule −14.4/40, noise +0.8) | n/a |

\* Sample 66ks: the 21.45 s drop of test_execute_tpcds is **diffexec noise** (main "dropped 46,
subtest keep", variant "dropped 47", same rule outcome). Diffexec/subtest decisions vary from
run to run — worth checking on its own.

Where the gain comes from: core functions with many callers, edit in a rarely-taken branch.
practice_like ehpl edits `TokenizerCore._add` under `if self._comments and token_type ==
SEMICOLON`: 1542 tests → 2.

**Soundness evidence.**
1. 0 misses on all six labeled sets.
2. Independent full-suite re-trace (`06/target_plugin.py`) under PYTHONHASHSEED 0, 1, 2 of the
   changed lines of the 55 labeled commits where threshold 0 drops tests: none of 9135 dropped
   pairs runs a changed line under any seed; per-line hit sets identical across seeds.
3. Scoping edits are already guarded: `classify.touched_statements` returns None (→ whole-function
   rule) when a touched statement binds different names (`x=` → `exp=`, `:=`), adds
   global/nonlocal, adds/removes `yield`, statements are added/removed, a nested def header
   changes, or the def writes state that outlives the call (caches). Covered by
   `test_slow_test_kept_when_the_edit_changes_name_bindings`.

**Residual risks.** Branches that depend on worker order or cache state (the map is one
suite-order run; memoization is already a shared risk of the function rule). A multi-line
statement whose changed line emits no LINE event (mitigated: whole-statement spans). Main's
reason for keeping cheap tests at the function rule ("absorb line-level noise") was never
measured; these are its first numbers.

## 4. What is left for real phase re-run / frontier replay

After threshold 0, private's passing mid+semi stake is mostly class/module-state (32.5 of
59.7 s → ~5/40).

- **Function-body edits (~2/40, mostly "not recorded (budget)").** A generic verifier would
  re-run each *fixture entry / subTest* reaching the change under swapped code and compare its
  outputs to pinned values. The unit must be the entry, not the frontier call (`.sql()` /
  `walk()` consume the reached `optimize` output). ≤ 0.2 s scorer CPU per test (reach ≤ 7 %),
  but pinned per-entry outputs for ~15 tests is a lot of work for ~2 s.
- **Class/module-state edits.** Need a patched-import child (genmem's `_PatchedFinder`):
  ~0.35 s EC2 ≈ 0.7 s scorer + reached entries, against a typical re-packed saving of
  0.4–1.2 s per commit → nets ≈ 0.
- **Handled tests: the biggest remaining stake.** test_execute_tpcds "subtest: keep (est ~13.6 s
  CPU for 57 queries)": 62/600 P, 87/1000 Q, 5/264 private passing, 1/30 sample (66ks). The
  changed function (Context.table, annotate_expression, optimize …) runs in every executed
  query, so re-running reached queries *is* re-running the test; decomposition cannot help.
  What could: a **build-time, uncapped** diffexec recording of the TPC hot-path functions in
  the Docker build (image size unlimited → lifts the 13 MB "not recorded (budget)" gate).
  Separate moonshot; build-time cost (recording a 21 s test, 4 cores, 10 min) unmeasured.

## 5. Soundness gates (if anyone builds the phase verifier anyway)

1. Code swapping only for clean function-body edits; import-time / class-state effects need a
   patched-import child.
2. Unit = fixture entry / subTest; all its outputs compared type-strict to pinned values.
3. Build time: digest sqlglot module/class state before/after each entry; any cross-entry write
   (pool memos, lru_cache, `@patch` mocks) → test not decomposable.
4. Phases outside entries (setUpClass, post-loop asserts) run for real or covered by line reach.
5. CPU charged at 2x, capped at a share of the saving; replays inside the existing diffexec fork.

## 6. Recommendation / smallest validation

Merge moon-06's `selector/select.py` (`STMT_SECONDS = 0` for `_slow_tests_off_statements`,
`SLOW_SECONDS = 5` kept for `slack`) and the two updated unit tests. Before merging: run
`eval/evaluate.py` on every labeled set in the pinned image and `eval/ab.py` on P/Q; optionally
rebuild `lines` with a second hash seed and union it in. Seed check re-run:
`06/mktargets.py` (changed lines of commits whose selection drops tests) → `06/target_plugin.py`
(`-p target_plugin`, `PYTHONHASHSEED=0/1/2`, full suite) → `06/checkseeds.py`.
Expect −3 to −8 s/40 on practice-style data, no added CPU, no measurable added miss risk.
