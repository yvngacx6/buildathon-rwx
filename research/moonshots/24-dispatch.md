# 24 — Dispatch precision: is the "function map" bucket over-selection that a finer static signal can remove?

## Verdict and numbers
- **No sound dispatch signal beats statement coverage. Don't build a (receiver class, method) map.**
  - The bucket splits cleanly in two:
    - **A: half the tests never ran the edited statement.** R23 (branch `r23-stmt`, NOT merged) already removes these.
    - **B: the other half ran it.** Every test that entered the method also executed the edited line.
      Only value-level replay (diffexec) can drop them. On practice_like, diffexec never replays them:
      the functions are dead in the store ("not recorded (budget)").
- **Split of the bucket (exclusive extras, greedy cost, main 3bec6e9 + pinned image; `24-tools/dispatch.py`):**

| set (commits) | A: ran fn, no touched stmt (R23 drops) | C: no stmt view | B: ran touched stmt | B with diffexec dead: budget / calls |
|---|---|---|---|---|
| practice_like (120) | **66.4 s** (≈ 22 s/40) | 2.1 s | **53.5 s** (≈ 18 s/40) | 49.4 s (17 commits) / 4.1 s (4) |
| private (264) | 88.0 s (≈ 13 s/40) | 9.2 s | 47.4 s (≈ 7 s/40) | 30.2 s (21) / 2.2 s (2) |

  - With R23 merged in (`m24-r23` = main + r23-stmt), A falls to 0.2 s and 1.9 s; B and C don't change.
  - practice_like total cost: 915.9 → 849.7 s.
  - The R23 gain belongs to R23, not to this moonshot. It is ready and still unmerged.
- **Instruction-level (bytecode) coverage, run on every B test of practice_like (`itlev.py` + moon-16 `itrace.py`):**
  saves **6.7 s per 120 commits, about 2.2 s per 40**, as an upper bound.
  - Two commits make it all: tggb 4.7 s and d0xm 1.8 s.
  - This matches moon-16's ≈ 2 s/40. It is not worth a map.
- **The lever for B is diffexec store coverage, not dispatch.** 1396 of the 1398 B tests on practice_like were never replayed.
  - Functions dead for budget: `Generator.datatype_sql`, `Create.kind`, `DuckDB.column_alias_precedence`,
    `Oracle.can_quote`, `Parser._pivot_column_value_name`, `decorrelate`, `optimize`, `_is_reorderable`, …
  - Dead for the call cap: `dump`, `sql_and`, `simplify_datetrunc`, …
  - The upper bound is ≈ 16 s/40 practice commits, if every budget-dead function were recorded and replay dropped
    every non-failing test. Realistically ≈ 8–12 s/40, after replay CPU and "differs" verdicts.
- **P(a dispatch / receiver-pair rule gives > 1 s/40 soundly) ≈ 0.05.**
  - P(instruction-level is worth building) ≈ 0.1, for ≤ 2 s/40.
  - Recommended actions, in order:
    1. Merge R23: −22 s per 40 practice_like commits, measured here.
    2. Hand the B residual (≈ 16 s/40, 92 % of it budget-dead) to whoever owns the diffexec store.

## 1. Why the gap.py bucket reads 145.5 s here and not ≈172 s
`gap.py` on practice_like at main 787921b (= 3bec6e9 for selector code) credits 145.5 s to "function map" (32 commits). Another 35.9 s goes to
`shared: function map + …`, mostly import-time map + function map on ehpl and 4gr2. Exclusive + shared is ≈ 181 s, over 36
commits; the ≈ 172 s in the task is the same bucket on an older HEAD. My split only counts **exclusive** function-map extras (122 s);
the shared ones also need the other rule relaxed (ehpl / 4gr2: the import-time runs of `TokenizerCore._add`,
`_parse_placeholder`). The index was built without slowverify.pkl / resources.pkl (OOM on the shared box). Those only touch
slow tests and resource-class tests. Neither is in this bucket.

## 2. Why receiver-class / dispatch pairs cannot beat statement coverage
- **Base method edited, subclass overrides without `super()`:** the subclass tests never execute the base
  statement. The per-test lines map already shows that, and R23 drops them.
- **Override calls `super()`, or the test dispatches the base through any dialect:** the base statement runs, so the
  edited code executes. A pair map would keep the test too.
- **Pair maps are therefore strictly coarser than statement coverage** of the method body. Whatever they could drop,
  R23 drops already.
- **Data, R23 view.** For the big B rows, every test that ran the function also ran the touched statement
  (ran / kept / ran-stmt):

