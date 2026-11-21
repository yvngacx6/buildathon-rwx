# Moonshot 10: wildcard angles (branch `moon-10`)

## Verdict and numbers first

None of the 17 angles below is a 190 → 120 s moonshot. The scoring rules are tight: you cannot beat the oracle, the
packing is already optimal, and labels can't be gamed. The one new, sound, measured lever is:

**V1: pytest-free verification of tiny residual selections.** After diffexec and the state diff, many commits still
end with a residual of a few cheap tests (0–0.7 recorded s) where no test fails, and that costs the 2 s worker
floor. In the sample's gap report this is 18 of the 24.4 s gap (2qn5 fryt 8y7j 2haz 66ks gbav ktz3; 4d63 is
6.1 s). The idea is to run those tests with plain `unittest` inside the process that already holds the patched
code, and output `[]` when every one passes.

| Measured (laptop; prototype in scratchpad `m10/`; labels from the sample and private sets) | Result |
|---|---|
| Fidelity: commits verified (labeled sets, selection ≤ 3 s) | 73 commits, 418 tests, 152 of them labeled failing |
| Harness verdict vs labels | **0 pass-here/fail-in-labels, 0 fail-here/pass-in-labels**. Every subtest-only failure reproduced |
| Timeouts (2gnj, 80lk: patch makes tests hang) | ran past 120 s and were kept; both are labeled failing, so keep-on-timeout is right |
| Sample: zero-failure commits turned into `[]` | 7 of 7 cheap ones (−15.5 s sim before gating) |
| Gated policy (verify iff predicted scorer CPU ≤ 0.6 s, scorer = 2x laptop, load-normalized) | sample **−6.8 s net / 30 commits (≈ −9 s per 40)**; private −6.7 s over 122 commits (≈ −2.2 per 40); P (unlabeled, harsher mutants) −7.3 s over 209 commits (≈ −1.4 per 40) |
| Marginal CPU per verified commit (warm .pyc, beyond `import sqlglot`) | dialect tests 0.02–0.2 s; test_optimizer 0.4 s import + pools (gate out); executor 7 s (gate out) |

**Expected gain: about 3–9 s per 40 practice commits** (the sample is the closest to practice, ~9 s; mutation sets
~2 s), so about 4–13 s on the 60-commit final, with 0 misses on 73 labeled commits. **P(works on the board as
measured) ≈ 60 %. P(it is a moonshot) ≈ 0 %.** It is a solid incremental round worth ~4–6 h.

**How V1 differs from parked I10/I10b (report 03, cand-verify).** Those ran pytest in a fork server: about 0.7 s
CPU per run before any test, wall-clock kills under load, and candidate sets from before diffexec.
1. **No pytest is needed.** Every sqlglot test is a `unittest.TestCase` method: ids are `file::Class::method`, and
   there is no conftest, no `[tool.pytest]` config and no plain `def test_`. So
   `unittest.TestSuite([Cls(m)]).run(TestResult())` gives the same verdicts. Subtest failures land in `failures`,
   an unexpected success counts as fail (conservative), and `setUpClass` runs per suite.
2. **No new interpreter is needed, but the verdict must run on clean state.** The diffexec child `fork()`s
   **before replay**, right after the code objects are swapped and before anything has executed, and the
   verification runs in that fork. Running it after replay is unsound: the replay runs old code, which can fill
   `lru_cache`s, `_build_dispatch` and the dialect caches with old values, and a test could then pass on stale
   data. The other place it can run is the state-diff subprocess, which imports the patched package from source.
   My prototype paid a fresh interpreter plus `import sqlglot` on a tree with the patch on disk, and I subtracted
   the import.
3. **Soundness rule: no tests that spawn processes.** Under a `__code__` swap, ProcessPool workers
   (`TestOptimizer.setUpClass`, the executor tests) import the unpatched files from /opt/sqlglot, so they could
   falsely pass. My optimizer agreements (2vdj, 535z, egys, …) do not transfer, because my tree had the patch on
   disk. Those tests are also too expensive to verify.
