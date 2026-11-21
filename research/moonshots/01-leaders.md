# Moonshot 01: reverse-engineering the leaders (126–140 s on practice)

## Verdict

- **The leaders are not near the oracle. Nothing exotic is needed to explain them.**
  - Practice oracle, rebuilt from our labels: 39 non-slow commits × 1.55–2.28 s, plus one commit that needs a slow test (4–37 s). That gives **≈ 65–115 s, central ≈ 95 s**.
  - The sample's 3.15 s/commit is inflated: 2 of its 30 commits need test_execute_tpcds, 51 of 94 s. Practice has only one such commit.
  - So leaders = oracle + 10–60 s. We are at oracle + ~100 s.
- **Most likely leader recipe:**
  - selection at statement or line level, not function level;
  - robust handling of class-body and import-time edits (a plain coverage selector would not get 0/40, see §3);
  - very little slow-test waste on practice, from verification or from luck;
  - low CPU.
- **Copy now (legal, measured):**
  - **(1) Statement-level selection for ALL tests in function bodies.** We use it today for slow tests only. Measured −24 s per 40 practice-style labeled commits (−8%), 0 new misses on 205. The coverage map is hash-seed stable on 204/204 traced tests.
  - **(2) TPC fixture-entry rule.** −8 s per 40 expected, high variance.
  - **(3) Run-time CPU 4.9 → ~2 s.** About −3 s.
  - Realistic practice: **194 → ~165–175 s.** Closing the remaining ~40 s needs better slow-test verification, not a new paradigm.
- **What blows up for leaders in the final:** any recipe that gets its cost from (i) plain coverage or (ii) a "slow test only if it ran the line" gamble.
  - Final kinds (import / syntax errors, helpers, deletes, renames) defeat plain coverage at 22–100% per commit.
  - The line-only slow rule misses 1.5% of practice-style commits. That is ≈ 0.9 misses ≈ **+263 s expected** over 60 final commits, more than the whole leader-to-us gap.
  - Leaders with real robustness (as we have) will not blow up. My probability that ≥ 2 of the current top 5 take ≥ 1 miss in the final is about 50%. That is a guess anchored on the numbers above.

## 1. Where the practice seconds go (us vs. leaders)

| Component (40 practice commits) | Us (S13) | Leader @126 | Source |
|---|---|---|---|
| Oracle | ≈ 95 (65–115) | ≈ 95 | labeled non-slow oracle mean 1.55 (sample), 1.86 (private), 2.28 (practice_like) s/commit |
| Non-slow sim (S11 diag., incl. its oracle part) | 127.9 | ? | S11: drop all ≥ 3 s tests → 427.9 − 300 |
| Slow tests (needed one + waste) | 61.2 | s (4–29) | 189.1 − 127.9 |
| CPU | 4.9 | c (≈1–2?) | board |

- Leader ≈ 127.9 + s + c − Δ, where Δ is their non-slow advantage over us. Two branches:
  - **Needed slow test is tpch or optimized_tpch (s ≈ 4–6 s):** Δ ≈ 5–10 s. The leaders are essentially us with zero slow waste.
  - **It is tpcds or max_nodes (s ≈ 23–31 s):** Δ ≈ 25–35 s. They are also much cheaper on non-slow selection, which fits statement-level selection (§2).
- **Cheapest discriminator (recommend; I did not spend a slot):** one diagnostic submission that drops only tests ≥ 5 s (tpcds, max_nodes, genmem) and keeps tpch and optimized_tpch.
  - 0 misses means the needed test is cheap, so the remaining gap is slow waste.
  - 1 miss means the needed test is tpcds or max_nodes, so the gap is mostly non-slow selection.

## 2. Candidate leader strategies, scored with our data

Score impact is per 40 practice commits. Final risk is for 60 final commits at 300 s/miss.

