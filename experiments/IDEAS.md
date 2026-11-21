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
| I06 | Statement expansion for multi-line conditions | partly (E005) | Used for slow tests only (classify.touched_statements). |
| I07 | Fallback table for unseen kinds (test files, helpers, new/deleted, config) | todo | PLAN.md §3. |
| I08 | Narrow class-body edits by attribute name (attribute-read tracing) | kept? (r1-attrnarrow, E003) | Static name-reader scan (selector/names.py) + lambda call lines. Sample 198.8→185.0, private41 625.8→583.2, 0 misses; CPU +2.5/+3.7 s. Still falls back: `arg_types` (Expr.__init_subclass__), Tokenizer settings (metaclass), TRANSFORMS (memoized `_build_dispatch`), call-valued entries (`_show_parser(...)`). Next: prebuild the name index at image build to cut CPU. |
| I09 | Line map inside large functions, to keep slow tests out | proposed (E005, r2-slowtests) | Slow tests need a touched statement, not just the function. −184.7 s private264, −29.7 s private41, 0 sample/adversarial. |
| I10 | Run-time verification of cheap selections (selector/verify.py) | proposed (r3-verify, E009) | Net +6.8 s sample (sim −8.9, CPU +2.1), +4.1 s private264 over 7 batches of 40, +1.1 private41, −0.2 adversarial; 0 misses. Fidelity audit: 249 commits verified regardless of cost, 0 labeled failures dropped after the tests/sqlglot shadowing fix. Small because most cheap selections do fail (single-test sets on the private sets fail ~75 %) and a run costs ≥ 0.15 s. Next: per-file import cost by module size; pre-import pytest plugins (−25 ms/run, measured). |
| I11 | Expected-cost rule for slow tests | partly (E005) | Done as a structural rule (statement-level for slow tests), no guessing. |
| I12 | Differential execution: record each function's inputs during the pinned-commit test run; at run time, run old vs new function on those inputs; a test whose recorded calls all give identical results cannot fail | todo | Ambitious. Attacks "edit changes nothing" commits (12/30 in sample) that still select tpcds. |
| I13 | Bytecode-equivalence filter (compile old/new function; equal co_code+consts → no-op) | todo | Wider than AST filter, still structural. |
| I14 | Oracle-gap report in eval: per commit, our cost − oracle cost, sorted | todo | Drives every round. |
| I15 | Native (Rust/Zig) selector for CPU; first: precompute at build + fast formats in Python | parked | Allowed ("any language"). Worth ~1–2 s; matters only for the final 1-s tie rule. Revisit late if CPU > 0.5 s. |
| I16 | Import-time map (`mapbuild/import_time.py`): per line, modules whose import runs it; function run at `import sqlglot` → ALL, at import of M → M's dependents | kept (r1-robust) | Coverage-by-test cannot see import-time calls (trie builders, decorator factories, `X = build()` tables, module `__getattr__`). Adversarial round 1, E005. The definitive `maps` branch must also build this file. |
| I17 | Module-level name dataflow: removed/renamed defs+classes join importer search; `B = f(A)` closure; same-module readers of changed names count as changed | kept (r1-robust) | E005. |
| I18 | Keep `diff --git` entries without ---/+++ (pure rename, empty file add/delete, mode change) | kept (r1-robust) | v1 silently dropped them inside multi-file patches. E005. |
| I19 | Adversarial private set (`eval/adversarial.py`), re-run every round with new targets; label in pinned image, full suite, SKIP_INTEGRATION=0 | kept | Patches/labels at eval/data/adversarial (gitignored). Labeling costs ≈4–5 min CPU-bound per patch on the 4-core box. |
| I20 | Import-time map per *function* reached from class bodies of lazily loaded dialects is coarse (dependents = whole module users); refine by recording which attribute/table the import-time call feeds | todo | Cost, not recall. |
| I21 | Selector CPU: import-time map + source scan adds ≈0.1 s/commit on adversarial patches; lazy-load only on function regions (already) and cache parsed trees | todo | |