4. **Budgets are CPU only** (RLIMIT_CPU / process_time). Per-test CPU is predicted from a build-time map. Pin
   affinity to the first 1–2 CPUs of `os.sched_getaffinity(0)`. Never hard-code CPUs 0 and 1: that raises if the
   cpuset lacks them. Without pinning, the spawn pool sized itself to my laptop and burned 41 CPU s on one test.

**Build plan.**
- Build: run each test alone in the harness and store its CPU and whether it spawns processes (a pinned-commit map, allowed).
- Run: for a non-empty selection whose predicted scorer CPU is ≤ ~0.6 s, with no pool tests, that is eligible
  for diffexec or the state diff, run the tests cheapest first and **abort on the first failure** (this cuts
  the wasted CPU on failing commits, which is what kills the gain on P). All pass → `[]`.
- Anything odd keeps everything: an import error, a crash, the CPU cap (3x the pinned CPU + 0.2 s), or a test that
  measures time, gc or memory.

**Remaining fidelity risks.**
- Cross-test state leaks: patched code run by an earlier test in the labeler's worker could affect a later one.
  diffexec already refuses defs that write module or class state. Add `lru_cache` / `cache` defs to that refusal.
- My one apparent mismatch (22yd: TSQL test_ddl) was my bug. I copied the tree while another run had a
  mutant applied. On a clean tree it agrees.
- Must be validated in the pinned image on EC2 (Python 3.13 slim, requirements.lock).

## Deep-dive 2: the mid band (selections of 1.5–10 s)

**Fixture-case slicing generalizes r17's TPC sub-query verifier to every fixture-loop test.** Not prototyped: over
budget.
- **The band.** P has 119/600 commits there. It is dominated by `test_annotate_funcs` (1.96 s, in 56 commits),
  `test_qualify_columns` (1.4 s, 44), `test_simplify`, `test_optimize`, `test_canonicalize`, and the snowflake,
  bigquery and duckdb dialect tests.
- **Why whole tests don't pay.** Running a whole optimizer fixture test costs 2–4 CPU s × 2 at the scorer, more than it saves.
- **The fix.** Both `test_annotate_funcs` and `check_file` loop over fixture cases under `subTest`, each case an
  independent `parse_and_optimize(...)` + assertEqual.
- **Build.** Record per-case coverage, each case in a cold fork as `subtest.py` does, so that memoized code is
  seen by every case that relies on it.
- **Run.** Re-run only the cases that ran a touched statement, using the test's own comparison. A typical edit
  hits a handful of the ~1000 cases, so it costs milliseconds instead of 2 s.
- **Ceiling (rough, unmeasured pass rate).** If ~25 % of mid-band commits pass entirely, about 5–10 s per 40 on
  P-like sets. It also trims mixed selections, for example dropping annotate_funcs (1.96 s) when only a dialect test fails.
- **Effort.** 6–8 h. **P(works) ≈ 35 %.**

## The 17 angles: feasibility and gain

Several of these were checked with one-liners on `sample/answers.json` and `tests.json`.
1. **Tests that fail at the pinned commit (would fail on every commit).** All 3 comment commits have 0 failures,
   so none exist. Dead.
2. **Zero-ms tests (94 of 1620; `round(s*1000)=0`).** They are free inside any non-empty worker. That makes
   them insurance (always add them when a worker exists, to cover recall risk), not a gain. ≈0 s, but free safety.
3. **The `skipped` key (1 test).** It has no number and can never fail. Nothing to exploit. Dead.
4. **Worker-cost structure (max + 2/worker).** LPT over k = 1..8 already equals the oracle packing (gap.py packing
   gap = 0). Dead.
5. **Beating the oracle.** Impossible: every failing test must be included. The sample oracle (94.4 s)
   includes 2 tpcds commits; practice has only 1 slow-test commit, so the practice oracle is ≈ 90–100 s (not
   126). The leaders therefore still have ~30 s of headroom. Ours is ~60 s of slow tests plus ~60 s of non-slow
   over-selection (128 s non-slow vs an oracle non-slow share of ~65–75 s).
