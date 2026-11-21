# Ideas backlog

Status: `todo` · `running` · `kept` · `rejected` · `parked`. Each idea links to its LOG.md entries.
Source refs: `research/FINDINGS.md`, `research/PLAN.md`, `research/agent-reports/`.

| ID | Idea | Status | Notes |
|---|---|---|---|
| I01 | AST no-op filter for .py files → `[]` | kept (v0) | Clears comment-only commits. |
| I02 | Optimal packing: LPT over k=1..8 | kept (v0) | |
| I03 | Function-level coverage map (sys.monitoring / coverage contexts), syncpool inline + pickle round-trip | todo | Core of rank-1 design. |
| I04 | Fixture read map (audit hook on open) | todo | 8/30 sample commits are fixture edits. |
| I05 | Class-use map with MRO for class-body / module-level edits | todo | Fixes 535z-type misses. |
| I06 | Statement expansion for multi-line conditions | partly (E003) | Used for slow tests only (classify.touched_statements). |
| I07 | Fallback table for unseen kinds (test files, helpers, new/deleted, config) | todo | PLAN.md §3. |
| I08 | Narrow class-body edits by attribute name (attribute-read tracing) | todo | 535z 165→15 tests (report 02). |
| I09 | Line map inside large functions, to keep slow tests out | kept? (E003, branch r1-slowtests) | Slow tests need a touched statement, not just the function. −29.7 s private, 0 sample: 2haz/66ks tpcds runs the mutated line. |
| I10 | Run-time execution of cheap candidate sets | parked | ~4 s gain, risk (report 03). |
| I11 | Expected-cost rule for slow tests | partly (E003) | Done as a structural rule (statement-level for slow tests), no guessing. Remaining tpcds cost is on lines tpcds really runs: dropping it would need equivalence reasoning (banned). |
