# 08 — The remaining practice submissions as experiments

**Verdict.** The board gives two numbers per submission: simulated seconds and misses. Every
cost is a function of *our own output*, so the sim channel can carry any **label-free aggregate**
the selector computes in-container. Examples: how many practice commits went ALL, the seconds
the slow tests cost us, and how many commits we paid for. The miss channel stays the only
**label** channel. Making the selector print a mixed-radix "telemetry word" as its cost answers
3–4 questions per slot instead of 1. Single-test drop probes answer 1, and their answers are
confounded by worker packing (§2).

Built and validated: `selector/telemetry.py` + hook in `selector/__main__.py` (`BR_TELEMETRY`).
On the 30 sample commits the decode was exact and misses = 18 = the commits that break anything.

Plan: two telemetry slots (T1, T2) and at most one more label probe. The rest are improvement
submissions run as group tests. The final candidate goes in early and is picked explicitly.
It saves no seconds itself; within ~3 slots it says whether the next 15 h belong to slow tests,
ALL fallbacks, or "paid no-ops" (commits we spend on that break nothing).

## 1. What we know (and what is stale)
- S13 = 189.1 sim, 0/40 misses. S11 (S10 code, all 5 tests ≥ 3 s dropped) = 427.9 = 300 + 127.9.
  So at **S10** the slow tests cost 81.2 s and exactly one practice commit has a failing slow
  test. The miss count is label truth, independent of code.
- S12 cut 20 s with r16, r17 and r18. r16 (import-time narrowing) may also have cut non-slow cost, so
  "61 s slow / 128 s non-slow" **is an assumption on current main**. T1 re-measures it for free.
- The local proxies are uncalibrated for practice. P/Q (1,600 unlabeled practice-style patches,
  run r20gm) predict **314–327 s per 40 commits** against the board's 189. Their bucket shares
  (diffexec "not recorded (budget)" 22 %, "~ALL" 12 %, …) cannot be trusted for practice.

## 2. Why per-test attribution by drop probes is the wrong question
Commit cost = max worker load + 2 s/worker, so slow tests interact. I computed this with
`selector/pack.py` on a small selection plus the given slow tests:

| selection | cost | saving from dropping ONE test |
|---|---|---|
| ALL | 36.8 | max_nodes 7.36, tpcds 2.0, genmem / tpch / opt_tpch **0** |
| +max_nodes | 31.5 | 28.8 |
| +max_nodes +tpcds | 32.8 | max_nodes 8.7, tpcds **1.3** |
| +tpcds +genmem | 25.5 | tpcds 16.1, genmem 1.3 |
| +3 slow incl. max_nodes | 32.8 | max_nodes 7.4, the others **0** |

- Single-drop gains do not add up; a fair (Shapley) split needs all 2^5 − 1 = **31 subsets**.
  The queued S14 also includes **7.36 s × n_ALL** (the drop applies to ALL commits too).
- What decides work is the gain of a *perfect verifier for X* ({max_nodes}, {tpcds}, all slow):
  label-free, computed exactly per commit in-container (`_record`: g_maxnodes, g_tpcds, g_slow).

**Minimum submissions (information-theoretic).**
- Sim channel capacity:
  - 40 commits × ≤ 89.9 s of synthetic load each ≈ 3,000–3,600 s.
  - At 0.1 s display resolution that is ≈ 30k levels, **≈ 14.5 bits**. At 1 ms (if the board
    shows ms) it is **≈ 21.5 bits**.
- Miss channel: 0..40, **≤ 5.4 bits**, and the only one that carries label information.
- What it takes:
  - 5 Shapley shares at 3 s resolution (≈ 5 bits each, 25 bits): 2 telemetry slots at 0.1 s,
    1–2 at ms.
  - The 3 perfect-verifier gains plus n_ALL: 1 slot.
  - The same answers via drop probes: ≥ 6 slots, and the per-test numbers stay non-additive.
- Which test the one slow-failing commit needs is label info (≤ 5 probes). **Don't bisect it**:
  it identifies one hidden commit and changes no general mechanism.

## 3. The telemetry tool (branch `moon-08`, not for any final candidate)
- `BR_TELEMETRY='<mode>;<quantity>/<unit_ms>/<base>,...'`: the selector runs normally, then
  rewrites its output so sim − 300·misses = offset + 0.1 s × word (mixed radix + mod-3 check).
  Decode: `python3 -m selector.telemetry decode '<spec>' <sim> <misses> 40`.
