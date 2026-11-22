# Moonshot 26: build-time headroom + bundle of the parked wins (branch `moon-26`)

## Verdict (numbers first)

- **Clean build on rwx (final_check C4 shape: --no-cache, 4 CPUs, 8 GB): cand-h 169.3 s → moon-26 113.5 s**,
  with moon-22 and moon-25 included (≈ 227 s on the 2x scorer, cap 600). Without the build work, cand-h +
  moon-22 + moon-25 would be ≈ 169 + 67 + 12 ≈ 250 s on rwx (≈ 500 s scorer).
- **Ported:** moon-22 (per-optimizer-rule TPC checkpoints), moon-25 (executor generate() checkpoint).
  **Dropped:** moon-15 (fixture-case slicing): its build needs duckdb + pandas (PyPI download in the image).
- **0 misses on every labeled set** (sample, practice_like, private, private41, adversarial, adversarial2) at
  each step; labeled sims identical to cand-h (private +0.006 s on one image = a diffexec timing keep on hgxu,
  see below).
- Repo (git archive): 17.32 → 17.45 MB (≤ 18 MB).
- **Head merges origin/cand-i** (r34, a select.py change that does not overlap). Unit tests: 225/225.
  imgcheck of 79146f1: 0 misses on all six sets.
  - Sims equal ffddeb3's, except adversarial2: 867.1 → 845.8, which is r34's own gain.
  - final_check on 79146f1: FINAL_FC.
- **Same-box A/B, idle rwx (final_check sets, one container at a time, scorer flags;
  `research/moonshots/26/abcpu.sh`).** base = cand-h b048c6a, new = ffddeb3. `gen0` = new with
  `BR_SUBTEST_GEN=0` (moon-25 off). `off` = new with moon-22 and moon-25 off. Container child CPU, sim s:

  | set | base | off | gen0 (+moon-22) | new (+moon-25) |
  |---|---|---|---|---|
  | practice_like (120) | 677.0 / 36.82 | 677.0 / 37.03 | 677.0 / 33.36 | 677.0 / 33.04 |
  | private (264) | 2016.8 / 66.09 | 2016.8 / 65.68 | 2016.8 / 61.42 | 2016.8 / 61.27 |
  | final_check batch P (60) | 293.5 / 18.15 | 293.5 / 18.23 | 293.5 / 17.06 | 293.5 / 17.09 |
  | final_check batch Q (60) | 501.3 / 26.31 | 501.3 / 27.22 | 501.3 / 23.32 | **479.8** / 24.80 |

  **Net per 40 commits = Δsim + 2 × ΔCPU:**
  - **moon-22 (off → gen0):** CPU only, no sim change. practice_like −2.4 s, private −1.3 s, batch P −1.6 s,
    batch Q −5.3 s per 40. **Kept.**
  - **moon-25 (gen0 → new):** CPU ≈ 0 on practice_like, private and batch P. On batch Q, one new drop:
    sim −21.5 s for CPU +1.5 s, so ×2 = +3.0 s and net −18.5 s per 60.
    On rwx-big PQ (same image and store, `BR_SUBTEST_GEN` 0 → 1), Q gives sim 6274.0 → 6239.2
    (−34.8 s per 1000 = −1.4 s per 40) at CPU 370.6 → 370.1; P is unchanged.
    The two new drops are 6b24 (−13.3 s, through gen + rule checkpoints together) and e74m (−21.5 s).
    moon-25 ground-truthed both (they PASS). **Kept.**
  - Total vs cand-h: practice_like −2.5 s per 40 (CPU). Batch Q −21.5 s sim and −1.5 s CPU per 60.
  - The final_check runs on rwx (cand-h b048c6a6ff38 and moon-26 ffddeb3, run at different times) agree:
    practice_like CPU 36.74 → 33.35 s, private 64.99 → 61.01 s, C7 P 18.11 → 16.99 s,
    C7 Q 26.41 → 24.79 s with sim 501.2 → 479.7.
- **rwx-big /shared/PQ, whole sets, loaded box.** Shared with other agents' imgchecks, at load 3–10.
  - P sim 3876.4 for every image. CPU: cand-h 233.2 s, moon-26 217.9 s, moon-22 and moon-25 off 266.0 s
    (the last of these is inflated by load).
  - Q sim: cand-h 6220.3, moon-26 6239.2.
    The 1000-commit sums are dominated by single threshold flips worth 20–30 s each, in both directions. See
    "Noise" below.

