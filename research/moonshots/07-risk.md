# Moonshot 07: calibrated expected-value risk on slow tests

## Verdict: NO-GO for expected score. On P(first) it is a ~3–5 % corner gamble. P(this helps) ≈ 5 %; expected gain ≈ 0 s.

- **Structural evidence can't predict which selected slow tests fail.**
  - Data: 470 labeled commits, 509 (commit, selected slow test) rows, 121 of which fail (24 %).
  - Grouped 10-fold CV logistic: AUC **0.56**, Brier **0.188**. Intercept-only is 0.181, so the model is *worse* than the base rate.
  - Leave-sets-out (train on private + adversarial, score on sample + practice_like): 0.113 vs 0.135. That is n = 27 rows with 4 failures, too few to rely on.
- **At the EV break-even the model drops nothing.**
  - Break-even is p < saving/300: about 3–7 % for tpcds, about 10 % for max_nodes.
  - At mult = 1 (the EV-neutral threshold), CV drops **0 tests**.
  - The lowest-p true failures sit at p ≈ 0.06 (adversarial/fix_delete_empty_init) and 0.11 (private/17ns).
- **The lever alone cannot reach first place.** On practice we are 194.0 and the leader is 126.1, a 68 s gap. Our whole slow-test spend is about 61 s.
- **Answer to (3).** The two objectives disagree:
  - Expected score / rank: best threshold = drop nothing.
  - P(first): best threshold = **drop every slow test**, a corner solution, worth only P(first) ≈ 0.03–0.05 today. Any interior threshold is worse than both corners (§3–4).
- **Spin-off (sound, not probabilistic).** Stratum: a slow test added only because the edited function "runs at import", having run 0 touched lines.
  - Record: 0 failures in 36 rows / 12 commits, 103 s of single-drop savings. It includes practice_like 4gr2 and ehpl (tpcds, about 9 s each).
  - Target it with the import-state-diff gate (§5), not a probability. The saving is unverified.

## 1. Data and features (`07-risk-tools/`: harness.py, analyze.py, win.py, analysis.txt)

**Data.**
- `harness.py` runs today's selector (main 4826380 + the pinned-image store) on sample, practice_like, private, private41, adversarial and adversarial2.
- That is 470 labeled commits. candB (481) and all120 have no labels.
- 61 of the 470 need a slow test, and the selector keeps every one of them (0 slow misses today).
- Slow tests are max_nodes 28.8 s, tpcds 21.5 s, genmem 6.7 s, tpch 3.9 s and opt_tpch 3.5 s.

**Features.** None of them is an edit kind:
- the mechanism (`eval/reasons.py` buckets, with real error/deleted/config ALL split out);
- touched old lines the test ran;
- enclosing-function lines it ran;
- popularity of the touched lines;
- diffexec outcome;
- exception path (never fired on these one-token mutants);
- multi-file.

| Stratum (selected slow rows) | n | fail | rate |
|---|---|---|---|
| all | 509 | 121 | 0.24 |
| test ran a touched line | 61 | 28 | 0.46 |
| ran 0 touched lines | 448 | 93 | 0.21 |
| function map, 0 touched lines | 54 | 3 | 0.056 (Wilson 95 % upper bound ≈ 0.15) |
| **import-time addition, 0 touched lines** | 36 | 0 | 0 (≈ 0.25 per commit by the rule of three) |
| ALL: base-file class/module body | 60 | 5 | 0.08 |
| ALL: `import sqlglot` lines | 50 | 10 | 0.20 |
| ALL: binding removed | 80 | 26 | 0.33 |
| ALL: real error / deleted / config | 65 | 20 | 0.31 |
| ALL: ~ALL cost | 25 | 5 | 0.20 |
| fixture → readers | 22 | 6 | 0.27 |
| diffexec "not recorded (budget)" | 26 | 13 | 0.50 |
| diffexec replayed and differs | 6 | 3 | 0.50 |

**Running a touched line is the only signal, and it points the wrong way for us.** The cheap-to-drop rows (0 touched lines) still fail at 21 %.

**Conservative ALL is not safe.**
- Joint-dropping every conservative-ALL slow test that ran 0 touched lines saves 870 s across the pool and costs 17 misses.
- An earlier mislabeled split had shown this stratum at 3/91. That was wrong (fixed and re-run).

**Pareto.** CV p, one drop per commit, ranked by p/saving, all 470 commits:

| Drops | Saved s | Real misses | Σp | Π(1−p) |
|---|---|---|---|---|
| 5 | 90 | 0 | 0.60 | 0.53 |
| 10 | 145 | 1 | 1.13 | 0.30 |
| 20 | 293 | 5 | 2.77 | 0.05 |
| 40 | 555 | 13 | 6.6 | 0.001 |

The best case buys about 90–145 s per miss on the pool, and each miss costs 300 s.

**Distribution shift.** 13 % of pool commits need a slow test, vs 1/40 = 2.5 % on practice (S11). Pool-calibrated p's are therefore about 5x pessimistic for practice-like commits, so the Pareto above is pool-only. The final projections below anchor on S11 instead.

