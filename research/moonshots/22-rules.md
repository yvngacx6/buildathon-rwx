# Moonshot 22: per-optimizer-rule checkpoints for the TPC sub-test (on top of moon-05)

## Verdict (numbers first)

- **It works, but the gain is small.** It cuts moon-05's re-run CPU by about 30 % and adds
  almost no new tpcds drops: **+1 drop in 1,872 commits**, P_6phk.
- **0 misses** on every labeled set: practice_like, private, private41, adversarial, adversarial2,
  sample. Drops are unchanged on all of them.
- **Ground truth:** __TRUTH__
- **Re-run (verification child) CPU, rules off (= moon-05) → rules on.** EC2 container, load ~100–200,
  store calibrated to laptop CPU (as moon-05).

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
- **Per 40 practice-style commits:** −1.7 s of child CPU. At the scorer's 2x that is **≈ −3.4 s**.
  The sim cost does not change: no new drops on practice_like.
- **Why so few new drops.** Of the 52 refusals still left on R, about 40 are full-mode. Their
  touched lines also run in the executor windows (prep / plan / post: `executor/context.py`,
  `table.py`, `trie.py`, `schema.py`, `scope.py`, `generators/python.py`, `planner.py`), so every
  query has to be re-run whole (11.4 s for 57 queries). An optimizer checkpoint cannot help them.
  The few that are optb-only were already within budget under moon-05.
- **Store and build:** __BUILD__
- **Recommendation:** merge it as part of the moon-05 / r29-tpcstage line. It is cheap and sound,
  and saves ~3 s per 40 commits. Do not expect board movement beyond that. The next lever for the
  remaining refusals is executor-step checkpoints (moon-05 phase 2, item 2), not finer optimizer
  checkpoints. **P(works on the board as measured) ≈ 0.75; expected gain ≈ 2–4 s per 40 commits.**

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
