# 09 — Win the final, not the practice board

## Verdict and numbers
- **Cost gap.** On a practice-like final of 60 commits we land near 291 s, against about 189–210 s
  for the top 5. That is about 100 s behind. **We take #1 only if every team ahead of us misses at
  least once (+300 s each) and we miss none.**
- **Safety is our one edge.** Our 0 misses on 446 labeled patches cover the new kinds.
  - A naive coverage selector misses **62 %** of our "structural" new-kind commits.
  - Coverage plus an import fallback still misses **15 %**.
  - A leader at 126 s has 0 practice misses, so it is tighter than both models.
  - If leaders fall back to ALL on detectable structural kinds, they pay about what we do on
    new kinds (11.0 vs 11.7 s per commit), and the gap stays.
- **Decides the final, in order:** (1) never exceed 4.5 min (no global guard today; a killed run
  prints nothing); (2) no misses on unseen kinds; (3) cut practice-like *code* cost (our excess:
  588 s private code, 213 s practice_like); (4) submit the final early (ties only).
- **Odds:** #1 ~10 %, top 5 ~25 %. Config fix −37 s per final config commit (~0 risk); wall guard
  removes a DQ tail at zero cost. Neither closes the ~100 s gap; only practice-like moonshots can.

## 1. How leaders likely fare on new kinds
`09-tools/naive.py` models two leader-style selectors on our labeled sets, using our pinned maps:
- **N1:** test-phase line coverage of the changed lines, plus fixture readers, plus a test file's own tests.
- **N2:** N1, plus "a line no test covers at test time (def, class body, module level) → all tests
  that load the module".
Calibration on the sample:

| selector | cost | misses |
|---|---|---|
| N1 | 153 s | 2 |
| N2 | 325 s | 0 |
| oracle | 94.4 s | – |
| us (EC2) | 115 s | 0 |

Leaders at about 3.2 s per commit run at roughly oracle rate. So they are precise and also handle def-time edits.
private264 per kind (laptop, main 4826380; s; m = misses; sim via `09-tools/ours.py`):

| kind | n | ours | oracle | excess | N1 cost/miss | N2 cost/miss |
|---|---|---|---|---|---|---|
| rename | 9 | 266.4 | 52.0 | 214.3 | 10.2 / 2 | 180.3 / 0 |
| dictentry | 9 | 154.9 | 17.8 | 137.1 | 0.0 / **7** | 198.5 / 0 |
| config | 6 | 110.5 | 0.0 | **110.5** | 0 / 0 | 0 / 0 |
| string | 9 | 104.6 | 20.9 | 83.7 | 65.6 / 1 | 88.0 / 0 |
| syntaxerr | 6 | 159.4 | 90.2 | 69.2 | 29.9 / **4** | 78.9 / 1 |
| deletefile | 7 | 126.4 | 67.7 | 58.7 | 9.6 / **4** | 67.7 / 0 |
| helper (test_dialect.py, helpers.py) | 10 | 121.0 | 81.0 | 40.1 | 137.2 / **6** | 137.2 / **6** |
| importerr | 6 | 165.2 | 129.6 | 35.6 | 0.0 / **6** | 129.6 / 0 |
| testedit + executor + fixturemeta | 29 | 111.0 | 91.6 | 19.4 | 0 m | 0 m |
| newfile, docstring, whitespace, comment | 42 | 0 | 0 | 0 | 0 m | 0 m |
| **code** (practice-like) | 89 | 1081.1 | 493.2 | **588.0** | 565.9 / 21 | 1120.5 / 4 |
| **total** | 264 | 2759.3 | 1209.1 | 1550.2 | 1496.6 / **51** | 3604.4 / **11** |

Miss sources: **helper** = edits to `tests/dialects/test_dialect.py` (`Validator`) reach every
dialect test file ("run that file" misses 6/10); **dictentry** = module tables run only at import;
**syntaxerr/deletefile** on collection-loaded modules need ALL.

