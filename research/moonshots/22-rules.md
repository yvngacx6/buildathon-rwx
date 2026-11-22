# Moonshot 22: per-optimizer-rule checkpoints for the TPC sub-test (on moon-05 / r29-tpcstage)

## Verdict (numbers first)

- **It works and is sound: re-run CPU drops ~25–35 %, 0 misses.** How many extra drops it buys
  depends on the base.
  - **On r29-tpcstage** (moon-05 plus the stage guard and build calibration; branch
    **`moon-22`** = `moon-22-r29`): **+11 tpcds drops**.
    - R: +10, sim −113.8 s over the 166 r17 refusals.
    - private41: +1 (op05, labeled, no miss).
    - P: +6, sim −67.8 s / 600. Q: +4, sim −46.0 s / 1,000. These are the same 10 commits as R's.
  - **On moon-05 as is** (moon-05-based code, `moon-22` up to 1a7b6f2): +1 drop in 1,872 commits (P_6phk). Moon-05's looser
    plan already fits most optb-only commits in the budget.
- **0 misses** on all labeled sets, on both bases: practice_like, private, private41, adversarial,
  adversarial2, sample.
- **Ground truth:** 18 commits re-run with the three real TPC tests on patched sqlglot. All 18 pass.
  - They are all P/Q drops of rules mode that moon-05's truth run had not covered.
  - They include **all 10 new P/Q drops on the r29 base** (P_6phk, a4cj, srm1, u34w, uero, z46m;
    Q_6ise, cpww, k21r, qtcd).
  - The 11th new drop, op05, is labeled.
  - Fewer than 15 *new* drops exist, so all of them are checked; the other 8 check the
    rule-checkpoint path on drops shared with moon-05.
  - Usage logging (moon-05 base): 42 of R's 55 drops and 6 of practice_like's 9 verified at least
    one query from a rule checkpoint (2,148 query runs on R).
- **r29 base, rules off → on** (EC2, 6-CPU container, the r29 in-build calibration).
  - The table below was measured at 9092ceb, which calibrates per boundary.
  - The head code (b540ada, one calibration per query) was re-checked with its own store on R and
    on all labeled sets. Results are identical:
    - drops: R 47 → 57, the same 10; op05; 0 misses;
    - child CPU: R 165.4 → 135.4 s, practice_like 15.4 → 10.9 s, private 15.8 → 11.1 s.
  - P/Q were not re-run at b540ada. Their 10 new drops are R's 10.

  | set | child CPU | drops | sim |
  |---|---|---|---|
  | R (166) | 165.4 → 146.1 s; on the 47 common drops 122.7 → 87.3 s | 47 → **57** | 3708.9 → **3595.1** |
  | practice_like (120) | 15.4 → 11.7 s | 9 → 9 | = |
  | private (264) | 15.8 → 11.6 s | 6 → 6 | = |
  | private41 (41) | 0.0 → 0.8 s | 0 → **1** (op05) | 388.9 → 377.6 |
  | sample / adversarial / adversarial2 | 0.4→0.2 / 0.5→0.5 / 0 s | = | = |
  | P (600) | 62.0 → 59.2 s; on the 26 common drops 48.8 → 36.7 s | 26 → **32** | 4636.0 → **4568.2** |
  | Q (1,000) | 132.1 → 119.9 s; on the 46 common drops 102.3 → 81.8 s | 46 → **50** | 7555.7 → **7509.7** |

