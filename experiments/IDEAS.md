# Ideas backlog

**Target: the oracle.** Perfect knowledge scores 94.4 s on the sample (≈3.15 s/commit), 0 misses. Leaders are at ≈3.7 s/commit. Every round attacks the largest gap between our cost and the oracle cost, per commit, with 0 misses on every set.

Status: `todo` · `running` · `kept` · `rejected` · `parked`. Each idea links to its LOG.md entries.
Source refs: `research/FINDINGS.md`, `research/PLAN.md`, `research/agent-reports/`.

| ID | Idea | Status | Notes |
|---|---|---|---|
| I01 | AST no-op filter for .py files → `[]` | kept (v0) | Clears comment-only commits. |
| I02 | Optimal packing: LPT over k=1..8 | kept (v0) | |
| I03 | Function-level coverage map (sys.monitoring / coverage contexts), syncpool inline + pickle round-trip | todo | Core of rank-1 design. |
| I04 | Fixture read map (audit hook on open) | todo | 8/30 sample commits are fixture edits. |
| I05 | Class-use map with MRO for class-body / module-level edits | todo | Fixes 535z-type misses. |
| I06 | Statement expansion for multi-line conditions | todo | Alternative/complement to I03. |
| I07 | Fallback table for unseen kinds (test files, helpers, new/deleted, config) | todo | PLAN.md §3. |
| I08 | Narrow class-body edits by attribute name (attribute-read tracing) | todo | 535z 165→15 tests (report 02). |
| I09 | Line map inside large functions, to keep slow tests out | todo | Phase E. |
| I10 | Run-time execution of cheap candidate sets | parked | ~4 s gain, risk (report 03). |
| I11 | Expected-cost rule for slow tests | todo | Only if mapped-in; never drop on guess. |
| I12 | Differential execution: record each function's inputs during the pinned-commit test run; at run time, run old vs new function on those inputs; a test whose recorded calls all give identical results cannot fail | todo | Ambitious. Attacks "edit changes nothing" commits (12/30 in sample) that still select tpcds. |
| I13 | Bytecode-equivalence filter (compile old/new function; equal co_code+consts → no-op) | todo | Wider than AST filter, still structural. |
| I14 | Oracle-gap report in eval: per commit, our cost − oracle cost, sorted | todo | Drives every round. |
| I15 | Native (Rust/Zig) selector for CPU; first: precompute at build + fast formats in Python | parked | Allowed ("any language"). Worth ~1–2 s; matters only for the final 1-s tie rule. Revisit late if CPU > 0.5 s. |
