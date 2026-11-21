# Moonshot index

Recurring loop: 10 moonshot agents run in parallel with the core improvement loop. When one finishes,
its report is linked here with a verdict, promising ideas become core rounds (experiments/LOG.md), and a
new moonshot angle is started in its slot.

| # | Angle | Branch | Status | Verdict / next step |
|---|---|---|---|---|
| 01 | Reverse-engineer the leaders | moon-01 | done | Practice oracle ≈ 95 s; leaders 10–60 s above it, us ~100 above. Copy: statement-level selection for ALL tests (−10…−25 s), TPC fixture-entry rule (−8 s), CPU 4.9→2 s (−3 s) → R23. |
| 02 | Warm fork-server: near-free test execution | moon-02 | done | ✗ Works (0 disagreements on 98 labeled) but per-test CPU ≈1.3× recorded secs (≈2.6× on scorer); net ≈ 0 s/40. Not built. |
| 03 | General semantic equivalence (value profiles) | moon-03 | done | ✗ ~1–2 s/40; most edits aren't comparisons/boolops. Warning: pickle-only paths (serde) invisible to non-pickling recorders → relayed to R23. |
| 04 | Build-time recording beyond the 20 MB repo limit | moon-04 | running | |
| 05 | Divergence-point re-execution of slow tests | moon-05 | running | |
| 06 | Generic sub-test decomposition | moon-06 | done | Full mechanism not worth it; zero-CPU special case STMT_SECONDS=0 (statement rule for all tests) 0 misses/440, −9…−14 s/40 → folded into R23. Flagged diffexec nondeterminism on 66ks → moon-17. |
| 07 | Calibrated risk model (P(first place)) | moon-07 | done | ✗ AUC 0.56 (no better than base rate); keep 0-miss policy. Spin-offs → R22 (import-run slow tests w/ 0 touched lines never fail), moon-12 (record tpcds callees). |
| 08 | Practice-board experiment design | moon-08 | done | Slot plan adopted in part (ablations; always pick final explicitly after diagnostics). Score-encoded 'telemetry word' REJECTED: it circumvents 'you never see what your program printed' — not used. |
| 09 | Final-round strategy | moon-09 | done | Deterministic under ±2x CPU. Must-fix: global wall deadline, Dockerfile download retry, Selector-construction guard, config/no-reader files → R25. Gap is practice-like code commits → moon-16. |
| 10 | Wildcard ideas | moon-10 | done | 17 angles; V1 = run leftover cheap tests via plain unittest (agrees with labels 418/418), gated ≤0.6 s → −3…−9 s/40 → R24. Fixture-case slicing → moon-15. |
| 11 | Assertion-level slicing: verify only the affected assertions of cheap tests | moon-11 | running | |
| 12 | Compact tree-table input encoding so diffexec can record tpcds/max_nodes callees | moon-12 | running | |
| 13 | Cut container CPU 4.9 s → <1 s (native/cached hot paths, cheaper state diff) | moon-13 | running | |
| 14 | Fixture-edit precision | moon-14 | done | ✓ Loader-output diff per reader: private −84.5 s, adversarial −19.6 s, P −10.5/40, 0 misses on 73 fixture commits → porting as R27 (r27-fixtures). |
| 15 | Per-fixture-case slicing for code edits (generalise r17 to test_annotate_funcs, test_qualify_columns, check_file loops) | moon-15 | running | |
| 16 | Practice-like code commits: per-mechanism over-selection breakdown + targeted fixes | moon-16 | running | |
| 17 | Determinism audit: identical input → identical output across runs/load (diffexec/subtest budgets, hash seeds, fork timing) | moon-17 | running | |
| 18 | Red-team soundness review of all run-time pruners (diffexec, subtest, slowverify, genmem, r19 import-fn, statement rule) | moon-18 | running | |
| 19 | 'Conservative def-time' + 'state diff' buckets on practice_like (~87 s): sound narrowing | moon-19 | running | |
