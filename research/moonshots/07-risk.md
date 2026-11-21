# Moonshot 07: calibrated expected-value risk on slow tests

## Verdict: NO-GO as a probabilistic dropper. P(it helps on the final) ≈ 5 %. Expected gain ≈ 0 s.

- **The structural evidence can't separate failing slow tests from passing ones well enough.**
  - Data: 470 labeled commits, 509 (commit, selected slow test) rows, 121 of which fail.
  - Grouped 10-fold CV logistic: AUC **0.64**, Brier **0.179** vs **0.181** for intercept-only.
  - Leave-sets-out (train on private + adversarial, score on sample + practice_like): Brier 0.129 vs 0.135.
- **At the EV break-even the model drops nothing.** Break-even is p < saving/300, which is about 3–7 % for tpcds and about 10 % for max_nodes.
  - At mult = 1 (the EV-neutral threshold), CV drops **0 tests**.
  - At mult = 0.5 (twice as aggressive), it saves 81 s across all 470 commits and pays **1 miss (300 s)**.
- **The lever alone cannot take first place.** On practice we are 194.0 and the leader is 126.1, a 68 s gap. Our *entire* slow-test spend is about 61 s.
  - Dropping every slow test still leaves us behind, and gives 1.5 expected misses over 60 final commits, so P(0 misses) ≈ 0.22.
- **Spin-off 1, worth doing soundly rather than probabilistically.** Stratum: a slow test added *only* because the edited function "runs at import", and the test ran 0 touched lines.
  - Record: **0 failures in 36 rows / 12 commits**, about 103 s of single-drop savings, and it is the stratum behind practice_like 4gr2 and ehpl (tpcds, about 9 s each).
  - 12 commits is too few to certify statistically (rule of three gives up to about 25 % per commit). Use it as a target for a sound mechanism, the import-state diff of the import-time call (§5).
- **Spin-off 2.** The "diffexec: not recorded (budget)" stratum fails **13 of 26** times, so it must stay. It is also the largest remaining tpcds spend: 359 s across 23 pool commits, and 1k2u, fq85, tggb and z8gc in practice_like.
  - The lever there is a store with a bigger budget for executor/optimizer functions, not risk.

## 1. Data and features (scripts in `07-risk-tools/`, raw output in `analysis.txt`)

**Data.**
- `harness.py` runs today's selector (main 4826380 + the pinned-image store) over sample, practice_like, private, private41, adversarial and adversarial2.
- It covers 470 commits. 61 of them need a slow test, and the selector already keeps all of those (0 pre-existing slow misses).
- Slow tests are max_nodes 28.8 s, tpcds 21.5 s, genmem 6.7 s, tpch 3.9 s and opt_tpch 3.5 s.

**Features per (commit, slow test).** None of them is an edit kind:
- the mechanism bucket (`eval/reasons.py` buckets, with error/collection ALL split from conservative ALL);
- the number of touched old lines the test ran (lines map);
- the number of enclosing-function lines it ran;
- the popularity of the touched lines;
- diffexec status (none / keep-all / skipped / differs / incomplete);
- whether the change is on an exception path;
- multi-file;
- the marginal packed-cost saving.

The ~600 patches in the brief turned out to be 470 with labels; candB (481) and all120 are unlabeled.

### Strata (selected slow rows → failure rate)

| Stratum | n | fail | rate | Wilson 95 % upper bound |
|---|---|---|---|---|
| all selected slow | 509 | 121 | 0.24 | 0.28 |
| ALL from error / collection | 210 | 68 | 0.32 | – (keep: a broken import fails everything) |
| ALL conservative (base body, other, binding removed, ~ALL), 0 touched lines | 91 | 3 | 0.033 | 0.092 |
| function map, test ran a touched line | 42 | 20 | 0.48 | – |
| function map, 0 touched lines | 54 | 3 | 0.056 | ≈0.15 |
| import-time addition, 0 touched lines | 36 | 0 | 0 | ≈0.10 per row, ≈0.25 per commit |
| diffexec not recorded (budget) | 26 | 13 | 0.50 | – |
| diffexec replayed and differs | 6 | 3 | 0.50 | – |

