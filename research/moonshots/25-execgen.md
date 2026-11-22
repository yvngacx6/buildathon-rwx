# Moonshot 25: code-generation checkpoint inside the TPC executor

## Verdict (numbers first)

**Sound and cheap, but the bucket it serves is tiny.**
- P(helps on the board) ≈ 0.15.
- Expected ≈ −0.5 to −1.5 s per 40 practice-style commits, 0 misses.
- **Recommend merge**: the subtest build gets +9–14 s on EC2 (≈+20–28 s on the scorer, 10-min cap) and the image store +0.6 MB, for a gain that is small but positive.

**Mechanism.** At build time, record every pure expression → string call the executor makes, with
its input and output:
- `self.generator.generate(expr)`, the Python source that PythonExecutor compiles;
- `query.sql()` in `_register_subquery`, the subquery-plan cache key.

At run time, take a commit whose touched statements run in the PythonExecutor window *only inside
those calls*. Replay the calls with the patched code and compare the strings and the generator
state exactly. Replay CPU for all 57 tpcds queries is **0.70 s** (laptop-calibrated), against
**11.43 s** to re-run them whole. 55 of the 57 executed queries are eligible (2 make nested
generate calls).

**A/B** (same calibrated store, `BR_SUBTEST_GEN=0/1`, EC2 at load 25–95):

| set | commits | sim 0 → 1 (s) | misses | child CPU (EC2 s) | new drops |
|---|---|---|---|---|---|
| practice_like | 120 | 824.2 → 824.2 | 0 → 0 | 16.2 → 16.4 | 0 |
| sample | 30 | 115.0 → 115.0 | 0 → 0 | 0.5 → 0.5 | 0 |
| private | 264 | 2226.7 → 2226.7 | 0 → 0 | 16.9 → 17.0 | 0 |
| private41 | 41 | 354.9 → 354.9 | 0 → 0 | 2.5 → 2.5 | 0 |
| adversarial | 46 | 1046.3 → 1046.3 | 0 → 0 | 0.5 → 0.5 | 0 |
| adversarial2 | 65 | 923.8 → 923.8 | 0 → 0 | 0.0 → 0.0 | 0 |
| P | 600 | 4407.2 → 4405.9 | – | 97.6 → 96.3 | 0 |
| Q | 1000 | 7131.1 → **7094.9 (−36.1)** | – | 162.1 → 168.5 | 2 (6b24, e74m) |
| R (m22: 166 r29 refusals of P+Q+private+practice_like) | 166 | 3603.2 → 3591.5 | 0 → 0 | 211.5 → 217.2 | the same 2 |
| G (all 80 one-token sites on inside-generate-only lines) | 80 | 1935.8 → **1820.9 (−115)** | – | 41.3 → 61.0 | 7 |
| S (60 string-literal mutants on those lines) | 60 | 1261.3 → **1152.0 (−109)** | – | 39.3 → 55.0 | 7 |

- R's one lost drop (Q_yt9b) is the global import-state time budget firing under load (ALL), not
  this change.
- Per 40 commits: Q-style −1.4 s sim with +0.26 s EC2 child CPU; P-style and practice_like 0.
  Over P+Q that is ≈ **−0.9 s/40**.

**The labeled sets do not test the new mode.** Mode 0 and mode 1 made identical decisions on all
six labeled sets, so "0 misses" there says nothing about gen mode. The evidence for its soundness
is only this:
- **16/16 new drops really pass** (15 distinct patches: Q 2, G 7, S 7);
- **7/7 gen-mismatch keeps really fail** tpcds;
- 2 hand-made controls are kept conservatively. For a25s, `dbg25.py` shows that only the state
  check fired.

## Where the executor-stage keeps are

Tpcds keeps by touched file (r29 `est` or `verification failed`):

| bucket | R (166) | private (17) | practice_like (6) | reachable? |
|---|---|---|---|---|
| generator.py, generators/python.py | 7 | 3 | 0 | yes: this moonshot |
| executor runtime (context / table / env / python.py loops, `execute`) | ~10 | 4 | 3 | no: per-row code; a post-only re-run is still ≈8 s for 57 queries |
| planner | 1–2 | 0 | 0 | a Plan-DAG digest would cover it; tiny |
| optimizer / scope / schema / serde / parser / trie / datatypes | ~140 | 10 | 3 | moon-05 / moon-22 territory |

The three private generator keeps (cyt7, tanh, yc4h) are cases where the optimized tree or the
duckdb SQL differs. They are unchanged, which is correct.

## Mechanism and soundness (`selector/subtest.py`; `BR_SUBTEST_GEN=0` turns it off)

