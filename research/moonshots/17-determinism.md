# Moonshot 17 — Determinism audit

**Verdict.** On a quiet machine main's output is byte-identical from run to run. On EC2, in
the pinned image with a 2-CPU container, every run so far gave the same output (see "EC2
reruns"), including runs where 6 busy loops shared the container's 2 CPUs. Every flip we saw
(2 commits out of ~470 laptop commit-runs) came from a **wall-clock guard that fired while the
host was stalled** (load average 35–80, swapping 5 GB). We then reproduced the op07 flip on
demand: main flips it in 2/2 runs at a 0.05-CPU quota, and `moon-17` flips it in 0/2. CPU-time budgets, hash seeds, blob loading
order and digests gave identical decisions in every run. Fixed on `moon-17`: wall clocks no
longer decide anything. Each child is capped by its own CPU (ITIMER_PROF / RLIMIT_CPU), and the
wall timeout only catches a child that blocks. A capped child that overruns still keeps
everything. **Seconds impact: 0 on a quiet machine (same output, same CPU). Under throttling or a stall,
each fixed flip is worth 5–18 s (op07 5.2 s, ktz3 4.9 s, tggb 18.3 s). On EC2, base flipped
tggb in 2/2 throttled practice_like runs; fix had 0 flips in 30 full-set runs (12 of them throttled).** On the final (5 reruns on an idle
machine, median taken) the expected gain is small, but nothing timing-based is left to decide
an outcome except at pathological margins.

66ks does **not** reproduce: 12/12 runs (laptop, under 6 load modes) give `diffexec: dropped 46`,
2.1 s. Moon-06's "dropped 46 / dropped 47" compared **two different programs**: main against
STMT_SECONDS=0, which changes diffexec's candidate set (test_execute_tpcds lands in diffexec's
jobs instead of subtest's). That is not run-to-run noise.

## Method

* Laptop (i7-13700H, 6P+8E cores, SMT): `python -S -m selector` with the run-time index built
  from origin/main. `runsel.py` maps the store's recorded `/opt/sqlglot` paths onto the local
  checkout; without it 11 blobs of 66ks fail with FileNotFoundError, which is a local-only
  artefact. Load modes: idle, 8–40 busy loops, pinned to 2 E-cores (≈1.3× CPU), E-cores plus
  busy loops on the same cores, and pinned to one P-core's two SMT threads with a busy loop on
  each.
* The laptop crashed twice under memory pressure from other agents, and the coordinator then
  moved all heavy work to EC2 (`drun.sh`, one container). See "EC2 reruns" for those numbers.

## Results (main = origin/main 80d3b31 ≡ bd16852 for selector/diffexec code)

| set | runs (modes) | distinct outputs | flips |
|---|---|---|---|
| sample (30) | 6 (idle×2, stress40, ecore, ecore+stress, pinned-SMT) | 2 | ktz3 in the pinned run: the state child's 60 s **wall** timeout fired during a host stall (449 s wall for 9 s of CPU). Result: "import-state diff unavailable", 3→130 tests, +4.9 s |
| private41 (41) | 5 (idle×2, stress8, ecore, ecore+stress) | 2 | op07 under stress: `diffexec: no answer`. The child needs ≈1.0–1.3 s of CPU and has budget 4.42 s, so the wall guard is 9.4 s. It fired during swap stalls: 4→127 tests, +5.2 s. Not reproducible in isolation (6 more runs under 8 and 24 busy loops) |
| practice_like (51), adversarial2 (65) | see EC2 | | |

* **Blob verdicts are bit-stable.** All 20 commits that ran a diffexec replay gave the same
  `blobs ok/total` in every run (e.g. 66ks 2531/2543, ary6 1316/1398, op07 867/873).
* **CPU-time margins (instrumented `moon-17` notes, E-cores ≈ 1.3× slower).** diffexec child
  CPU / per-commit budget ≤ 0.35 (ary6 2.6/7.6 s, 66ks 2.7/15 s). `late` (blobs skipped at the
  CPU deadline) = 0 on every commit. genmem 0.56–0.67 s against a 3 s cap. subtest re-run
  0.52 s. slowverify never reached verification.
* **CPU time is not load-invariant on SMT hosts.** Under the stress-8 run, private41 user CPU
  went from 15.0 to 28.4 s, because SMT siblings share a core and `process_time` almost
  doubles. Every budget is therefore checked against ≥2× headroom (see the table).
* **PYTHONHASHSEED.** On sample with BR_DIFFEXEC=0 (no re-exec), parent seeds 1, 2 and 3 gave
  byte-identical stdout. Every child (diffexec fork/subprocess, state, genmem, subtest build)
  runs with seed 0.

## Inventory: every timing- or order-dependent decision

| # | where | decides | kind | main | risk | fix on moon-17 |
|---|---|---|---|---|---|---|
| 1 | diffexec `_replay_fork` parent | whole replay → None | **wall** budget+5 s | observed flip (op07); reproduced at 0.05 CPU | high on a stalled host | wall guard min(4·b+30 s, global budget left); child RLIMIT_CPU 2·b+3 s (SIGXCPU → keep) |
| 2 | state `_run` | import-state diff → None (→ ALL / no narrowing) | **wall** 60 s | observed flip (ktz3) | high on a stalled host | RLIMIT_CPU 60 s set in the child's `_main` (no preexec_fn, so no slower fork path), wall min(180 s, global budget left) |
| 3 | subtest / slowverify `verify` parent | verification → keep | **wall** 2·b+2 s | not observed | medium | wall 4·b+30 s; the child's ITIMER_PROF already caps CPU |
| 4 | genmem `run_child` | keep | **wall** 2·b+5 s | not observed | medium | 4·b+30 s; ITIMER_PROF in the child |
| 5 | diffexec child: `process_time() > deadline` per blob | blob → not ok | CPU | late=0 everywhere, ≤0.35 of budget | low (flips only near the budget) | kept; `late` counter in the note. Proposal below |
| 6 | diffexec child: `cpu_new <= 5·cpu_old + 0.05`, per-call cap 10·cpu_old+0.2 | blob → not ok | CPU ratio, single sample | did not fire in our runs (retry=0); likely 66ks mechanism under swap | medium under memory pressure | one re-measure (min of two pairs) before a timing-only rejection |
| 7 | diffexec `BATCH_BUDGET_S - spent` | budget for later commits | CPU, cumulative | spent ≤ 4 s/set ≪ 100 | low | kept |
| 8 | subtest per-query ITIMER 3·cpu_q+0.8, total min(6, 2.5·est+1) | verification fails → keep | CPU against **build-time** CPU | 0.52 s used | low if build = run hardware | kept (see rebuild note) |
| 9 | subtest plan `spend ≤ min(12, 0.6·gain)` with build-time `cpu_exec` | which TPC tests are verified | **build-time timing** | fixed per image | different across *rebuilds* | proposal: commit the per-query estimates |
| 10 | genmem ITIMER 3 s, est constants | keep | CPU | 0.6/3 s | low | kept |
| 11 | parent PYTHONHASHSEED | set iteration order | seed | seed-independent (3 seeds) | none | — |
| 12 | effect digests (`FastCmpPickler`) of sets / frozensets of id-hashed objects | blob → not ok | ASLR address order | identical blob counts in every run | low (only sets with >~256 id-hashed members reach page bits) | proposal: sort set members by digest |
| 13 | `gc.get_objects()` order in replay/subtest `_swap` | which live functions are swapped | allocation order | all swapped, so order does not matter | none | — |
| 14 | id()-keyed memos (names.py, testimport.py, state.py) | caches | object identity | trees and modules are kept alive, so no id reuse | none | — |
| 15 | tracemalloc growth (genmem) | keep | allocator | limit 256 B against a 32 B baseline | low | — |

## Proposals not implemented (each keeps the "overrun → keep" rule)

* **Deterministic diffexec work budget (#5).** Replace the CPU deadline with a work counter
  that is a pure function of the store: Σ bytes of replayed blobs + k·blobs. On the commits we
  measured, replay CPU ≈ 0.6–1.1 s per MB on P-cores (66ks 1.78 MB→1.5 s, ary6 2.57 MB→2.0 s,
  op07 1.16 MB→1.0 s). Gate before replaying: if the estimate exceeds the budget, keep
  everything, deterministically. Keep RLIMIT_CPU as the backstop. Not worth doing now: the CPU
  deadline never bound (late = 0), so this only moves determinism at margins that do not occur.
* **Rebuild-invariant subtest plan (#9).** If the final rebuilds the image, `cpu_exec` per
  query is re-measured, so which TPC tests get verified can change. Commit a rounded
  per-query cost table (≈120 floats, < 4 kB) and let the build use it in place of its own timings.
* **Digest canonical set order (#12).** In `FastCmpPickler.persistent_id`, write set and
  frozenset members sorted by their own digest. The cost is a recording rebuild, so this is
  only worth doing together with moon-12's re-record.
* **Global wall deadline (moon-09 R25)** is the one wall clock that has to stay. It should trip
  only in a real overrun (> 4 min), never at normal load.

## EC2 reruns (rwx-big, `blast-mapbuild` image = python 3.13 + /opt/sqlglot, `--cpus 2`)

Index built in the container. Base = origin/main (bd16852), fix = moon-17. Odd runs are idle;
even runs put 6 busy loops in the same 2-CPU container, so the selector gets ≈2/7 of a CPU and
wall time stretches 3–5×.

| set | base runs | fix runs | outputs (md5) | total s |
|---|---|---|---|---|
| sample | 5 | 5 (+4 with the blob retry) | **1 distinct** across all of them | 115.1 every run |
| private41 | 5 | 5 | **1 distinct** | 388.9 every run |
| adversarial2 | 5 | 5 (+3 retry) | **1 distinct** | 937.4 every run |
| practice_like | 5: **tggb flips in both throttled runs (2 and 4)** | 5 (+3 retry): 0 flips | base 2 distinct, fix 1 | base 989.6 / **1007.9** / 989.6 / **1007.9** / 989.6; fix 989.6 every run |
| op07 alone, `--cpus 0.1` | 3 | 3 | same (child needs 0.85 s of CPU, ≈8.5 s wall, under the 9.4 s guard) | |
| op07 alone, `--cpus 0.05` | **2/2 flip**: `no answer`, 127 tests, 7.6 s | **0/2 flip**: dropped 123, 2.4 s | | |

* **tggb (practice_like) is a third wall-clock flip, and it happens at ordinary throttling.**
  subtest verifies 6 TPC queries, 3.7 s of child CPU against cap 6.0 s. Base's wall deadline
  is 2·6+2 = 14 s. With the container's CPU shared by 6 busy loops, the child had used 3.2–3.7 s
  of CPU when it was killed, which turned test_execute_tpcds back on: +18.3 s. The fix's guard
  (54 s) let it finish in both throttled runs. That is −18.3 s per occurrence, and the 3.7 s of
  CPU spent on a killed verification is no longer wasted.
  It is also the **tightest CPU margin** we found (0.62 of the cap). It is self-consistent only
  while the build machine, which measures `cpu_exec`, is as fast as the run machine.
* In the real image, 66ks gives `dropped 46 (blobs 2531/2543)` in all 10 sample runs; the 12
  failing blobs also fail there. So 66ks is stable in the image too.
* **CPU impact of the fix: none measurable.** Parent `cpu` on private41 is 3.45–3.75 s (base)
  against 3.46–3.75 s (fix); total container user time is 12.45–12.59 s against 12.38–12.68 s
  on idle runs. `late` = 0 and `retry` = 0 on every commit.
* **Seconds impact: 0 s on every labeled set when the host is quiet** (all idle runs are
  identical between base and fix). On a starved host each
  avoided flip saves ≈5 s (op07 +5.2 s, ktz3 +4.9 s), plus the 300 s risk of a run that
  overruns (now bounded by the global budget).

## Why moon-06 saw 66ks change

Moon-06 forked from 4826380, and nothing in `maps/diffexec.zip`, `diffexec/` or `selector/`
changed between there and main. Its sample run of main scored 140.2 s against our 115.1 s:
about 25 s, which is the cost of keeping test_execute_tpcds for 66ks. That run happened on the
laptop during the same memory-pressure period. The most likely mechanism (not reproduced) is
the per-blob timing check: `cpu_new ≤ 5·cpu_old + 0.05` with a per-call cap of
10·cpu_old + 0.2 s. `process_time` includes system CPU, and direct page reclaim under swap
pressure is charged to the faulting process. So one reclaim spike of more than 50 ms during a
"new" run marks the blob not ok, and with it every test phase that uses the blob. Context.table
runs in every TPC-DS query, so test_execute_tpcds is the most exposed test. moon-17 now
re-measures a timing-only failure once (min of two old/new pairs) before rejecting the blob.
The digest comparison is never retried. Cost: only on timing failures, which number 0 on every
labeled set.