## 1. Build profile and what changed

Per-step profile of cand-h's big RUN (`research/moonshots/26/prof.sh`, 4-CPU container on rwx, steps serial as
in the Dockerfile):

| step | wall | notes |
|---|---|---|
| FROM + fetch_sqlglot + COPY | 4.9 s | download is fast; nothing to win |
| compileall -j4 (stdlib, sqlglot, selector) | 1.7 s | |
| selector.maps build | 0.5 s | |
| selector.nameindex build (+ state snapshot) | 34.5 s | single-threaded |
| selector.subtest build | 74.3 s | single-threaded, serial over 121 queries |
| selector.slowverify build | 51.9 s | single-threaded |
| selector.diffexec resources | 0.7 s | |
| chmod | 1.3 s | |
| **total** | **165 s** | all four store builders run one after another on 1 of 4 cores |

None of the four store builders reads another's output (each writes its own file under /app/index; checked:
the build entry points only read /opt/sqlglot and the repo; `slowverify`'s stored `cpu` is never read at run
time). So:

1. **Dockerfile: the four builders run side by side** in the one RUN (`& p=$!` per job, then
   `wait $p1 && wait $p2 && ...` so any failure fails the build; a bare `wait` would hide it).
   compileall and maps stay serial and first (the builders import from the .pyc).
2. **subtest build: fork pool over queries** (`BR_SUBTEST_JOBS`, 3 in the Dockerfile, default 1). Every
   query already ran in fresh forks of the builder process, which runs nothing between queries, so a query's
   result does not depend on which worker or in which order it runs. `imap` keeps fixture order; ck members
   are keyed by fixture index (was `len(queries)`, same value).
3. **moon-22's two seed interpreters** (`PYTHONHASHSEED` 1 and 2 boundary digests) run side by side
   (≈ 11 → 6 s).

With moon-22 the subtest build is 127 s serial (rwx, alone) → **58 s with 3 workers**.

**Store equivalence (pooled vs serial, `research/moonshots/26/cmpstore.py`):** all 831 zip members
byte-equal except meta.pkl; meta.pkl equal outside the timing fields (`cpu_exec`, `cpu_opt`, `cpu_stage`,
`cpu_from`, `calib`, `cpu_steps`). Normalized timings, pooled/serial: median 0.93–0.97, p10 ≈ 0.6, p90 ≈ 1.1–1.6
(raw `calib` 1.7x: 4 vCPU = 2 physical cores, so concurrent processes slow each other; `_norm` corrects most
of it). Timings only feed the gate / caps (an underestimate → the CPU itimer fires → keep), never soundness.

Build times (rwx, `research/moonshots/26/btime.sh` = final_check C4 command):

| ref | what | build |
|---|---|---|
| cand-h b048c6a | baseline | 169.3 s |
| 8754be6 | parallel RUN + moon-22 | 150.5 s |
| d013d8d | + subtest pool (3) + parallel seeds | 111.4 s |
| ffddeb3 | + moon-25 | 113.5 s |
| 79146f1 | + merge origin/cand-i (r34) = head code | FINAL_BT |

The remaining build is ≈ throughput-bound: ≈ 240 CPU-s of builders on 4 vCPU (2 physical cores).
Further wins would need less CPU (e.g. nameindex 35 s), not more parallelism.

## 2. Ports