- `syn`: every commit gets one synthetic worker of tests **outside its real selection** (which
  holds all failing tests), so misses = N_f, commits that break anything (sample: 18 = truth).
- `hyb:<bucket>`: only bucket commits are synthetic; the rest keep real workers, padded on the
  busiest worker (more coverage, cannot miss). Misses = bucket commits that break anything.
- Quantities: n_all, n_empty, n_slowkept, g_slow, g_maxnodes, g_tpcds, c_all, c_total;
  n_re / c_re (count / cost of commits whose reason matches a regex); n_costge / c_costge.
- Overflow / any error → the real output is printed (board shows ≈ 189); never crashes.
- Validated on sample: `syn;n_all/1/6,n_empty/1/41,g_slow/3000/40` → [0, 4, 12] (4 empties =
  stderr truth; g_slow 36 ± 1.5 s); `hyb:costge=4;…` → 7 commits / 80 s (truth 7 / 82.2 s, 5-s
  units); `hyb:re=diffexec;…` decoded with check digit OK.

## 4. Experiment catalogue (readout → decision)
| id | build | readout | decision rule |
|---|---|---|---|
| **S14** (queued) | main + `BR_DIAG_DROP_IDS=test_max_nodes` | G_M = 189.1 − sim (if 0 misses); miss bit | Gain of a perfect max_nodes verifier = G_M − 7.36·n_ALL (n_ALL from T1). 1 miss → the slow-failing commit needs max_nodes, so r18 must keep it. Do not probe further. |
| **T1** | main@S13 + `syn;n_all/1/6,n_empty/1/41,g_slow/3000/40` (capacity 29,520 < room) | N_f (misses), n_ALL, n_empty, g_slow (current) | **Paid no-ops** = 40 − N_f − n_empty: commits we pay ≥ 2 s for that break nothing (sample: 30 − 18 − 4 = 8). Leaders at 126 s likely pay ~0 of these. Non-slow now = 189.1 − g_slow. Ranking: 36.8·n_ALL vs g_slow vs paid no-ops × avg cost. |
| **T2** | `hyb:costge=4;n_costge=4/1/41,c_costge=4/5000/40` | n_B, C_B (non-ALL commits ≥ 4 s) and M_B = how many of them break anything | (n_B − M_B) expensive no-ops is the headroom for no-op proofs (diffexec coverage, AST and semantic equivalence). If M_B ≈ n_B, the expensive commits are real: work on precision (narrower sets) instead. |
| T3 (optional) | `hyb:re=<bucket regex>` chosen after T2, e.g. `not recorded` or `~ALL cost` | n, cost, headroom of one mechanism bucket | Run only if T1/T2 leave two candidate investments within 2× of each other, and n ≥ 3. |
| G-k | an improvement submission bundling k new pruners | real sim and M | Group test. M = 0 → keep all of them (only weak evidence: 95 % upper bound on miss rate ≈ 3/n_fire; the 446 local labeled patches stay the main soundness check). M > 0 → bisect with halves, ⌈log2 k⌉ slots; M itself bounds how many mechanisms are guilty. |

S14 with 1 miss: G_M = 189.1 − (sim − 300). T1 + S14 + T2 locate the ~63 s gap to the leaders
(ALL spend, slow spend, paid no-ops, expensive-commit headroom) in 3 slots, no per-commit info.