- **moon-05 base, rules off → on** (first run; EC2, store calibrated to laptop as moon-05):

  | set | commits | child CPU | tpcds-commit drops | sim cost |
  |---|---|---|---|---|
  | practice_like | 120 | 21.8 → 16.7 s (−23 %) | 9 → 9 | 898.5 → 898.5 |
  | P | 600 | 72.5 → 51.9 s (−28 %) | 30 → **31** (+6phk) | −10.6 s |
  | Q | 1,000 | 129.9 → 100.1 s (−23 %) | 49 → 49 | −2.8 s |
  | R: moon-05's 166 r17 budget refusals from P+Q+private+practice_like | 166 | 175.7 → 126.7 s | 54 → 55 | −10.6 s |
  | private | 264 | 20.1 → 14.0 s | 6 → 6 | = |
  | private41 / adversarial / adversarial2 / sample | 41 / 46 / 65 / 30 | 4.1→1.0 / 0.8→0.9 / 0→0 / 0.7→0.3 s | = | = |

  On commits dropped in both modes, child CPU falls 32–35 % (P: 61.5 → 40.6 s, Q: 112.5 → 83.3 s,
  R: 150.9 → 102.8 s).
- **Per 40 practice-style commits:** −1.2 to −1.7 s of child CPU. At the scorer's 2x that is
  **≈ −2.5 to −3.4 s**. There are no new drops on practice_like.
  - The refusal population (R, P/Q) shows a larger tail: a ≈ −11 s sim win per affected commit,
    on 10 of 1,600 P+Q commits (≈ 0.6 %) on the r29 base.
  - On the final's ~60 unseen commits that is ≈ +0.4 drops, ≈ −4 s expected.
- **Why not more drops.** Of the 52 refusals still left on R (moon-05 base), about 40 are full-mode. Their
  touched lines also run in the executor windows (prep / plan / post: `executor/context.py`,
  `table.py`, `trie.py`, `schema.py`, `scope.py`, `generators/python.py`, `planner.py`), so every
  query has to be re-run whole (11.4 s for 57 queries). An optimizer checkpoint cannot help them.
  The few that are optb-only were already within budget under moon-05.
- **Store and build:** nothing is added to the repo; everything is built in the Docker build.
  - Subtest store: 6.15 → 11.28 MB.
  - Build CPU, EC2 3-CPU container at load ~100: 1m10s → 2m09s user (+59 s), 1m21s → 2m36s wall.
  - Expect ≈ +40–60 s on the idle 4-core scorer build, within the ≤3 min target.
- **Recommendation: merge `moon-22-r29` into the r29-tpcstage line.**
  - It is a clean merge: r29's 15 unit tests pass. Rule CPU is normalized by r29's `_calib`, once
    per query.
  - Build: r29 subtest build 1m19s → 2m26s wall on a 2-CPU container (+67 s), within the 3 min
    target.
  - Expect ≈ −3 s per 40 practice commits (CPU), plus occasional extra drops worth ~11 s each.
  - The next lever for the remaining refusals is executor-step checkpoints (moon-05 phase 2,
    item 2), not finer optimizer checkpoints.
  - **P(works on the board as measured) ≈ 0.75; expected gain ≈ 3–6 s per 40 commits.**

## Mechanism (`selector/subtest.py`, `BR_SUBTEST_RULES=0` turns it off)

Inside moon-05's optb window (execute → `optimize(...)` → `.sql(pretty=True)`) there are 16 steps:
- glue (`ensure_schema`, `maybe_parse(copy=True)`, the rule loop), numbered −1;
- `RULES[0..13]`: qualify … simplify;
- `.sql()`, numbered 14.

Boundary k is the tree that step k receives, for k = 1..14.

**Build** (per executed tpcds query, in forks):
1. **Step mirror.** `rule_steps` mirrors `optimize()` step by step: the same `RULES` objects and
   the same `getfullargspec` kwargs glue. It records each step's line coverage, a strict digest at
   each boundary, and a pickle of each boundary tree. Its final digest must equal moon-05's
   `d_opt`.
2. **First step per line.** Store `fs`, the lowest step that runs each line. Two kinds of line count
   as glue (−1):
   - lines in moon-05's optb window that the mirror never runs (`optimize()`'s own body);
   - lines that loading a checkpoint runs.