Probability of at least one miss over k structural commits (rename, delete, syntax, importerr,
dictentry, helper; 47 commits):

| per-commit miss rate | k = 3 | k = 6 | k = 12 |
|---|---|---|---|
| 62 % (N1-like) | 0.94 | 1.00 | 1.00 |
| 15 % (N2-like) | 0.38 | 0.62 | 0.86 |
| 5 % (practice-tuned, plausible for the top 5) | 0.14 | 0.26 | 0.46 |

Plausible final board, assuming 60 commits = 45 practice-like + 15 new-kind in the private mix:

| team | practice-like | new kinds | final |
|---|---|---|---|
| leader, robust (ALL on rename/delete/syntax/importerr, oracle-like otherwise) | 45 × 3.2 = 144 | 15 × 11.0 = 165 | **~309 s** |
| leader, aggressive (oracle-like everywhere) | 144 | 15 × 4.9 = 74 | **~218 s**; ≥ 518 s with one miss |
| us (main) | 45 × 4.85 = 218 | 15 × 11.7 = 176 | **~394 s** |
| us + config fix + −40 s/40 practice moonshot | ~173 | ~166 | ~339 s |

Caveats: the kind mix and private264 are our own generator; our 0/446 is on mutants we wrote, so it
overstates our safety on truly unseen kinds. **Takeaway:** spend moonshot effort on practice-like code commits. On new kinds we already roughly match a robust leader.

## 2. Timing (ties within 1 s → earliest submission wins)
- Two different methods tying within 1 s on about 250–400 s needs a cluster. We are about 100 s
  off the top, so the tie rule barely affects us. Don't optimise for it, but follow these free rules:
  1. **Pick the final explicitly.** The default "best practice submission" rewards practice
     score, not robustness. Pick the commit that has:
     - 0 misses on sample, private, private41, adversarial and adversarial2;
     - the wall guard;
     - a valid board run.
  2. **Freeze and submit the candidate by Sun ~04:00 PST**, leaving 3–4 hourly slots. A later
     commit loses every tie within 1 s to anyone earlier. Switch only for an expected final gain
     **> 2 s** (the 1 s window plus run noise) that passes the full regression.
  3. **Prefer the earlier commit** when the later one changes only practice-irrelevant behaviour
     or gains less than 2 s. For example, S12 and S13 had the same practice sim, so choose
     between them on robustness, not on 0.2 s.
  4. Never resubmit the same tree under a new hash. It only moves the timestamp later.
- Unclear whether "earlier" = the picked commit's submission time or the pick time: ask the organizers; assume the former.

## 3. Idle-machine 5× re-run: CPU budgets and determinism
- Every verifier budget is a process-CPU cap (`ITIMER_PROF` or `process_time`) that keeps
  everything on overrun. So a faster machine can only drop more tests, never cause a miss.
- **Measured.** Every budget was halved (≈ a 2× slower machine: `BR_DIFFEXEC_COMMIT_S=7.5
  _BATCH_S=50 _SHARE=0.425 BR_SUBTEST_S=6 BR_SLOWVERIFY_S=2 BR_GENMEM_S=3`) and doubled
  (≈ a 2× faster machine). Stdout was **byte-identical** against two default runs on sample,
  practice_like, adversarial2 and private264 (md5 28be4a9d / efd708e5 / 4a2b9e5b / 9d9f3f94). No commit sits near a budget edge, so the 5 re-runs give the same selections, and the
  score varies only by CPU noise (about 5–8 s of CPU in total; ±0.5 s).
- Build-time CPU (subtest's per-query `cpu_exec`/`cpu_opt`) feeds the run-time caps and gates.
  The final rebuild fixes them for all 5 runs, and an overrun only keeps tests. No action needed.
- **Don't retune `SCORER_SLOWDOWN` for an idle machine.** Selections don't move under 2× budget
  changes, so the gain would be about 0 and we couldn't validate it. Our CPU is small (4.9 s per
  40 on the board, about 7 s per 60), so an idle machine helps CPU-heavy teams more than us.