| Strategy | Practice score it explains | Final risk | Evidence |
|---|---|---|---|
| (a) Heavy run-time execution (run the candidates, output only failures) | cannot reach 126. Best exec policy = our static score at f ≥ 1 (f = scorer CPU / recorded s) | low (exec only prunes) | Model on 205 practice-style labeled commits (`ours205.py`): exec-everything 451–754 s/40 vs static 304.7 at f = 1–2.5; best threshold policy saves ≤ 0.4 s/40. Static output parallelises (cost = max worker + 2k), exec pays Σ. Wins only for sets < ~1.5 CPU-s with no failure. Report 03: ~4 s. |
| (b) Slow-test gamble: "slow test only if it ran the changed line" | explains the low slow cost only if practice was lucky. The rule still wastes 139 s/40 on practice-style labeled data | **0.015 miss/commit → 0.9 misses → +263 s** (practice-style); 13%/commit on other kinds | `slowrisk.py` (below) |
| (b') "never run slow tests" | impossible: 1 practice commit needs one (S11) | 6 misses → +1,840 s | – |
| (c) Mutation kill matrix (patch/site → failures). **Banned.** | ≈ oracle + CPU ≈ 65–115. Leaders at 126+ is weak evidence against a complete key | DQ if read ("top entries are read"); fails on new kinds | – |
| (d1) **Statement-level selection everywhere** (we do it only for slow tests) | −24 s/40 on labeled; −45 s on 24 practice_like commits | ≈ 0 if guarded (below) | `stmt3.py` |
| (d2) Fixture-entry knowledge for TPC fixtures | −0…−25 (≈ −8 expected) | ≈ 0 | `tpcfix.py` |
| (d3) Low CPU | −3 | 0 | board CPU 4.9 s vs 1.3 s at S05 |

### (b) Slow-rule risk, measured

- Population A (practice-style): sample + practice_like + private {code, fixture, comment, multi}, n = 205.
- Population B: every other labeled kind, n = 265.
- Non-slow selection is assumed perfect. A miss counts only failing slow tests left out.

| Policy | A misses (per commit) | A slow waste /40 | B misses (per commit) |
|---|---|---|---|
| line: ran an exact changed line, or reads the fixture | 3 (0.015): 55em, tanh (tpcds), tiyl (genmem) | 139 s | 35 (0.13) |
| stmt: ran the changed statement (our r2 rule, before verification) | 1 (0.005): tiyl, import-time | 240 s | 32 (0.12) |
| never | 21 (0.10) | 0 | 40 (0.15) |

- P(slow test fails | it ran the exact changed line), population A (`slowstatsA.py`):
  - tpcds 15/36 = 0.42
  - tpch 3/8
  - optimized_tpch 2/8
  - max_nodes 1/5
- Dropping a line-covered tpcds therefore costs 0.42 × 300 = 120 s expected, against a 21.5 s saving. No rational static rule drops it. Only verification can (our r17/r18/r20).
- **Conclusion:** a leader with low slow cost on practice either verifies (as we do) or got lucky on practice. The lucky kind pays ≈ 0.3–0.9 misses in a 60-commit final.

## 3. Why the leaders are not plain coverage, and what that does to them in the final

- Plain selector: statement-level in functions, file tests for class/module bodies, fixture readers, whole test file for test edits. Labeled miss rate per commit (`naivekinds.py`):
  - **code 15/124 (12%)**
  - fixture 0/35, comment 0/24, multi 0/22
  - rename 2/9, dictentry 2/9
  - **deletefile 4/7, helper 6/10, syntaxerr 4/6, importerr 6/6**
  - adversarial 21/46, adversarial2 24/65
- With ~25 code commits in practice, P(0 misses) ≈ 0.88^25 ≈ 4%. The leaders, at 0/40, almost surely handle class-body, import-time and dialect-reach edits.
- Their final exposure is the new kinds. If the final has ~15 non-practice-kind commits, a team without import / collection-error / deletion / helper rules expects several misses (≥ 900 s).
- Our 0/446 labeled (111 adversarial) is the moat. Do not trade it for practice seconds.

## 4. What we should copy (legal)

### 4.1 Statement-level selection for non-slow tests (highest value, about 0.5 day)

- **Rule.** For a clean in-place edit inside a function body, select only the tests whose lines map ran a line of the touched statement. For a compound statement whose header changed, that means the header lines; our `classify.touched_statements` already computes this for the slow rule.
- **Today.** We use the whole function's tests for every non-slow test (`_library` → function map). The "function map" mechanism is the largest gap item in `eval/gap.py` (308 s / 120 practice-style commits).
- **Measured** by taking our real outputs and removing only function-but-not-statement tests (`stmt3.py`; fixture and test-file contributions kept):

  | | Ours | Statement-level |
  |---|---|---|
  | All tests, per 40 | 304.7 | 280.5 |
  | Non-slow only, per 40 | 252.3 | 228.0 |
  | New misses | – | **0** (n = 205; recall check over all labeled sets: stmt-level misses = function-level misses, 3/107 + 7/36, all import-time) |

  Biggest wins: ehpl 17.5 → 2.0, ekhw 17.4 → 3.4, xvab 11.1 → 2.3, dmei / 31h2 / 55bc 9.3 → 2.0, q4bl / r007 / nnfp 8.8 → 2.1.
- **Why it is sound.**
  - A one-token change on statement S changes behaviour only in a process that executes S.
  - Two exceptions are function-wide effects, so guard them: compile old and new def, and if `co_varnames`, `co_cellvars`, `co_freevars`, `co_flags` (generator / coroutine) or `co_argcount`-like fields differ, use the function rule. Cheap.
  - Short-circuited continuation lines (2jrq) are covered, because any line of the statement counts.
  - **Pure insertions (an added line):** select the tests of the PREVIOUS statement in the same block, or of the block header if the insert is first in its block (∪ the next statement).
  - My emulation (`stmt_span`) used the next statement only, which is not a superset of the tests that reach the insertion point. The 0-miss evidence is almost all one-token replacements, so it does not test inserts.
  - Reuse `classify.touched_statements`; do not port `stmt_span`.
- **Map determinism.**
  - Traced 204 random non-slow tests under PYTHONHASHSEED = 1 and 2 on the laptop and compared them with the shipped map (seed 0, pinned image). There were **0 differing sqlglot lines in 204/204 tests** (avg 1,528 lines each; `seeds.py`, `seedcmp.py`).
  - Builds mapsA vs mapsC differ only in `executor/*` (288 keys, one-sided additions from a tracer change). Keep the function rule in `sqlglot/executor/*` or union both maps.
- **Practice estimate: −10 to −25 s** (sample −3.5/30; private −20/40; practice_like −74/40; practice sits between).

### 4.2 TPC fixture-entry rule (about 2 h)

- **The waste.** `tests/fixtures/optimizer/tpc-h/tpc-h.sql` edits select test_execute_tpcds (21.5 s), because both .sql files are read in `TestExecutor.setUpClass` and reads are widened to the class. tpcds failed in 0/3 labeled tpc-h fixture commits (17ns, qp5p, v2bv: 25.5 s ours vs 5.9–7.9 oracle).
- **Code facts** (tests/test_executor.py, tests/helpers.py):
  - the execute tests use only `sql` (`_, sql, _`), never the expected half of a pair;
  - `_extract_meta` raises only on a `#` line without `:`;
  - an unexecuted query only goes through `parse_one`.
- **Rule.**
  - tpc-h edit: drop tpcds unless the edit can make `load_sql_fixture_pairs` raise (`#` line without `:`) or changes the `;` pairing, which means the whole class.
  - Expected-half edit: only test_optimized_tpch / test_optimizer readers.
  - Query-half edit: re-run that one query through r17's store (≈ 0.2 s CPU).
- 2% of practice-style commits touch tpc fixtures (P 12/600, Q 14/1000, labeled 5/246), so ≈ −8 s per 40 in expectation.

### 4.3 Also

- CPU back toward ~2 s (−3 s).
- 28% of our labeled waste (266 s / 45 commits) sits in commits where diffexec says "not recorded (budget)". A bigger store would pay, but it is bounded by the 20 MB repo and the 10-min build. That is another agent's angle.

## 5. What NOT to copy

- Do not copy a line-only or "never" slow policy, or any drop of a slow test that ran the touched statement without verification. 42% of those fail.
- Do not ship a kill matrix: banned, and it is also an answer key for the final's mutant family.
- Do not copy whole-candidate run-time execution: CPU ≥ the parallel static cost.

## 6. Smallest validating experiments

1. **Statement-level:** implement it in `_library` with the code-object guard. Run `eval/evaluate.py` on all labeled sets plus the r4 mutation-probe harness: 40 random function-body edits, full suite. Expect 0 misses and −20 to −25 s per 40 on private. Then submit: expected board −10 to −25 s.
2. **Diagnostic submission:** drop only tests ≥ 5 s. This shows whether the leaders' edge is slow waste or non-slow selection.
3. **TPC fixture rule:** unit-test on 17ns / qp5p / v2bv / d5qt / db6n plus 10 generated tpc fixture mutants, labeled with the full suite.

Scripts (scratch, reproducible): `/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m01/`
- `oracle.py`, `naive.py`, `ours.py`, `stmt.py`, `stmt3.py`, `slowrisk.py`, `naivekinds.py`, `tpcfix.py`, `reasons.py`, `seeds.py`, `seedcmp.py`
- our outputs: `our_*.jsonl`