**Build** (moon-05 `windows()` fork, post window):
- `GenRecorder` wraps `Generator.generate`. Calls are `py` (the executor's generator, from
  `PythonExecutor.generate`) or `sql` (`Dialect.generate` ← `Expr.sql` ← `_register_subquery`).
- Per call it stores a lossless input pickle (all slots, root parent dropped), the input's strict
  and `_hash` digest, the stack depth and the output; at the end, a generator-state digest.
- Another caller, a nested call, `copy=False`, an exception or a mutated input makes the query
  ineligible.
- `_SplitCov` splits post coverage into inside-call and outside-call lines. Only an outside hit
  DISABLEs a location, so a line seen inside then outside counts as outside.
- A fresh-fork replay with the pinned code must reproduce every string and the state.

**Run.** A query uses gen mode only if all of these hold:
- r29 `stage_guard` passes;
- touched ∩ (prep ∪ plan ∪ post-outside ∪ glue) = ∅;
- touched ∩ post-inside ≠ ∅;
- touched ∩ {Generator.generate, Dialect.generate, Expr.sql / copy / __deepcopy__} = ∅. Only these
  could expose the parented, uncopied input the pickle drops.

The child replays the calls in order on one `E.PythonExecutor().generator` (`sql` calls via
`e.sql()`), each padded to its recorded depth, then every `py` call again on a fresh instance.
Strings and final state must match, else the moon-05 fallback.

**Argument.** Everything before post is identical, and post runs only untouched code until the
first recorded call. A call's effects are all covered:
- its string: compared;
- input mutation: impossible (generate copies; copy may not change);
- instance state: compared, plus fresh-instance order independence;
- global writes: `stage_guard`;
- exceptions and depth: compared or padded.

By induction, the executor compiles the same code.

**Gaps.**
- An unchanged callee writing global state (as r17 / r29).
- A real worker stack deeper than the build's. This is assumed, not measured.
- An order-dependent patched output that neither replay exposes.

## Ground truth

The real 3 TPC tests on patched sqlglot (`truth25.sh`):

| commit | mode 1 | real tests |
|---|---|---|
| Q 6b24, e74m; G g004, g005 (= e74m), g038, g043, g047, g056, g058 | dropped | **all pass** (9/9) |
| P dy76, P gci2, G g003, g016, g033, g039, g045 | kept (gen replay differs) | **all fail** tpcds (7/7) |
| a25o (sort direction swapped in `_ordered_py`) | kept (outputs differ in 47 queries) | passes: conservative keep |
| a25s (`_next_name()` side effect in `_rename`) | kept (outputs equal, state differs in 46 queries) | passes: conservative keep |
| S s007, s008, s012, s019, s025, s027, s048 | dropped | **all pass** (7/7) |

The patches behind the new drops:
- `is_is and` → `or` (6b24), and `is` → `==` (g004 / g038 / g056), in Generator.binary;
- `Func) and is_var_len_args` → `or` (e74m), and `len(values) == 1` → `== 0` (g043), in `_rename`;
- the backslash-escape condition in string escaping (g047 / g058).

S adds strings: "IS" / "IS NOT" / "operator" in binary; "True" / "False" in `_ordered_py`, a branch no executed query takes.

## Build cost

- Same container, two interleaved runs each. `selector.subtest build` takes cand-a **75.3 / 76.7 s**
  and moon-25 **84.5 / 90.5 s** of wall time (+9–14 s; user +8–13 s).
- The zip grows from 6.16 MB to 6.76 MB (+0.6 MB). It lives in the image, not in the repo.

Verification CPU of the new drops is 1.8–3.5 s of EC2 child CPU on a loaded box. Most of it is
*not* the replay. Generator lines also run in `execute()`'s eager debug `expression.sql(pretty=True)`
(optb window), so optimize is re-run from the moon-05 checkpoint.

## Next steps (not built)

1. **Record the debug `expression.sql(pretty=True)` as a pure call too.** Its string is discarded;
   only an exception or a mutation matters. Generator-only edits then skip the optimize re-run:
   ≈0.8 s instead of ≈2.6 s per commit, and more `est` refusals become affordable. Store +~3 MB.
   About 1 h.
2. **Plan-DAG digest for planner edits.** Covers 1–2 of the 166 refusals.
3. **Executor-runtime edits** (the larger refused bucket) stay out of reach. They run per row, and
   no pure step separates them from the data.

Reproduce: rwx-big `~/work/m25`; scripts in `research/moonshots/25/` (build25, job25, an25, mkgen / mkstr / mkadv, truth25, buildab, dbg25).