### moon-22 (per-optimizer-rule checkpoints) — kept
- Merge of `origin/moon-22` (one textual conflict: the drop/keep message; merged both).
- Unit tests 223/223 (incl. r29's 15 stage tests).
- Net CPU win in every set (table above). No new drops on the labeled sets or practice-style batches.

### moon-25 (executor generate() checkpoint) — kept
- Semantic merge with moon-22: the run-time query tuple is now
  `(suite, i, opt, mode, parse_hit, optb_hit, duck_hit, k, gen_hit)`; `_cpu` adds gen replay CPU on top of the
  checkpoint (`cpu_from[k]`) or stage CPU; the child's itimer = 3 × (checkpoint-or-stage CPU + gen CPU) + 0.8.
  gen replay runs after the optb check (rule checkpoint or moon-05 stage), as in moon-25.
  Its build-time `gen_timed` uses cand-h's calibrate-after convention (as `stage_timed`).
- Two new Q drops (6b24 and e74m), both ground-truthed by moon-25. Its CPU cost is ≈ 0 except when a query
  actually replays.

### moon-15 (fixture-case slicing) — dropped
- Its build imports `tests/test_optimizer.py`, which imports `duckdb` and `pandas` at module top and uses
  duckdb in `setUpClass` and non-case asserts. The scorer image is bare python:3.13-slim: landing it means a
  PyPI download of duckdb + pandas + numpy in the build (new network dependency at the final rebuild,
  ≈ +20–40 s) plus ≈ 6.6 CPU-min of build (5.6 CPU-min case tracing + 1 min warm pass) ≈ +150 s on
  rwx → ≈ 265–300 s, over the 260 s budget, for ≈ −1…−2 s/40. Stubbing duckdb would change setUpClass
  coverage (and break non-case asserts), so it is not a safe shortcut.

## Noise and a small per-verifier tax
- **Q_e9bs and Q_dn9c** (errors.py `highlight_sql`) sit on slowverify's 2.0 s CPU itimer (`BUDGET 4 s / 2`).
  - Inside the 1000-commit batch the verification takes ≈ 2.0 s CPU. Alone it takes 0.19 s, and re-running it
    alone on both images gives a drop both times.
  - cand-h passed at 2.02 s. All moon-26 images failed at 2.03–2.05 s, which costs +29 s on that commit.
  - **This is systematic, not noise.** Paired per commit (h26 → moon-26 images, commits where subtest did not
    fire), diffexec child CPU has a median ratio of 1.01–1.026 and slowverify of 1.01.
  - So the moon-26 images carry a ≈ 1–2.5 % tax on the other verifiers. The likely cause is the larger
    resident subtest meta (moon-22's per-line `fs` maps), which lengthens `_swap`'s `gc.get_objects()` walk.
  - This tax is already included in the net CPU table above, and the net is still clearly negative.
  - Near-cap commits will occasionally flip to keep.
  - The 10x batch-vs-alone gap is a pre-existing fragility worth its own look. Cheaper `_swap`, or not keeping the
    subtest meta resident, would also remove this tax.
- **Q_1h4b** depends on store timing.
  - cand-h and c26 keep it: moon-05's fallback CPU goes over FALLBACK_CAP 1.5 s, and the per-query
    `cpu_exec` values in the store vary from build to build.
  - a26 and b26 drop it.
  - Ground truth PASSES, so neither decision is wrong. It is not a moon-22 gain.
- **private hgxu**: diffexec child 0.67–0.69 s vs a 0.78 s budget; under imgcheck load one image kept one more
  test (+0.006 s). Alone, both images give the identical selection.
- rwx-big was shared with other agents' imgchecks during these runs (load 3–10).

## Ground truth
- **Q_1h4b** is dropped by some moon-26 builds (the `unnest` condition changes from `and` to `or`).
  Applied with the selector's patcher, the three TPC executor tests **PASS** (79 subtests).
- moon-25's two drops, 6b24 and e74m, PASS in moon-25's own ground truth.

## Robustness of the build time
The ×2 scorer figure assumes the scorer gets rwx's parallel speedup. Even with no speedup at all (1 core), the
build cannot take longer than the serial sum, ≈ 250 s on rwx, ≈ 500 s at 2x. That is still under the 600 s cap.

## Reproduce
- `research/moonshots/26/prof.sh REF TAG` (rwx): per-step build profile.
- `research/moonshots/26/btime.sh REF...` (rwx): final_check C4 clean build time.
- `research/moonshots/26/sub.sh REF JOBS` (rwx): the subtest build alone, timestamped; `cmpstore.py A B`.
- `research/moonshots/26/pqrun.sh TAG [LABEL -e VAR=..]` (rwx-big): image on /shared/PQ/{P,Q} with the scorer's
  flags, sim + container child CPU. Labeled sets: `~/shared/imgcheck.sh` (https clone variant `~/ic26.sh`).
