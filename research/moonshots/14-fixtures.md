# Moonshot 14: fixture edits, compared through what each reader consumes

## Verdict

**Build it. It is a small, sound and cheap rule, not a moonshot.**

- **Saving:** about −8 s per 40 practice-style commits in expectation (range −5 to −13 across sets).
  - Almost all of it comes from TPC fixture edits, about 2% of commits, each worth −18 to −21 s.
  - Variance is high: a given 40-commit set holds 0, 1 or 2 such commits.
- **Final:** about −12 s expected over ~60 commits.
- **Misses:** 0 on every labeled fixture commit (73 across 6 sets) and on {PROBES} real mutation probes.
- **CPU:** ≈ 5 ms per fixture commit on the laptop. A parse check, when needed, adds ≈ 0.3 s.
- **Effort:** about 1.5 h to port into `selector/select.py::_select_file`. The prototype is in `research/moonshots/14-fixtures/`.

**Two of the brief's ideas do not pay off:**

- **"Record pinned outputs per pair":** this is unnecessary, because the pinned actual output of an exact-string reader *is* the old expected text (the test passes at the pinned commit).
  - So any expected-half edit that survives the loader is a **certain fail**.
  - We already select exactly that test, so predicting the failure saves 0 s.
- **"Run the changed input pair to predict"** (F3) is worth at most ≈ 1 s per 40.
  - In private it would only help 3 of 264 commits (dialect-meta and one input edit that still passes), at ~2 s each.
  - It needs per-test reconstruction of `check_file` arguments and a duckdb execute sub-test. Not worth it now.

This overlaps moon-01 §4.2 (TPC fixture-entry rule, "≈ −8 s"). This report gives a general, measured and probe-checked form of that rule, plus one sound extension: a pair shift or input edit in tpc-h does **not** need the whole class.

## Where the fixture cost is (reads map, labeled)

**Fixture-only commits in private (37 of 264):**

| | Cost |
|---|---|
| Our selection (= readers) | 176.0 s |
| Readers ∩ failing (oracle) | ~96 s |

The gap has two parts:

- **TPC files: about 84 s across 4 commits.**
  - tpc-h.sql and tpc-ds.sql each have **61 readers** (31.9 s / 33.8 s).
  - The cause is `TestExecutor.setUpClass`, which runs `list(load_sql_fixture_pairs(...))` on *both* files.
  - So every one of the 60 executor tests "reads" both files, including test_execute_tpcds (21.5 s).
  - Only 2 of those tests ever consume tpc-h pairs and 1 consumes tpc-ds pairs.
  - The cost is 25.5 s per TPC commit against an oracle of 4.2–7.9 s.
- **Single-reader optimizer files: about 7 commits where the reader passes**, costing 2.0–2.6 s each against an oracle of 0.
  - Title edits or deletions: 7kn5, cp83, n7tc.
  - Dialect-meta changes: 3sjk, qk7l, fix_meta_line.
  - One input edit that still passes: bo1g.
  - All other single-reader fixture commits fail their reader, so the oracle cost equals ours.

## The rule ("consumer views")

**Map (built from the pinned test sources, allowed):** for each (fixture file, reader test), the loader it uses and the **fields of the loader output it consumes**. It is derived by `build_consumers()`:

- It parses the AST of `test_optimizer.py` and checks that `check_file("<stem>")`, `load_sql_fixture_pairs("<rel>")` or `load_sql_fixtures("<rel>")` appears in the reader's method body.
- A few rows are hand-checked: TestExecutor, test_pretty, test_identity / test_serde / test_partial.
- Any reader not in the table (cts.json, CSVs, anything new) is kept, exactly as today.

| Reader | Loader | View compared |
|---|---|---|
| TestOptimizer check_file / annotate_* | pairs | (meta minus `title`, sql, expected). The title only names a subTest; no call uses `only=`. |
| test_qualify_columns__invalid, test_identity, test_serde, test_partial | list | every item |
| test_pretty | pairs | (sql, expected). Meta is ignored (`for _, sql, pretty`). |
| test_execute_tpch / tpcds | pairs | (bool(meta.execute), sql), plus the parse check below |
| test_optimized_tpch (tpc-h only) | pairs | (sql, expected) |
| other 57–58 TestExecutor tests | pairs | none: only that loading does not raise (setUpClass) |

**Run time, per fixture FilePatch** (same path, not binary, not .csv.gz; else the current rule):

1. Apply the hunk to the pinned text.
2. Run the **pinned** `tests/helpers.py` loaders on the old and new bytes.
   - Import them from the pinned clone; never reimplement them.
   - Feed the bytes through `open(..., encoding="utf-8")` semantics, so the universal-newline and UTF-8 errors are identical.
3. If the new load raises (for example a `#` line without `:` makes `_extract_meta` raise ValueError, or a decode error), **keep every reader**, because setUpClass errors the whole class.
4. For each reader, project the old and new loader output onto its view.
   - Equal → drop the reader.
   - Different → keep it.
   - A pair shift (an inserted or removed `;`) changes every view after it, so every consuming reader is kept.
   - Load-only consumers are still dropped on a pair shift. They only `list()` the output.
