# 20 — Tokenizer settings through `TokenizerCore(...)`: what is left, and what shipped

## Verdict and numbers
- **The constructor hop the brief asks for already exists and already fires.** `names._init_flow`
  (r9/r10) follows `TokenizerCore(kw=self.X)` to the `self.<field>` read sites in
  `tokenizer_core.py`. `_state_readers` then intersects those reads with the users of the edited
  tokenizer class (the classes map). Moon-16's "the constructor is the reader" premise is stale:
  `tokens.py:606` has `flow=("attr", "sqlglot/tokenizer_core.py", ("TokenizerCore",), field)`, and
  its own line contributes 0 tests (`diag.py`).
- **The residual on tokenizer commits has two parts:**
  1. **Real readers, ~70 %.** These tests ran that dialect's escape / comment code:
     `tokenizer_core.py:1240` (`numeric_escapes.get(peek)`), `:1324→1388` (`drop_unknown_escapes`)
     and `:908`. Examples: 4d63 11 tests, 4.1 s + 2 s floor (all from `:1240`, diag on 0b54a3d); p3ow 13 tests, 4.1 s; gozk 27 tests, 5.1 s. They
     did read the changed value. Only value-level evidence could drop them: which dict key was
     looked up, or whether the tokens changed. Static flow cannot, so **4d63 cannot reach 0 by flow
     refinement.**
  2. **Spurious dynamic-read floors, ~30 %.** Computed-name reads match *every* attribute name:
     - `tokenizer_core.py:538`: `getattr(self, k) for k in self._attrs` in `Token.__repr__`
     - `generators/duckdb.py:1434`: `getattr(annotated, return_value_attr)`
     - `tests/test_schema.py:429`: `getattr(schema, method)`, where `method` loops over a literal

     Together they add 0.7–2.8 s to **every** class-attribute state-diff or name-narrowing commit,
     tokenizer or not.
- **Shipped (sound, static, build-time only, no repo-size or run-time CPU change):**
  - **R20a, constant-valued computed names.** `names.Index._var_constants`:
    - **Trigger.** The name argument of `getattr` / `hasattr` / … is a local variable.
    - **Condition.** Every binding of that variable in the function, nested scopes included, is
      one of:
      - `v = "const"`;
      - a for / comprehension target, or a fixed position of a tuple target, over a literal
        tuple / list / set of strings;
      - a loop target over `self.X`, where X is a class-level literal *tuple* of strings, X is
        bound exactly once in the whole source, and `self` is the method's never-rebound
        receiver.
    - **Result.** The site may read only those names. 3 of 21 dynamic sites are narrowed (the
      three above). With the existing `.*_sql` patterns, 9 of 21 sites are now narrowed (was 6).
  - **R20b, `bool()` truth flow.** `TokenizerCore(has_bit_strings=bool(self.BIT_STRINGS))` used to
    stop at `bool(...)`, so every tokenizing test ∩ users was selected. It now follows to the
    `has_bit_strings` read sites with flow tag `"truth"`:
    - if the edit keeps the truth (`same ∋ "truth"`), no reader is selected;
    - otherwise the field readers are selected, with `same = ∅`.
    - `bool` must not be rebound in the module.

## Per-set deltas (EC2, pinned image, gap.py; old = origin/main c105ec7, new = moon-20)
| set | commits | old s | new s | Δ s | Δ per 40 | misses old→new |
|---|---|---|---|---|---|---|
| practice_like | 120 | 990.2 | 987.4 | −2.8 | −0.9 | 0→0 |
| private | 264 | 2439.4 | 2429.9 | −9.5 | −1.4 | 0→0 |
| private41 | 41 | 388.9 | 382.9 | −6.0 | −5.9 | 0→0 |
| adversarial | 46 | 1063.2 | 1062.6 | −0.7 | −0.6 | 0→0 |
| adversarial2 | 65 | 937.4 | 932.9 | −4.4 | −2.7 | 0→0 |
| sample | 30 | 137.8 | 136.3 | −1.4 | −1.9 | 0→0 |
| probes (below) | 22 | 271.9 | 267.1 | −4.8 | – | 0→0 |
| PQ/P (unlabeled) | 600 | 4898.6 | 4874.5 | −24.1 (−20.2 excl. diffexec noise) | −1.3 | – |
| PQ/Q (unlabeled) | 1000 | 7905.4 | 7839.2 | −66.2 (−35.0 excl. diffexec noise) | −1.4 | – |

- **No commit got more expensive in any set.** All deltas are ≤ 0.
- **PQ noise.** The baseline PQ runs overlapped a heavier box load, so some diffexec replays timed out
  there and not in the new run (e.g. Q rhgj: "diffexec: no answer" → "dropped 58"). The
  "excl. diffexec noise" figure drops every commit whose reason mentions diffexec / subtest, so it is a lower bound on the R20 gain (it also drops R20 wins on those commits).
