# Moonshot 04: escape the 20 MB repo limit (build-time artefacts)

Status: DRAFT (results being filled in). Branch `moon-04`.

## Verdict (provisional)

- (b) GitHub release asset on our own repo, (c) our own PyPI data package: **not allowed**
  (conservative reading, see Rules). Do not ship.
- (a)/(d) record the "budget"-dead diffexec functions during the Docker build: the label
  "not recorded (budget)" is misleading. Most of that bucket is cap-limited, not byte-limited.
  The measured realistic gain is in Results below.

## Numbers so far

- A/B bucket (r20gm3, P+Q, 1,600 sample-style patches): "diffexec: function not recorded
  (budget)" = 3,204 s of 12,766 s (25 %), 263 patches, 158 distinct functions.
- Earlier measurement r14 (m14 → d14): 13.1 MB store → 77.8 MB store (store_budget 27e6,
  3,071 functions alive instead of 2,039) saves only **35.7 s / 1,600 patches** (0.3 %).
  On the bucket's commits it saves 17.6 s of 3,363 s. In that 6x store, 2,985 s of the 3,240 s
  bucket still belong to functions that are dead as "budget". `build.restrict()` labels every
  function with no plan as "budget": a global (IMPORT/OTHER) phase incomplete, or no test with
  all phases complete (input > 16 kB after elision, 2 MB per-file byte cap, unpicklable).
- Ceiling (scratch copy, `prune()` drops ALL candidates when the precheck says
  `not recorded (budget)`, measurement only, ignores failing tests): bucket 3,341.7 → 84.4 s.
  So the seconds are diffexec-reachable in principle (incl. the 67 patches at 25.5 s =
  test_execute_tpcds), but a perfect store is not realistic.
- In-build recording cost, laptop (20 cores, Python 3.13.12), `DIFFEXEC_ONLY` = the 4,134
  budget-dead functions, default caps, `-j 16`: census 69 s wall (~227 CPU-s); pass 2 pickling
  alone 1,866 CPU-s (sum of worker `pickle_s`), 2.45 M calls recorded, 1.6 GB of recordings;
  wall 525 s, critical path = tests/test_executor.py and tests/test_optimizer.py, ~520 s each
  in ONE process.