| commit | function | ran / kept / ran-stmt | B cost |
|---|---|---|---|
| z8gc | `Generator.datatype_sql` | 403/403/403 | 10.8 s |
| gv9h | `Create.kind` | 96/96/96 | 6.3 s |
| d0xm | `DuckDB.column_alias_precedence` | 49/49/49 | 5.8 s |
| 1o11 | `Oracle.can_quote` | 97/97/97 | 4.2 s |
| q407 | `_pivot_column_value_name` | 38/38/38 | |
| 1k2u | `_is_reorderable` | 94/94/94 | |
| hjcs | `optimize` | 94/94/94 | |
| 06t7 | `_parse_derived_table_values` | 436/436/436 | |

  The edits are one-token mutants at the top level of the method body (`and`↔`or`, `is`↔`==`, dropped `not`, …),
  so every caller executes them.
- **Dialect-scoped methods** (DuckDB.*, Oracle.*, DatabricksGenerator.*) are already dispatched to only that dialect's tests.
  The extras are that dialect's tests that ran the line and still pass.

## 3. Per-dialect branch conditions (`if self.dialect.X and <edited>`) = instruction level
- **The signal.** When an edited operand sits behind a short-circuit on a dialect flag, tests on the other dialects run the
  statement but skip the edited instruction. That is exactly what instruction-level coverage sees.
- **How it was measured.** `idiff.py` (from moon-16) finds which OLD bytecode instructions differ. `itrace.py` runs
  `sys.monitoring` INSTRUCTION events on those code objects only. Every B test was traced in the pinned image, plus the failing tests
  as a positive control.

| commit | B tests | ran changed instruction | droppable | seconds saved |
|---|---|---|---|---|
| tggb | 14 | 2 | 12 | 4.7 |
| d0xm | 48 | 17 | 31 | 1.8 |
| dnc2 | 7 | 6 | 1 | 0.2 |
| 1k2u | 90 | 25 | 65 | 0 (slow tests dominate) |
| nfoh | 32 | 11 | 21 | 0 |
| z8gc, gv9h, 1o11, hjcs, lxx5, j9oq, … | all | all | 0 | 0 |
| q407 / 06t7 / 7zco | | | 0 | 0: the edited code also runs before any test (import / collection), no drop |
| **total** | | | | **6.7 s / 120 commits** |

- **Positive control.** Every failing test of a single-file commit hit a changed instruction. The one
  `FAILING-NOT-HIT` is r1hk, a multi-file commit whose failures come from its fixture file.
- **Caveat.** One process per job, so a lazy cache filled by an earlier test can hide a later test's hit. Treat the
  6.7 s as an upper bound. I did not run per-process confirmation, because I am not proposing the rule.
- **Private.** Instruction-level numbers are in `24-tools/out/it_private.txt` (same tool) if present. Moon-16 measured
  6.9 s over 71 labeled function-body commits on the same idea.

## 4. What B actually needs: diffexec store coverage
- **Where B sits.** practice_like B = 53.5 s, of which 49.4 s is in functions that `diffexec/build.py choose()` dropped for the
  13.5 MB store budget. Another 4.1 s hit the 50 k call cap. On private, 30.2 of 47.4 s is budget-dead. The rest of private's B
  was replayed and kept ("differs" 112–114, "incomplete" 66–74).
- **Why the budget misses these functions.** `choose()` weights functions by `lines(f) * saving ** 1.5`. That favours long
  functions with expensive callers. Practice-style mutants land on boolean / comparison tokens of medium-size hot methods
  (`datatype_sql`, `can_quote`, `Create.kind`, …), which cost many bytes because they have many callers.
- **Options. Not built here: a store re-record is ≈ 26 min on 14 workers, which breaks the 8-CPU cap.**
  1. Re-weight `choose()` by mutable-token count, not lines.
  2. Keep only the *return-value digest* for hot short functions. Their inputs are what cost bytes. For a
     hot function, an input-elided store could hold "test X's calls all had result digest D". The selector still needs inputs
     to replay, so this needs design work.
  3. Spend the remaining ≈ 2.4 MB of repo headroom (tree 17.6 MB, limit 20 MB) on the functions with the largest
     `B seconds / bytes` here. In order: datatype_sql, Create.kind, column_alias_precedence, can_quote, decorrelate.
- **Sizing.** ≈ 16 s/40 upper bound, ≈ 8–12 s/40 realistic. This is the moon-12 / diffexec owner's call.

## 5. Files
- `24-tools/dispatch.py`: wraps the selector (main or R23) and splits exclusive function-map extras into A / B / C
  by touched-statement coverage, with each B test's diffexec verdict.
- `24-tools/itlev.py`: builds idiff specs and test lists, then scores itrace hits into seconds. `idiff.py` and `itrace.py`
  are copied from moon-16.
- `24-tools/out/`: the raw outputs (`d_m24.txt` = main, `d_m24r.txt` = main + R23, `it_practice_like.txt`, gap.py output).
- **EC2:** `~/work/m24` (main) and `~/work/m24r` (main + r23-stmt); results in `~/work/m24out/`.