3. **Hash-seed invariance.** Two extra interpreters, at `PYTHONHASHSEED` 1 and 2, recompute every
   boundary digest. A boundary is usable only if all three seeds agree, because the real test runs
   rules 0..k-1 under a random seed. All 57 × 14 boundaries agreed.
4. **Usability proof.** In a fresh fork, under pinned code, run
   `optimize(load(ck_k), fresh schema, rules=RULES[k:], copy=False)` + `.sql()`. It must
   reproduce `d_opt`. The CPU of that exact operation is stored as `cpu_from[k]`.
5. **Lossless checkpoints.** `Expr.__reduce__` goes through `serde`, which drops None-valued args,
   so all 14 boundaries failed the usability proof at first. `ck_dumps` pickles every slot instead
   (args incl. None values, parent / arg_key / index, `_type`, `_meta`, `_hash`, sharing) through
   `copyreg.__newobj__`. Loading therefore runs no sqlglot code.
   - Result: 798 checkpoints, all usable, 31.6 MB raw / ~11 MB deflated, as zip members
     `ck/tpcds/<i>/<k>`. `meta.pkl` stays ~5.9 MB.

**Run.**
- `plan()`: for an optb-hit stage query, first = min `fs` over the touched lines, and
  k = the largest usable boundary ≤ first. Glue or step 0 gives k = 0, which is moon-05's path.
  Cost = `cpu_from[k]` + 0.02.
- **Child:** read only `ck/<i>/<k>` and run rules k.. + `.sql()` under the swapped code. It decides
  on the *final* `d_opt` digest, because intermediate trees may re-converge. On divergence it
  takes moon-05's capped whole-query fallback, unchanged.

**Soundness.** Every touched line first runs in step ≥ k, so steps < k run unchanged code on the
same input. The test's tree at boundary k therefore equals `ck_k`: seed-invariant, losslessly
stored, and proven to reproduce `d_opt` from a fresh schema. The fresh schema matters only through
`MappingSchema`'s memo caches. Their fills are deterministic functions of the input, and any fill
that runs a touched line has first ≤ that step. From there, moon-05's argument applies: an equal,
strict `d_opt` means plan / post see identical input. The gap is the same as moon-05's: global
state written by optimize and read later, e.g. lru caches or class attributes.

**Offline per-line view** (calibrated, 5,350 optb lines × 57 queries). Mean CPU to verify a line
falls from 1.44 to 1.00 s; median from 2.00 to 0.82 s. By file:
- `generator.py` lines: 5.6x cheaper (they only need `.sql()`);
- canonicalize: 4.1x; eliminate_joins: 2.9x; merge/eliminate_subqueries: 2.6x; annotate_types: 2.1x;
- qualify, scope, qualify_columns, resolver lines: ~1.0x. They run in step 0 (qualify), and 44 % of
  (line, query) pairs start there.

Splitting qualify into its sub-passes saves at most qualify's 21 % share of optimize CPU, so it was
not built.

## Reproduce (rwx-big, `~/work/m22`, all through `~/shared/drun.sh`)

- `sub.sh`: build the store.
- `research/moonshots/22/calib22.py`: calibrate to the laptop store's `cpu_exec`.
- `research/moonshots/22/job22.sh name:dir ...`: run both modes per set.
- `research/moonshots/22/an22.py OUT name:dir ...`: compare the modes.
- `research/moonshots/22/sim22.py STORE`: the offline per-line view.
- `research/moonshots/22/truth22.sh LIST`: ground truth (pure-Python patch apply + the three TPC
  tests).
- Outputs: `~/work/m22/out2` (labeled + R), `out3` (P), `out4` (Q), `out5` (truth), `out6`
  (build timing), `out7` (R / practice_like with rule-usage logging).
- r29 port: `~/work/m22/r29.sh` (build + unit tests + all sets), `pq.sh` (P / Q), outputs in
  `o29`; build timing in `o29b`.
