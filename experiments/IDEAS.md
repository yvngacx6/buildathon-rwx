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
| I08 | Narrow class-body edits by attribute name (attribute-read tracing) | kept? (r1-attrnarrow, E003) | Static name-reader scan (selector/names.py) + lambda call lines. Sample 198.8→185.0, private41 625.8→583.2, 0 misses; CPU +2.5/+3.7 s. Still falls back: `arg_types` (Expr.__init_subclass__), Tokenizer settings (metaclass), TRANSFORMS (memoized `_build_dispatch`), call-valued entries (`_show_parser(...)`). Next: prebuild the name index at image build to cut CPU. |
| I09 | Line map inside large functions, to keep slow tests out | todo | Phase E. |
| I10 | Run-time execution of cheap candidate sets | parked | ~4 s gain, risk (report 03). |
| I11 | Expected-cost rule for slow tests | todo | Only if mapped-in; never drop on guess. |
| I12 | Differential execution: record each function's inputs during the pinned-commit test run; at run time, run old vs new function on those inputs; a test whose recorded calls all give identical results cannot fail | todo | Ambitious. Attacks "edit changes nothing" commits (12/30 in sample) that still select tpcds. |
| I13 | Bytecode-equivalence filter (compile old/new function; equal co_code+consts → no-op) | todo | Wider than AST filter, still structural. |
| I14 | Oracle-gap report in eval: per commit, our cost − oracle cost, sorted | todo | Drives every round. |
| I15 | Native (Rust/Zig) selector for CPU; first: precompute at build + fast formats in Python | parked | Allowed ("any language"). Worth ~1–2 s; matters only for the final 1-s tie rule. Revisit late if CPU > 0.5 s. |
| I16 | Import bound: per-test traced import closure (each test alone in a fresh process, audit "import" hook incl. subprocesses / pool workers); an edit confined to module M reaches only tests that load M (∪ readers). Replaces ALL for syntax errors, deleted / renamed .py, removed bindings, changed imports, and every rule that gives up | kept? (r2-importclosure, E005) | private264 3453.9→2887.8, 0 misses; private41 583.2→545.8; sample 185.0 unchanged. Map matches labels exactly (pushdown_projections 149/149, databricks 171/171, presto 326/326, singlestore 37/37). Remaining: removed class members (generator/tokens methods → 1617 loaders), dsa7-type renames of an unreferenced module function pay the whole import bound (tried: route them to the v1 rules when no importer mentions the name, 27.5→13.8 s on dsa7, reverted as unprobed). Next: per-name importer closure for removed bindings (`from M import X` at module level → loaders of the importer; run-time reads via name index). |