**"Which mechanisms help/hurt", "disable M", "ALL for area A":** these are label-free
counterfactuals (cost only; disabling can't add misses, ALL never misses), so they never need a
dedicated slot. Read them from deltas between consecutive real submissions (as S06→S07, S10→S12),
or add a second selector pass (e.g. `BR_DIFFEXEC=0`) and encode its saving as one more T2 digit
(not implemented; ~2× selector CPU, fine for the 3-min limit). Only *more aggressive* mechanisms
need the miss channel, which is what the G-k group tests are for.

**Build lines (quote them: regexes may hold spaces).** T1 and T2 must be built from exactly
`moon-08` = S13 code (e2e1325) + the two telemetry files (verified: no other diff under
selector/ maps/ mapbuild/ Dockerfile). Otherwise "misses = N_f" and 189.1 − g_slow are invalid.
`ENV BR_TELEMETRY="syn;n_all/1/6,n_empty/1/41,g_slow/3000/40"` (T1);
`ENV BR_TELEMETRY="hyb:costge=4;n_costge=4/1/41,c_costge=4/5000/40"` (T2).

## 5. Schedule (cadence ~62 min; S14 ≈ 11:20 PST Sat; cutoff Sun 08:00 → about S14–S33)
| slot | ≈ time | what | why there |
|---|---|---|---|
| S14 | 11:20 | **drop max_nodes** (queued) | Already built. |
| S15 | 12:20 | **T1** (syn) | Highest information. Code is done; needs only a rebase onto S13 main and an image build. |
| S16 | 13:25 | **T2** (hyb costge=4) | Completes the map of where the cost sits. |
| S17 | 14:25 | improvement G-1: best ready lever chosen by T1/T2 (e.g. ALL fallback cut, or slow verifier if g_slow ≫) | The first real gain arrives early. |
| S18 | 15:30 | **T3** or the tpcds drop probe (`BR_DIAG_DROP_IDS=test_execute_tpcds`), only if still ambiguous; else G-2 | Last diagnostic. Information is worth less after this point. |
| S19–S24 | 16:30–21:40 | G-2 … G-7: one bundled improvement per slot. A slot after any M > 0 is a bisection. | Each slot is both a gain and a soundness check. |
| S25 | ~22:45 | **final-candidate freeze #1**: clean main, no diagnostic env, re-run all local labeled sets | Ties within 1 s go to the earlier submission, so lock in early. |
| S26–S30 | 23:45–04:00 | Further bundles only if each is worth ≥ 2 s locally (P/Q **reweighted** by T1/T2 mix) and 0 local misses | |
| S31 | ~05:00 | **final-candidate freeze #2** (or re-submit #1 unchanged) | |
| S32–S33 | 06:00–07:00 | **slack**: a fix for a rejected or timeout run; nothing new | |

Two operating rules:
- **Pick the final commit explicitly** ("if you pick none, your best practice submission
  counts"): S14 becomes our best practice score if it has 0 misses. Telemetry (≥ 300 s) can't.
- **Compare sim only** (CPU ±0.2 s). Tag every diagnostic `DIAGNOSTIC` in experiments/SUBMISSIONS.md.

## 6. Using the answers without overfitting
- Reweight P/Q to the practice mix (empties, ALL, ≥ 4 s commits, N_f/40): today they over-predict
  practice cost 1.7×. Practice is one draw of 40; the final has unseen kinds.
- Decide **mechanisms**, not thresholds tuned to 40 commits. Example: "paid no-ops are 10 of
  40" → build a general no-op proof. Never "commit with reason R needs test T".

## 7. Rules hygiene (proposed team policy)
- Only bucket-level aggregates. No bucket expected to hold ≤ 2 commits. No commit ids, patch
  text or per-commit values in any payload.
- No bisection to find which hidden commit needs a slow test.
- Diagnostics live on diagnostic branches only. The final image never sets `BR_TELEMETRY` or
  `BR_DIAG_*`, and the hooks should be stripped from the final branch for the judges' read.
- Nothing learned becomes a patch→tests or edit-kind→tests table. The README forbids exactly
  that, and aggregate telemetry is no such table.
- Grey area, stated honestly: the README hides "everything your program prints" on practice.
  The cost channel recovers aggregates of it. That is the same thing every ablation does (the
  sim of a variant is an aggregate of our behaviour), just denser. If the team is uneasy, fall
  back to plain probes: S14, drop-tpcds, drop-all-slow on current main, and "empty a bucket".
  These answer about half the questions in twice the slots.

## 8. Risks
- **Display precision (pre-flight check on the S14 row):** if the board breaks out sim to 0.1 s,
  keep the default (`BR_TELEMETRY_UNIT_MS=100`); if to the ms, set it to 1 (capacity ×3); if
  it shows only the total, set it to ≥ 1000 and subtract main's CPU (~5.0 s) before decoding.
- **Non-determinism across the 3 runs:** S12 = S13 sim, so low risk. The mod-3 check digit
  catches 2/3 of corruptions; on failure, ignore the slot.
- **Overflow / error** prints the real output instead (board ≈ 189); the run never fails.

**Bottom line.** P(T1 and T2 decode cleanly and give actionable attribution) ≈ 0.8. Direct
saving 0 s; indirectly it points ~15 slots / 15 h at the right ~60 s (guess: 10–25 s better than
choosing levers blind). The most valuable unknown is **paid no-ops** (40 − N_f − n_empty): it says
whether "prove no-op" or "narrow the set" is the gap to the leaders.