## 2. Legality: "no table from a patch, or kind of edit, to the tests it breaks"

**What we must not ship.**
- Fitted coefficients mapping patch evidence to P(this test fails). That is a soft patch→failures function learned from labels.
- Per-test intercepts (tpcds 0.33, max_nodes 0.14) are worse: a literal test→failure-rate table learned from patches.

**The defensible form.**
- A hand-written *structural* rule whose premise is execution semantics, with a single scalar threshold that labels only validate.
- This is the same status as `SLOW_SECONDS`, `ALL_FRACTION` and `STATE_MIN_GAIN`, which E006 and E013 tuned on labeled sets.
- No kind, file or test identity in the rule. Labels count misses; they never fit shipped weights.

**Consequence.** A defensible rule must be *sound*, and a sound rule needs no probability. The legal version of this moonshot is ordinary selector work (§5).

## 3. Expected score vs variance (final ≈ 60 commits)

**Reruns don't resample misses.**
- Simulated cost is deterministic, so a miss happens in all 5 reruns or in none.
- Variance comes from which commits the final draws (shared by every team) plus about ±0.5 s of CPU.

**Projections, anchored on practice × 1.5:**
- Ours: 194.0 → **≈ 291 s**. Leader: 126.1 → **≈ 189 s**.
- Dropping all slow tests saves about 92 s, with 1.5 expected misses (S11: 1/40). Expected score rises to about 650 s; P(0 misses) ≈ e^(−1.5) ≈ 0.22.

**Monte Carlo (`win.py`, 20k draws).**
- Commit-set shock N(0, 8) shared by all teams, plus N(0, 6) per team (assumed, not measured).
- The top 5 practice scores × 1.5, each with probability q of at least one miss on the final's new kinds.
- f = fraction of slow spend dropped.
- Model A: misses ~ Poisson(1.5 f), i.e. no better than random, which matches AUC 0.56.
- Model B: an oracle-like rule that keeps the needing commit 90 % of the time. It is optimistic.

| Base (practice, with slow) | q | f=0 | f=0.25 A/B | f=0.5 A/B | f=1 A/B |
|---|---|---|---|---|---|
| 194 (today) | 0.2 | 0.000 | 0.000/0.000 | 0.000/0.000 | **0.026**/0.104 |
| 194 | 0.4 | 0.010 | 0.006/0.009 | 0.005/0.010 | **0.046**/0.186 |
| 170 (non-slow ≈ 109) | 0.2 | 0.000 | 0.001/0.000 | 0.006/0.012 | 0.226/0.900 |
| 155 | 0.2 | 0.000 | 0.007/0.011 | 0.294/0.596 | 0.222/0.905 |
| 145 | 0.2 | 0.001 | 0.185/0.261 | 0.460/0.935 | 0.223/0.900 |

## 4. Threshold that maximizes P(first)

**P(first) is a step function of cost, capped by P(0 misses).** The optimal rule is therefore: drop the shortest prefix, ordered by p/saving, whose saving clears the leader projection plus a margin of about 3–5 s. If no prefix clears it, take the corner that comes closest.

**Today.**
- No prefix clears the 68 s gap: the whole slow spend is only 61 s.
- **The P(first)-max choice is f = 1 (drop all slow): 0.026–0.046 under model A, vs 0.000–0.010 at f = 0.** Intermediate f is worse than both corners, because it pays miss risk without getting under the leader.
- f = 1 costs about +360 s of expected score, and with it almost surely the top-5 re-run. Which objective governs is the team's call.
- Our recommendation is to keep f = 0. The 0-miss record across 111 adversarial patches is our real edge if leaders miss new kinds (q), and a 3–5 % lottery does not justify giving it up.

**If other work brings non-slow cost to about 110 s/40** (base about 155–170 with slow): an interior f ≈ 0.5–1 maximizes P(first), at about 0.3 (model A).
- Ship it then as one global threshold on a structural rule, last submission only.
- Margin: ties within 1 s go to the earlier submission, and the commit-set shock is mostly common to all teams, so 3–5 s covers the per-team σ.

## 5. Do instead (sound, rule-compliant)

1. **Import-time addition → import-state-diff gate** (stratum 0/36).
   - When a function selection adds "runs at import (+N)" users and the import-state diff of the patched import is empty, keep only run-time readers. Slow tests that ran no touched line then drop.
   - I25 already does this for sqlglot-only roots. First check why it did not fire on 4gr2 and ehpl: test-module roots, or the marginal-cost gate.
   - Possible gain: practice_like 4gr2 and ehpl ≈ 18 s per 24 commits, unverified.
   - CPU: about 2.4 s on the scorer per diff, so gate on saving ≥ 4 s.
   - Validate with 0 new misses on all six sets.
2. **diffexec recording budget for the functions tpcds calls** ("not recorded (budget)" fails 50 %, so only replay can separate those rows).
   - Affects practice_like 1k2u, fq85, tggb and z8gc, 12–18 s each.
   - The repo ≤ 20 MB cap is the constraint, not the image.
3. **Don't ship the logistic.** AUC 0.56, legally exposed, and it drops nothing at break-even.