5. **Parse check** (test_execute_*): `_mp_execute` runs `parse_one(sql)` plus a pickle round trip on **every** query, and runs the executor only when `meta.get("execute")` is truthy.
   - tpc-h has **no** `# execute: true` at all. tpc-ds has 57 of 99.
   - If every changed query has execute false (old and new), and `parse_one(new sql)` and the pickle round trip succeed under pinned code, the test's outcome is unchanged, so drop it.
   - Only when the commit has no `.py` edit, i.e. the pinned code is the code under test.
6. **Guards** (fall back to all readers):
   - The commit touches `tests/helpers.py`, `tests/test_executor.py`, `tests/test_optimizer.py`, `tests/test_transpile.py` or `tests/test_serde.py`.
   - The fixture is renamed, deleted or binary.
   - Any exception in the rule.

**Why it is sound.** Each reader's only dependency on the file is the loader output it consumes:

- The executor tests outside the tpc group never touch `tpch_sqls` / `tpcds_sqls`. A full grep finds them only at lines 118, 119, 158, 167, 188 and 191.
- The loader is the pinned function run on the exact new bytes.

So an equal view gives an identical execution of that reader. The fixture's contribution is computed per FilePatch, and code edits in mixed commits still go through their own rules (union), so they are unaffected.

## Numbers

### Labeled sets (soundness and cost)

Prototype: `evalfx.py`. Cost is the fixture contribution only. The oracle is readers ∩ failing.

| Set | Fixture commits | Old | New | Oracle | Misses | Commits now [] |
|---|---|---|---|---|---|---|
| private (264) | 49 | 229.4 | **144.9** | 138.9 | 0 | +3 (7kn5, cp83, n7tc) |
| adversarial (46) | 4 | 32.2 | **12.6** | 6.5 | 0 | 0 |
| sample, practice_like, private41, adversarial2 | 11 / 7 / 1 / 1 | unchanged | | | 0 | 0 |

Per commit on the TPC commits (old → new, oracle):

| Commit | Old → new | Oracle |
|---|---|---|
| 17ns | 25.5 → 7.9 | 7.9 |
| d5qt | 25.5 → 4.2 | 4.2 |
| qp5p | 25.5 → 5.9 | 5.9 |
| v2bv | 25.5 → 5.9 | 5.9 |
| fix_setupclass_tpch | 25.5 → 5.9 | 2.4 (test_optimized_tpch kept; it compares duckdb frames) |
| db6n (tpc-ds input edit to an executed query) | 25.5 → 25.5 | correctly kept |

### Unlabeled practice-style sets (cost only)

| Set | Fixture commits | Old | New | Δ | Per 40 commits | Fixture-only Δ per 40 |
|---|---|---|---|---|---|---|
| P (600) | 147 | 626.4 | 469.0 | −157.4 | **−10.5 s** | −9.1 s |
| Q (1000) | 255 | 919.1 | 718.8 | −200.3 | **−8.0 s** | −4.9 s |
| private (264) | 49 | 229.4 | 144.9 | −84.5 | **−12.8 s** | |

- In P and Q, all of the gain is TPC: 8 of the 12 TPC commits in P and 10 of the 14 in Q move 25.5 → 4.2–7.9 s.
  - The rest are tpc-ds input edits to executed queries, which are kept.
  - One Q commit (rmmg) was cleared by the parse check (tpc-ds, non-executed query): −21.3 s.
- **Mixed commits:** the code part may already select a slow test, so the true gain is between the "fixture-only" and "all" columns.
- **Rule CPU (laptop):** 0.25 s for 49 commits, 0.8 s for 255. On the scorer (2× slower) that is ≈ 0.01 s per fixture commit.

## Real mutation probes (scratch sqlglot copy, SKIP_INTEGRATION=0, every reader run)

`probe.py` edits the fixture, predicts the kept set, runs **all** readers with pytest -n 6, records every failed report (including subtests and setUpClass errors) with a pytest plugin, and checks failing ⊆ kept.

{PROBE_TABLE}

## Plan

- **R-next (≈1.5 h):**
  1. Move `fxrule.py` into `selector/fixtures.py`, using an in-memory `open` shim instead of a temp dir (/tmp is 64 MB and the image is read-only).
  2. Call it from `_select_file` before `return readers, "fixture → readers"`.
  3. Run the parse check in the existing pinned-code subprocess path, or in-process when the commit has no `.py` edits.
  4. Gate it with `eval/gap.py` on private, adversarial and adversarial2 (expect private −84 s, 0 misses), then A/B.
- **Do not build** the per-pair output recorder or F3 (pair replay) now. Together they are worth ≤ 1 s per 40.

## Probability and gain

- **P(sound and merged with 0 new misses): ≈ 0.9.** The rule is simple, the guards are conservative, and it has been probed.
- **Expected gain:**
  - Practice board: −8 s per 40 in expectation. It is 0 if the board's 40 commits hold no TPC fixture edit; moon-01 suggests the practice set has at most 1.
  - Final: ≈ −12 s expected over ~60 commits.
