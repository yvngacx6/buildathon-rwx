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
| I06 | Statement expansion for multi-line conditions | todo | Alternative/complement to I03. |
| I07 | Fallback table for unseen kinds (test files, helpers, new/deleted, config) | todo | PLAN.md §3. |
| I08 | Narrow class-body edits by attribute name (attribute-read tracing) | kept? (r1-attrnarrow, E003) | Static name-reader scan (selector/names.py) + lambda call lines. Sample 198.8→185.0, private41 625.8→583.2, 0 misses; CPU +2.5/+3.7 s. Still falls back: `arg_types` (Expr.__init_subclass__), Tokenizer settings (metaclass), TRANSFORMS (memoized `_build_dispatch`), call-valued entries (`_show_parser(...)`). Next: prebuild the name index at image build to cut CPU. |
| I09 | Line map inside large functions, to keep slow tests out | todo | Phase E. |
| I10 | Run-time execution of cheap candidate sets | parked | ~4 s gain, risk (report 03). |
| I11 | Expected-cost rule for slow tests | todo | Only if mapped-in; never drop on guess. |