6. **V1: run the residual tests without pytest in the existing child.** Measured above: −3…−9 s per 40. **Top pick.**
7. **Fixture-case / assertion-level slicing.** Deep-dive 2 above: −5…−10 s per 40 ceiling, 6–8 h.
8. **A statement rule for fast tests**, not just slow ones. This helps only where diffexec has no recording
   (2qn5 "not recorded (budget)"): diffexec already drops tests that never ran the touched statement. Small,
   1–2 s per 40, but cheap to add as a fallback when diffexec is unavailable.
9. **Reading "file cannot be imported" vs. collection error → ALL.** README says the failing tests are those
   whose file cannot be imported. That hints the labeler used per-file semantics (like
   `--continue-on-collection-errors`), not a session abort. If so, syntax / import errors in modules loaded at
   collection could use the import bound instead of ALL (36.8 s → maybe 10–30 s per such commit). There is no
   official example in the sample. The final has "new kinds", so this is worth one diagnostic board submission
   only if such kinds appear. Risk: one miss = 300 s. Keep ALL unless evidence appears.
10. **Rank, not score (game theory).** We're ~#120 and need ~−70 s. A soundness-only path is unlikely to get
    there by Sunday. A calibrated gamble on slow tests (S11: dropping all ≥ 3 s tests = −61 s but 1 miss / 40)
    loses in expectation (300 s per miss vs ~60 s). A selective gamble (drop a slow test when it ran the touched
    statement in < x % of its sub-queries) would need labels to calibrate. That is close to the banned
    "edit-kind → failures" table. Not recommended. Noted only so the team decides it deliberately.
11. **Board probing.** Diagnostic submissions, like S11 but per mechanism (e.g. disable diffexec), can attribute
    the practice cost. Each costs an hourly slot. Useful for targeting, with 0 direct gain.
12. **CPU floor.** We are at 4.9 s per run vs. leaders' total of 126. Moving the state-diff and diffexec children to
    one forked zygote (import sqlglot once), `-X frozen_modules`, and skipping `site` would save ~1–3 s
    (×1.5 on the final). It matters for the 1-s tie rule.
13. **Recorded seconds ≠ real CPU.** Some tests record far more than they cost to run (pool spawn wall time,
    e.g. test_cache_does_not_retain_queries 1.3 s). Those are the cheapest to verify per saved second. This folds into V1's gate.
14. **Determinism on the final** (5 runs on an idle machine). The gates use `process_time` CPU budgets. Wall
    deadlines (budget × 2 + 2) are only backstops, so selections should be stable. On a faster idle host more
    replays finish, which is the safe direction. OK.
15. **Comment-only edits "not promised harmless".** Line shifts can change tests that assert line numbers or
    tracebacks, and an encoding cookie or `# type:` comment can matter. A quick grep for `co_firstlineno` /
    `lineno` in tests/ is worth doing as a recall guard. 0 s gain.
16. **Patches that also touch tests or fixtures.** Covered by the existing reads / test-file rules. An edited test
    method that is itself the change could be *run* (V1) instead of selected. That is free if cheap, and part of V1.
17. **Build-time "answer" precompute.** Precomputing mutant → failures, even from the pinned commit, is a
    patch → failures table, explicitly banned. Dead. Building warm per-test CPU / coverage maps is fine (V1, slicing).

## Recommendation

Ship V1 as a round: about 4–6 h with EC2 validation in the pinned image, plus an A/B on sample / private /
adversarial / adversarial2 with 0 misses. Expect −3…−9 s per 40. If time remains, do fixture-case slicing (7).
Neither closes the gap to 126 s alone. The slow-test levers (other moonshots) remain the big ticket.
Prototype: scratchpad `m10/{harness,driver,analyze,policy}.py`. Results: `m10/*.res.json`. `m10/tree` and `m10/tree3` may still hold a mutant (drivers stopped mid-commit), so re-copy them before reuse.
