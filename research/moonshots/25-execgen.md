# Moonshot 25: code-generation checkpoint inside the TPC executor

## Verdict (numbers first)

**The checkpoint is sound and cheap, but the bucket it serves is tiny. Mergeable as a small, safe
gain; not a moonshot. P(helps on the board) ≈ 0.15. Expected gain ≈ −0.5 to −1.5 s per 40
practice-style commits, 0 misses.**

- **Mechanism.** At build time, record every call the executor makes into a pure code-generation step:
  - `PythonExecutor.generate` → `self.generator.generate(expr)` (expression → Python source);
  - `_register_subquery` → `query.sql()` (the key of the subquery-plan cache).

  At run time, a commit whose touched statements run in the post (PythonExecutor) window *only
  inside those calls* replays them with the patched code (≈0.70 s laptop-CPU for all 57 tpcds
  queries, against 11.43 s to re-run every query) and compares the strings and the generator state
  exactly.
- **A/B, same calibrated store, `BR_SUBTEST_GEN=0/1`, EC2 at load 25–95:**

| set | commits | sim 0 → 1 | misses | subtest child CPU (EC2) | new drops |
|---|---|---|---|---|---|
| practice_like | 120 | 824.2 → 824.2 | 0 → 0 | 16.2 → 16.4 | 0 |
| sample | 30 | 115.0 → 115.0 | 0 → 0 | 0.5 → 0.5 | 0 |
| private | 264 | 2226.7 → 2226.7 | 0 → 0 | 16.9 → 17.0 | 0 |
| private41 | 41 | 354.9 → 354.9 | 0 → 0 | 2.5 → 2.5 | 0 |
| adversarial | 46 | 1046.3 → 1046.3 | 0 → 0 | 0.5 → 0.5 | 0 |
| adversarial2 | 65 | 923.8 → 923.8 | 0 → 0 | 0.0 → 0.0 | 0 |
| P (unlabeled) | 600 | 4407.2 → 4405.9 | – | 97.6 → 96.3 | 0 |
| Q (unlabeled) | 1000 | 7131.1 → 7094.9 (**−36.1**) | – | 162.1 → 168.5 | 2 (6b24, e74m) |
| R (m22's 166 r29 refusals from P+Q+private+practice_like) | 166 | 3603.2 → 3591.5 | 0 → 0 | 211.5 → 217.2 | 2 (same) |
| **G** (every one-token mutant on inside-generate-only lines, 80) | 80 | 1935.8 → 1820.9 (**−115**) | – | 41.3 → 61.0 | 7 |

  - In R, Q_yt9b flips to ALL in mode 1. That is the global import-state time budget under load,
    not this change.
- **Per 40 commits:**
  - Q-style: −1.4 s sim and +0.26 s EC2 child CPU.
  - P-style and practice_like: 0.
  - Over P+Q: −36 s / 1600 ≈ **−0.9 s per 40**.
- **Ground truth:** the real `test_execute_tpcds` / `test_execute_tpch` / `test_optimized_tpch`
  on patched sqlglot, for every new drop (2 in Q plus 7 in G; e74m = g005 is the same patch) and
  for the controls. Results are in the table under "Ground truth".
- **Code:** branch `moon-25`, `selector/subtest.py` (+~300 lines; `BR_SUBTEST_GEN=0` turns it off).
  - Store: 5.7 → 6.8 MB. It is built in the image, not in the repo.
  - Subtest build: 1 m 39 s wall on EC2 under load, including the gen record and self-check forks.
  - 55 of 57 executed tpcds queries are eligible. Two are refused for "nested generate".

## Where the executor-stage keeps really are (step 0)

The r29 tpcds keeps (`subtest: keep (est …)` or `verification … failed`) were bucketed by touched file:
- practice_like: 6;
- private: 17;
- R: 166 refusals out of 1,872 P/Q/private/practice_like commits.

| bucket | R | private | practice_like | reachable here? |
|---|---|---|---|---|
| generator.py / generators/python.py (codegen) | 7 | 3 | 0 | yes: this moonshot |
| executor runtime (context.py, table.py, env.py, python.py loops, `execute`) | ~10 | 4 | 3 | no: per-row code; a post-only re-run still costs ≈8 s for 57 queries, over the 6 s cap |
| planner (Step.from_expression) | 1–2 | 0 | 0 | a Plan-DAG digest would cover it; tiny |
| optimizer / scope / schema / serde / parser / trie / datatypes | rest (~140) | 10 | 3 | no: moon-05 / moon-22 territory (optb checkpoint, rule checkpoints) |

So the codegen bucket is ≈4% of refusals. In private, the 3 generator keeps (cyt7 join_sql, tanh
set_operations, yc4h select_sql) are verifications where the optimized tree or the duckdb SQL
differs. The new checkpoint does not change those, which is correct.

## Mechanism (implemented in `selector/subtest.py`)

**Build** (inside the existing moon-05 `windows()` fork, post window):
- `GenRecorder` wraps `Generator.generate` at class level.
- Each call is classified by its caller frames:
  - `py`: the executor's own generator instance, from `PythonExecutor.generate`;
  - `sql`: `Dialect.generate` ← `Expr.sql` ← `PythonExecutor._register_subquery`;
  - anything else, a nested call, `copy=False`, an exception, or a mutated input → the query is
    ineligible.
- Each call stores:
  - a lossless pickle of the input: every slot, including None-valued args, key order, `_hash`,
    `_type`, `_meta`, comments and parent links; the root's parent is dropped;
  - strict_digest + `_hash` digest of the input;
  - the call depth;
  - the output string.
- After the run, a digest of the generator's state (all slots; closures by cell contents, e.g. the
  `_next_name` counter; `identify`; `unsupported_messages`).
