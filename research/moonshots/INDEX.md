# Moonshot index

Recurring loop: 10 moonshot agents run in parallel with the core improvement loop. When one finishes,
its report is linked here with a verdict, promising ideas become core rounds (experiments/LOG.md), and a
new moonshot angle is started in its slot.

| # | Angle | Branch | Status | Verdict / next step |
|---|---|---|---|---|
| 01 | Reverse-engineer the leaders | moon-01 | running | |
| 02 | Warm fork-server: near-free test execution | moon-02 | done | ✗ Works (0 disagreements on 98 labeled) but per-test CPU ≈1.3× recorded secs (≈2.6× on scorer); net ≈ 0 s/40. Not built. |
| 03 | General semantic equivalence (value profiles) | moon-03 | running | |
| 04 | Build-time recording beyond the 20 MB repo limit | moon-04 | running | |
| 05 | Divergence-point re-execution of slow tests | moon-05 | running | |
| 06 | Generic sub-test decomposition | moon-06 | running | |
| 07 | Calibrated risk model (P(first place)) | moon-07 | running | |
| 08 | Practice-board experiment design | moon-08 | running | |
| 09 | Final-round strategy | moon-09 | running | |
| 10 | Wildcard ideas | moon-10 | running | |
| 11 | Assertion-level slicing: verify only the affected assertions of cheap tests | moon-11 | running | |