- **Expected board gain is small: ≈ −1 to −1.5 s per 40 practice-style commits.**
  - Each narrowed site removes a 0.7–1.3 s floor from every name / state-diff commit whose
    attribute name it used to match.
  - Tokenizer-specific wins: 48vt −2.1, w7sj −1.8, p3ow −0.7, probe t04 −2.1.
  - 4d63 stays at 6.1 s. R20a cut its selection from 15 to 11 tests at no cost change. All 11 come from `:1240`: they ran `numeric_escapes.get(peek)` under Snowflake (next section).


## Probes (full suite, SKIP_INTEGRATION=0, --continue-on-collection-errors)
22 one-line tokenizer edits (`20-tools/probes.py`), each labeled with the **full suite** in the pinned
image (blast-mapbuild, py3.13, SKIP_INTEGRATION=0, `--continue-on-collection-errors`; SUBFAILED
subtests mapped to their test). **16 of 22 probes fail tests; the new selector misses none of them, and the
baseline misses none either.**

| probe | kind | failing tests | new cost s (old) |
|---|---|---|---|
| t01 base NESTED_COMMENTS | base flag, overridden by mysql/sf/oracle/sqlite | 3 | 7.1 (7.8) |
| t02 base DROP_UNKNOWN_ESCAPES | base flag, overridden by pg/mysql/hive/sf | 1 | 7.2 (7.9) |
| t03 base STRING_ESCAPES_ALLOWED_IN_RAW_STRINGS | base flag | 1 | 7.2 (7.9) |
| t04 base COMMENTS_TERMINATE_AT_NEWLINE_ONLY | base flag | 1 | 2.8 (5.0) |
| t05 base HINT_START | base str | 1247 | ALL |
| t06 base IDENTIFIERS (+`) | hook-derived `_IDENTIFIERS` | 0 | ALL (near-ALL) |
| t07 Postgres BIT_STRINGS (drop B') | inherited by Redshift/Materialize/RisingWave; `bool()` + `_FORMAT_STRINGS` | 0 | 9.7 |
| t08 Postgres HEX_STRINGS = [] | truth flips (`bool()` path) | 3 | 10.7 |
| t09 MySQL DROP_UNKNOWN_ESCAPES | inherited by Doris/StarRocks/SingleStore | 1 | 6.4 |
| t10 MySQL NESTED_COMMENTS | inherited | 0 | 5.9 |
| t11 Hive NUMERIC_ESCAPES octal min | dict value, inherited by Spark2/Spark/Databricks | 0 | 6.1 |
| t12 ClickHouse NUMERIC_ESCAPES x min | dict value | 0 | 5.9 |
| t13 Snowflake NESTED_COMMENTS | dialect flag | 1 | 5.9 |
| t14 SQLite HEX_STRINGS (drop 0X) | list → `_FORMAT_STRINGS` | 0 | 6.3 |
| t15 Materialize NUMERIC_ESCAPES U min | dict value on a Postgres child | 0 | 2.0 |
| t16 ClickHouse IDENTIFIER_ESCAPES = [] | hook-derived set | 2 | 8.1 |
| t17 MySQL QUOTES (drop ") | hook-derived `_QUOTES`, inherited | 6 | 9.8 |
| t18 KEYWORDS RELEASE → VAR | trie | 1 | ALL (base class body) |
| t19 Dialect NUMBERS_CAN_BE_UNDERSCORE_SEPARATED | `self.dialect.X` into the core | 1 | ALL (near-ALL) |
| t20 ClickHouse BIT_STRINGS = [] | `bool()` truth flips | 1 | 7.9 (8.2) |
| t21 Snowflake HEX_STRINGS (drop x') | list | 6 | 8.7 |
| t22 DAX COMMENTS (drop //) | list | 0 | 2.1 |


## Why the rest needs value-level evidence (not built; flagged)
- **What would remove the residual:** replay of `Tokenizer.tokenize`.
  - **Build time:** record, per test (fresh interpreter, phase-aware like diffexec), the
    `(dialect class, tokenizer class, sql)` of every `tokenize` call.
  - **Run time:** for state-diff tests reached only through the `_init_core → TokenizerCore`
    flow, re-tokenize old vs patched, and drop a test whose token streams and errors are all
    identical.
- **Caveats:**
  - Calls at import or collection go to a global bucket; setUpClass calls go to a class bucket.
  - Store size is unknown. Repo headroom is ~5 MB, since diffexec.zip already takes 13 MB.
- **Upside, labeled:**
  - 4d63 6.1 → ~0–2 s.
  - private tokenizer commits (p3ow, gozk, 2iie, ga8d, w7sj, d3ad) ≈ 25 s → ~8 s.
  - Per 40 practice-style commits that is ~3–5 s (P/Q: tokenizer-setting commits ≈ 1–2 per 40).
- **Cost:** about 1 day including the recording run and a soundness check, so it is
  low-priority next to R-DEF (moon-16, ~13 s/40).

## Files
- `selector/names.py`: `_var_constants`, `_binding_constants`, `_literal_items`,
  `_stores_of`, `_rebinds`, and the `bool()` branch of `_flow`.
- `selector/select.py`: `_site_reads`, which handles the `("attr", …, "truth")` flow.
- Tools in `research/moonshots/20-tools/`:
  - `diag.py`: per-site contributions
  - `probes.py`: make / label / answers
  - `cmp.py`: compare runs