- `_SplitCov`: the post-window coverage is split into lines run inside these calls and lines run
  outside.
  - A location is DISABLEd only on an outside hit. Inside hits keep it enabled, so a line first
    seen inside and later outside still counts as outside: the DISABLE trap.
  - The recorder's own work is excluded from coverage.
- Self-check in a fresh fork with the pinned code: thaw, compare digests, replay. The query is
  used only if every string and the state are reproduced (all 55 recorded queries pass). Replay
  CPU is stored, calibrated (`calib25.py`).

**Run.** The new mode applies to a query only when all of these hold:
- r29 `stage_guard` passes (no shared-state writes in changed functions);
- touched ∩ (prep ∪ plan ∪ post-outside ∪ glue) = ∅;
- touched ∩ post-inside ≠ ∅;
- touched ∩ {Generator.generate, Dialect.generate, Expr.sql / copy / __deepcopy__} = ∅. Only
  these can make the generator see the uncopied, parented input that the pickle drops.

It stacks with the existing parse / optb / duck checks.

The child replays the calls:
- in recorded order, on one generator obtained via `E.PythonExecutor().generator` (the same
  construction path);
- `sql` calls via `e.sql()`;
- each call padded to at least its recorded stack depth (a deeper recursion hits the limit no
  later than at build).

It then compares:
- every output string;
- the final generator state;
- every `py` output again on a fresh instance per call. The executor's call order follows
  set iteration over plan steps, so outputs must not depend on what earlier calls left in the
  instance.

On a mismatch it takes the existing fallback: re-run the whole query within `FALLBACK_CAP`,
else keep.

**Soundness argument.** prep, plan, glue and post-outside run no touched statement, and the
optimized tree is equal (strict digest) or untouched. So the patched run is identical to the
pinned one up to the first recorded call, and that call's input equals the recorded one.

A recorded call has these effects:
- its return value: compared;
- mutation of its input: impossible, because the generator copies, and copy / generate are
  forbidden to change;
- its generator instance: state compared, and order-independence checked;
- global state: `stage_guard`;
- an exception or recursion depth: compared or padded.

By induction, the executor sees the same strings, compiles the same code, and returns the pinned
result.

**Remaining gaps** (all shared with r17 / r29):
- an unchanged callee that writes global state on the changed function's behalf;
- a real pool-worker stack deeper than the build's (the build's is deeper here);
- call order in the real worker differing from the build when the patched outputs depend on
  instance state *and* are order-dependent in a way neither the forward nor the per-call-fresh
  replay shows.

## Ground truth and controls

Run with `truth25.sh`: apply the patch with the selector's patcher, then run the 3 TPC tests.
- Newly dropped:
  - Q 6b24: `is_is and` → `is_is or` in Generator.binary. **PASS.**
  - Q e74m = G g005: `Func) and e.is_var_len_args` → `or`, in `_rename`.
  - G g004, g038, g056: `is` → `==` in Generator.binary.
  - G g043: `len(values) == 1` → `== 0`, in `_rename`.
  - G g047, g058: the `escape_backslash` / `ch != "\\"` branch in Generator's string escaping.
- Controls that must be kept, and were kept:
  - a25o: sort direction swapped in `_ordered_py`. Kept after a gen mismatch.
  - a25s: `_next_name()` advanced inside `_rename`. Same strings, state differs. Kept.
  - dy76 / gci2: `_rename` arity. Gen mismatch, kept.
  - G g003 / g016 / g033 / g039 / g045: gen mismatch, kept. Their truth is in the table.

| commit | selector (mode 1) | real tests |
|---|---|---|
| TRUTH_TABLE |  |  |

## Costs the A/B does not show

- Build:
  - one extra fork per executed query (thaw + replay ≈ 0.01–0.02 s each);
  - recording overhead in the windows fork;
  - the whole subtest build took 1 m 39 s on EC2 under load.
- Store: +1.1 MB zipped (3.5 MB raw of frozen inputs).
- Verification CPU of the new drops: 1.8–3.5 s EC2 child CPU (loaded box). Most of that is
  *not* the replay (0.70 s calibrated for all 57 queries). It is the optb re-run, because
  generator lines also run in `execute()`'s eager debug `expression.sql(pretty=True)`, which lives
  in the optb window.

## Next steps (not built, ranked)

1. **Treat the debug `expression.sql(pretty=True)` as a recorded pure call too.** Its string is
   discarded; only an exception or a mutation matters. Generator-only edits then skip the optimize
   re-run: verify cost ≈ 0.8 s instead of ≈ 2.6 s per commit, and more `est` refusals become
   affordable. Store +~3 MB (frozen optimized trees). About 1 h.
2. **Plan-DAG digest checkpoint for planner edits.** Covers 1–2 of the 166 refusals. Low value.
3. **Executor-runtime edits.** context.py, table.py, env.py and the python.py loops make up the
   larger refused bucket, and they remain out of reach. They run per row; no pure step separates
   them from the data.

## Reproduce

rwx-big `~/work/m25`:
- `build25.sh` (index + calibrated store);
- `job25.sh name:dir …` (modes 0/1);
- `an25.py OUT name:dir`;
- `mkgen.py` (G set), `mkadv.py` (adversarial);
- `truth25.sh list`.

All scripts are in `research/moonshots/25/`.