What this shows:
- Running a touched line is the only strong signal (0.46 vs 0.21).
- The exception-path flag never fired on these one-token mutants.
- Call counts per test are not separable from "ran a touched line" with this store.

**The ALL-conservative stratum.** Its point rate (3.3 %) is below break-even, but the 95 % upper bound is 9 %. Joint dropping there saves 345 s across all 470 commits and pays **2 misses** (private/tiyl `Parser._match` "~ALL cost" and adversarial/del_transforms_func), so net −255 s. It saves 0 s on sample + practice_like, because practice commits do not land in it.

**Lowest-p true failures under CV:**
- tiyl (p ≈ 0.03–0.05, all five slow tests fail);
- fix_delete_empty_init (0.06);
- pqfr (0.095);
- 17ns and qp5p (0.10).

So the bottom 2 % of the probability scale is not clean.

### Pareto (CV p, one drop per commit, ranked by p/saving, all 470 commits)

| Drops | Saved s | Realized misses | Σp | P(0 misses) = Π(1−p) |
|---|---|---|---|---|
| 1 | 7 | 1 | 0.03 | 0.975 |
| 5 | 72 | 1 | 0.38 | 0.67 |
| 10 | 127 | 1 | 0.76 | 0.45 |
| 20 | 208 | 3 | 1.41 | 0.23 |
| 40 | 464 | 6 | 4.15 | 0.01 |

The very first drop (tiyl) is already a miss. At best this buys about 70–130 s per miss, and a miss costs 300 s.

## 2. Legality: "no table from a patch, or from a kind of edit, to the tests it breaks"

**What we must not ship.**
- Learned coefficients fitted on labels that map patch evidence to P(this test fails). They are a soft patch→failures function.
- Per-test intercepts (tpcds 0.33, max_nodes 0.14) are worse: they are a literal test→failure-rate table learned from patches.

**The defensible form.** A hand-written *structural* rule whose premise is execution semantics, and whose only label-fitted part is a scalar threshold.

- This is the same status as `SLOW_SECONDS`, `ALL_FRACTION` and `STATE_MIN_GAIN`, which E006 and E013 already tuned on labeled sets.
- Example: "a slow test that ran no touched statement and reaches the edit only through an import-time call is dropped when the import-state diff of that call is empty."
- Labels are used to *validate* (count misses), never to fit shipped weights.
- The rule has no kind, file or test identity in it, so it says nothing about which tests a given patch breaks.

**Conclusion.** The defensible form is exactly a *sound* mechanism. Once it is sound, it needs no probability. The legal version of this moonshot therefore collapses into ordinary selector work (§5).

## 3. Expected score vs variance on the final (~60 commits)

**Misses don't add variance across reruns.**
- The simulated cost is deterministic. A miss happens in all 5 reruns or in none.
- Variance comes only from which unseen commits the final draws, plus about ±0.5 s of CPU noise.
- Whether a team wins by 1 s comes down to commit-set luck and CPU, not to rerun noise.

**Our final at today's selector.**
- Practice 194.0 (189.1 sim + 4.9 CPU) × 1.5 ≈ **291 s**.
- Leader 126.1 × 1.5 ≈ **189 s**.
- Spending the whole slow budget saves about 92 s and expects 1.5 misses (S11: dropping every test ≥ 3 s gave 1 miss in 40). Expected score 291 → about 650 s.

**Monte Carlo.**
- `win.py`, 20k draws. Commit-set shock N(0, 8) shared by every team, then N(0, 6) per team. 5 leaders at 1.5 × {126.1, 130.3, 134.3, 135.3, 139.9}, each with probability q of at least one miss on the final's new kinds.
- f = fraction of slow spend dropped.
- Model A: misses ~ Poisson(1.5 f), i.e. no better than random, which is what AUC 0.64 delivers.
- Model B: an oracle-like rule that keeps the one needing commit 90 % of the time.

