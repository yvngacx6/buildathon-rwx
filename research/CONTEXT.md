# Shared context for research agents

Repo: `~/Desktop/hackathons/buildathon-rwx` (starter kit). Read `README.md` and `SCORING.md` first.

## Task (RWX "Blast Radius", Builders Buildathon)
- Input per run: `/data/commits/<id>.patch` (unified diff vs sqlglot @ eb11aec4be8cb2122afe713a4816662c3f0fb2fa, each standalone) and `/data/tests.json` (1620 numbered pytest tests, file, recorded seconds).
- Output: one JSON line per commit `{"commit": id, "workers": [[test#...], ...]}`, ≤8 workers. `[]` = run nothing.
- Cost per commit = max worker load (recorded secs) + 2 s × workers; +300 s if any failing test is omitted. Plus total container CPU seconds. Median of 3 runs.
- Build: network, 4 cores, 8 GB, 10 min; may clone sqlglot at pinned commit and run its tests (coverage maps etc.). Repo ≤20 MB.
- Run: no network, 2 cores, 4 GB, 3 min for whole batch (final ~1.5x commits, 4.5 min), read-only disk, 64 MB /tmp, user nobody, ≤256 procs.
- Running sqlglot tests during the run is ALLOWED (CPU charged).
- BANNED: precomputed table patch→failing tests or edit-kind→failing tests. ALLOWED: maps built from the pinned commit (coverage, imports, fixtures). Top entries are read by judges.
- Final uses unseen commits incl. kinds of change not in practice.
- "Fails" = pytest failure/error/timeout or import error of the file. SKIP_INTEGRATION=0 was set when labeling.

## Sample data facts (30 commits)
- Kinds: code 16, fixture 8, multi 3, comment 3. Failing-test counts: 12 commits fail nothing; most fail 1; max 37.
- Total recorded test time 103 s. Slowest: test_parser::test_max_nodes 28.8 s, test_executor::test_execute_tpcds 21.5 s, test_generator_memory 6.7 s.
- ~1020 of 1620 tests are in tests/dialects/. Fixture patches edit tests/fixtures/optimizer/*.sql (read by test_optimizer etc).
- Starter (all tests, 3 workers): 1210.5 s on sample, 0 misses. Practice leaderboard: baseline 1614.1 s, best ~169 s.
- "Comment-only" changes are NOT promised harmless.

## Infra
- Local laptop has the repo. EC2 box `ssh rwx` (4 cores, 8 GB, Docker) has a clone at ~/buildathon-rwx. If you use it, work only in your own directory (~/agent-<name>) and do not touch ~/buildathon-rwx.