## 4. Wall time: 4.5 min for ~60 commits
- No global deadline: only diffexec has a batch cap (100 CPU-s); subtest 6 s, slowverify 2 s,
  genmem 3 s are per commit; each `state.py` call has a 60 s *wall* timeout. ~30 CPU-s/commit
  ceiling plus hung children → nothing bounds 60 commits under 270 s. Stdout is written once,
  after the loop, so a kill = no output = invalid run.
- **Measured** with `09-tools/percommit.py`: per-commit wall including children, on a laptop with
  load average 15–36, which inflates wall. Scorer ≈ ×2–2.5:

| set | n | mean wall | p95 | max |
|---|---|---|---|---|
| sample | 30 | 0.51 | 3.10 | 3.99 |
| practice_like | 24 | 0.65 | 2.48 | 3.04 |
| adversarial | 46 | 0.75 | 2.08 | 4.09 |
| adversarial2 | 65 | 0.78 | 3.32 | 3.90 |
| private | 264 | 0.63 | 3.05 | **13.53** (k7l7: multi, parser + state diff) |

- **Bootstrap of 60-commit batches** from all 429 commits (laptop): median 39 s, p99 66 s, p99.99 92 s.
  On the scorer that is roughly 80 / 150 / **210 s**.
- **The 60 most expensive commits** sum to 180 s on the laptop, so **360–450 s on the scorer, over the limit.**
- Typical case safe (real practice is cheaper still: board CPU 4.9 s / 40); the tail is not.
**R1 (must; ~30 lines; no cost on normal runs).** Add a global deadline in `selector/__main__.py`:
- **Limit:** `4.5 s × n_commits` (180/40 = 270/60), measured from process start. Pass the start
  time through the PYTHONHASHSEED re-exec in an env var.
- **Past 0.40 × limit:** turn off diffexec, subtest, slowverify, genmem and the state diff. Each
  of them only *drops* tests from the static superset, so this cannot cause a miss.
- **Past 0.70 × limit:** emit the cached ALL packing for the rest.
- Clamp child timeouts to the time left. Test: private264 with `BR_DEADLINE_SCALE=0.02` → 0 misses.

## 5. Other things that decide the final
- **R2: the rebuild after the cutoff.** The Dockerfile makes a single `urlopen` of the GitHub
  tarball with no retry. Use 5 attempts with backoff: a transient error at rebuild means no image and no score.
- **R3: config → nothing (~37 s per such commit, ~0 risk).** private config costs 110.5 s with 0
  failing tests: three commits went to ALL.
  - `setup.py` comment: run `setup.py` through the AST no-op filter. Identical AST → nothing; otherwise keep ALL.
  - `MANIFEST.in` delete: return nothing for `MANIFEST.in`, `.gitmodules` and `.gitpod.yml` when
    the reads map lists no reader. Only pyproject.toml, setup.cfg and sqlglotc/setup.py have
    readers, and no test imports root `setup.py`.
  - Keep ALL for pytest sections, conftest, `tests/__init__.py`, pytest.ini, tox.ini. Saves 73.6 s on private264.
- **R4: no crash at start.** `Selector(...)` construction sits outside the per-commit try in
  `__main__`. If maps fail to load, the run crashes and prints nothing. Wrap it, and fall back to ALL for every commit.
- **Edge-format probe** (`09-tools/mkedge.py`, 14 synthetic patches: mode-only, binary, CRLF, empty, rename, copy, symlink, new conftest / tests/__init__, no-newline marker, latin-1, ...): no crash, sane fallbacks; copy and `py.typed` delete go to ALL (conservative).
- **Top-5 gate.** The idle 5× re-run is only for the top 5, so we first have to place top 5 on the normal scorer.

## Recommendations (ordered)
R1 deadline guard (before the final pick) → R2 Dockerfile retry → R3 config → nothing → R4 wrap Selector init → remaining effort on practice-like code commits → freeze and submit by Sun ~04:00 PST; switch only for > 2 s with 0 regression misses; pick explicitly.