| Our practice-equivalent base (with slow) | q | f=0 | f=0.5 A / B | f=1 A / B |
|---|---|---|---|---|
| 194 (today) | 0.2 | 0.000 | 0.000 / 0.000 | 0.03 / 0.10 |
| 194 | 0.4 | 0.010 | 0.005 / 0.010 | 0.05 / 0.19 |
| 170 (non-slow cut to ~109) | 0.2 | 0.000 | 0.006 / 0.012 | 0.23 / 0.90 |
| 155 | 0.2 | 0.000 | 0.29 / 0.60 | 0.22 / 0.91 |
| 145 | 0.2 | 0.001 | 0.46 / 0.94 | 0.22 / 0.90 |

How to read the table:
- P(first) is a step function of cost, capped by P(0 misses). Model A at f = 1 can never exceed e^(−1.5) ≈ 0.22.
- Risk only pays when the rest of the selector is already within about 15–45 s of the leader. In that case the right move is the *smallest* drop set that clears the leader projection, not the EV threshold.
- At today's base, no threshold gives a meaningful P(first): f = 1 under the unrealistic model B gets 0.10–0.19, and under realistic model A 0.03–0.05.

## 4. Threshold that maximizes P(first)

Order candidates by p/saving and drop the shortest prefix whose cumulative saving S satisfies:

    our_projection − S < leader_projection − margin

then stop. Using today's numbers:

- **Today.** The gap is 68 s (practice) and the full slow spend is 61 s, so the inequality has no solution. **The P(first)-optimal threshold is "drop nothing."** We keep the 0-miss record, which is worth the most if leaders miss new kinds; q is unknown and our 111-patch adversarial record is the edge.
- **If other moonshots bring the non-slow cost to ≤ about 110 s/40**, a gap of ≤ 15–30 s remains. The prefix is then about 5 drops on the Pareto (P(0 misses) ≈ 0.67). In model-A terms that is P(first) ≈ 0.3–0.45, vs ≈ 0 without risk, so taking the gamble becomes correct.
- That decision belongs to the last submission. Even then it should be expressed as one global threshold on a structural rule (§2), never as fitted weights.

**Margin.** Ties within 1 s go to the earlier submission, and the commit-set shock is mostly common to all teams. A 3–5 s margin covers the per-team noise (σ ≈ 6 s for 60 commits is our assumption, not a measurement).

## 5. What to do instead (sound, rule-compliant)

1. **Import-time addition → state-diff gate** (stratum 0/36).
   - When a function-map selection adds tests because the function "runs at import (+N)", run the existing import-state diff (state.py) on the patched import.
   - An **empty** diff means the import-time call produced identical state. Then only run-time readers (line map) are needed, so slow tests that ran no touched line drop.
   - This generalizes I25 (already sound for sqlglot-only roots).
   - practice_like: 4gr2 and ehpl tpcds, about 9 s each over 24 commits, so maybe 10–20 s per 40 practice commits.
   - CPU: about 1.2 s per diff on EC2 (about 2.4 s on the scorer), so gate it on saving ≥ 4 s.
   - Smallest test: run the gate on practice_like + private and confirm 0 new misses on all six sets.
2. **diffexec budget for executor/optimizer functions** (13/26 fail, so only replay can separate them).
   - These are 1k2u, fq85, tggb and z8gc in practice_like, worth 12–18 s each.
   - Raising per-function caps for the functions tpcds calls, and lazy-loading those blobs, is the sound path. Store size is not the constraint: the image has no size limit, but the repo ≤ 20 MB applies unless the build fetches the store.
3. **Don't ship the logistic.** It is near-useless (AUC 0.64), legally exposed, and its EV-neutral threshold drops nothing.

## Numbers in one place

- 470 labeled commits; 509 selected slow rows; 121 fail; 61 commits need a slow test.
- Pool slow spend: 2558 s.
- CV AUC 0.64; Brier 0.179 vs 0.181.
- EV-neutral rule: 0 drops. 2× aggressive: +81 s, 1 miss.
- Today: no threshold gives P(first) > about 0.05 under realistic model A. The gamble only becomes rational if the non-slow cost reaches about 110 s/40.
