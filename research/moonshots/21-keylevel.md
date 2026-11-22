# 21 — Key-level import-state diff + key-lookup map (built, validated, branch `moon-21`)

## Verdict and numbers
- **Works, with 0 misses.** Key-level narrowing of changed dict entries in the import-state diff,
  backed by a key-lookup map built from the pinned commit (`maps/keys.json.gz`, **1.19 MB**, committed; repo 16.7 MB).
  It is shippable as is. It is a Δ on top of main 904f48c, which sits beneath every other rule.
- **Labeled sets (simulated s, main → moon-21, 0 misses on both sides):**

| set | main | moon-21 | Δ |
|---|---|---|---|
| practice_like (40) | 989.6 | 967.3 | **−22.2** |
| private | 2343.5 | 2310.8 | −32.7 |
| private41 | 388.9 | 388.9 | 0 |
| adversarial | 1063.2 | 1031.4 | −31.8 |
| adversarial2 | 937.4 | 887.9 | −49.5 |

- **Biggest commit wins:**
  - practice_like: 7zco −11.6, 09if −9.3, 6o3v −2.0
  - private: dv9g −9.3 (ALL → 432 tests), pw9b −6.8, rywc −5.5, p3ow −4.5, 80sx −3.4
  - adversarial: default_arg_new_trie −9.3, imp_binary_range_default −9.3, cls_hive_quotes −7.9
  - adversarial2: a2_closure_duckdb_sort −24.8, a2_pop_duckdb_datepart −7.8
  - Small rises: mn99 +0.3, ptjt +0.4, s66j +0.3, jyy4 +0.4 (one cheap test each). The selection is ⊆ main per
    entry, not per commit.
- **Probes:** 19 keyword / token-type / comment / dispatch-table entry edits (`21-tools/mkprobes.py`). Each was labeled with the
  FULL suite in the pinned image (`21-tools/labelfull.sh`).
  - **0 misses**, and main also has 0. See the table in §3. k19, a pure reorder, is a coverage check only, with no label failures.
  - Typical cut: hook / `_COMMENTS` edits go from ALL (103 s of test time) to 24–47 tests (1.4–4.7 s).
  - Dialect `KEYWORDS` entries go from 73–429 tests to 1–17 tests.
- **P/Q A/B (unlabeled, `eval/ab.py`):** P (600) **−5.0 s/40**, Q (1000) **−5.1 s/40**; see §4.
- **Cost:**
  - Map build: two traced suite runs (forward with seed 0, reversed with seed 1), run in parallel, ~2.5 min wall
    on the loaded EC2 box, followed by `mapbuild/keymerge.py`. The map is committed, not built in Docker
    (`21-tools/trace2.sh`).
  - Image build: `nameindex build` takes 36.7 s in total. The per-key tables come from sample 0 only.
  - The run-time extra CPU is small: keymap load 0.09 s, `state.pkl.keys` load 0.01 s, plus per-key tables
    only for changed dict entries. Both are paid only on commits whose diff shows changed dict entries.

## 1. Mechanism
- **State side (`selector/state.py`)**
  - At build, `snap` also writes `index/state.pkl.keys` (4.4 MB, image only). For every exact-dict state entry it stores
    path → {key id → value digest}, recursing into nested dicts (tries).
  - Key ids are hash-free (`state.kid`). Strings are keyed by value, which covers str-enums. int, bool and integral floats are keyed by int value, which covers
    TokenType (an IntEnum). Enums are keyed by class and name, classes by qualname, tuples and frozensets element-wise. A key with no id
    disables key-level for that entry.
  - In the diff, `_key_changes` gives the changed (path, key) pairs per changed dict entry: added, removed,
    value-changed, or a nested dict appearing or disappearing.
  - An order-only change (same keys and values in a new order) gives an `ORDER` marker, which selects the whole readers of that
    dict and of its ancestors.
  - A changed entry with no key differences gets no key-level answer and keeps today's rule.
- **Map (`mapbuild/keytrace.py`, a pytest plugin, pinned commit)**
  - Before collection it imports all of sqlglot, enumerates the entries with `state.take`, and replaces every exact
    dict entry (2105 entries, nested dicts included) by a recording `dict` subclass.
  - It swaps every reference to the originals in one `gc` pass: class attributes, module globals, dict values, lists,
    cells and `__slots__`. Whatever still holds an original makes its entry **leaky**. Only `MODULE_BY_DIALECT` is leaky.
  - What gets recorded:
    - **Key reads** (`[]`, `get`, `in`), misses included.
    - **Whole reads**: iteration, len and bool, keys / values / items, copy, `==`, `|`, repr, pickling, deepcopy,
      and `**`, which goes through `keys()` because `tp_iter` is overridden. A self-check asserts 23 such idioms are recorded.
    - **Writes**, which make the entry *mutated*.
  - Where reads are attributed:
    - test call phase → that test
    - setup / teardown (setUpClass, fixtures) → every test of the file
    - collection or outside any test → ALL
    - subprocess / fork audit events → that context reads every table
  - **Process pools run inline**, with a pickle round trip and the initializer run once per executor. test_optimizer's class-wide spawn pool
    otherwise hides every later test's lookups. This was the cause of the 3 misses in the first version.
  - Parity: the forward traced run gives 1620 passed, 1 skipped, the same as untraced.
  - **Second trace, reversed order and `PYTHONHASHSEED=1`** (`keymerge.py`):
    - Records are unioned.
    - A table whose per-test record differs between the two orders is **unstable** and never narrowed.
      Tests that fail in either order are left out of that comparison; they are 2 order-dependent tests of the suite itself.
    - This flagged exactly 33 entries: all 31 `TRANSFORMS`, `EXPR_CLASSES` and `_DISPATCH_CACHE`, which is the generator
      dispatch cache. That independently confirms the cache-fed rule's main case.
- **Entries never narrowed** ("bad", 457 in total):
  - leaky entries
  - entries mutated at run time
  - entries whose value the run itself changed (state snapshot after the session vs before)
  - **cache-fed** entries: read whole by a test that also wrote some import-time dict, **in both orders**. This catches
    `_DISPATCH_CACHE ← dict(TRANSFORMS)` and also takes out base `Tokenizer.KEYWORDS` / `SINGLE_TOKENS`.
    - Flags raised in one order only are coincidences of that order. Example: the reversed run flagged 7 dialects'
      `_COMMENTS`, and those entries are stable across orders.
  - unstable entries (above)
- **Selector (`_state_readers`)**
  - The per-entry site loop still runs, so its Fallbacks still guard: memo / cached code, special names, TypedDict.
  - For an entry with key-level info, its readers are
    (tests that looked up a changed key at that path ∪ whole readers of that dict and of every dict above it)
    **∩** the entry's site-reader bound. Both are sound upper bounds, so the result per entry is ⊆ main's.
    When the static scan sees no reader, the map alone is used, since it records every read of the object.
  - `BR_KEYLEVEL=0` disables it.

## 2. Soundness argument and limits
- **Why it holds.** A test that never looked up a changed key, never read the dict or one of its ancestors whole, and
  ran no changed function executes identically up to its first divergence. Every read of the object goes
  through the recording wrapper, because no unswapped reference is left; otherwise the entry is leaky.
- **The remaining trust assumption is cross-test caches** that copy a table into new run-time state that is not an
  import-time dict. Two defences cover them:
  - the static memo guard, as on main;
  - the run-state diff plus the cache-fed rule, for caches that are import-time dicts;
  - the two-order instability check, for any first-user-fills-a-cache pattern that shows in either order.
- **Hash seed.** The scorer does not pin PYTHONHASHSEED. The second trace runs with seed 1, and any
  seed-dependent record is unstable.
- **Set-valued entries keep today's rule.** A set subclass is unsound: `set(s)`, `frozenset & s`,
  `x |= s` and `issubset` read the set's internals without any Python call. So jtc6 / bc18 / 7evr (sets and scalars) do not change.
- **Scalar entries keep today's rule.** Example: `HEX_START`, copied by the hook.

## 3. Probes (full-suite labels; `checkprobes.py`)
The columns are: failing test ids, then tests selected (main → moon-21), then summed test seconds (main → moon-21). Misses: 0 for
moon-21 and 0 for main on all 19.

| probe | edit | fail | sel main → m21 | test-s main → m21 |
|---|---|---|---|---|
| k01 | hook: add `"{%": "%}"` comment | 1 | 1620 → 24 | 103.0 → 1.4 |
| k02 | hook: `"{#"` end `#}`→`##}` | 1 | 1620 → 24 | 103.0 → 1.4 |
| k03 | hook: hint comment end | 4 | 1620 → 47 | 103.0 → 4.7 |
| k04 | hook: national-string prefixes | 2 | 508 → 25 | 54.2 → 1.4 |
| k05 | base KEYWORDS `DIV` value | 6 | 1620 → 1620 | cache-fed: no narrowing |
| k06 | base KEYWORDS add `ZQXJ` | 0 | 1620 → 1620 | cache-fed |
| k07 | base SINGLE_TOKENS `#` | 9 | 1620 → 1620 | cache-fed |
| k08 | mysql KEYWORDS `CHARSET` | 2 | 287 → 8 | 11.6 → 0.1 |
| k09 | duckdb KEYWORDS `//` | 4 | 429 → 4 | 46.9 → 0.5 |
| k10 | snowflake KEYWORDS remove `BYTEINT` | 1 | 354 → 1 | 11.0 → 0.7 |
| k11 | postgres KEYWORDS add `FOO BAR` (trie) | 0 | 317 → 17 | 14.0 → 1.0 |
| k12 | bigquery KEYWORDS `BEGIN` | 3 | 288 → 3 | 13.1 → 0.4 |
| k13 | tsql KEYWORDS `DATETIME2` | 9 | 168 → 13 | 7.8 → 0.8 |
| k14 | clickhouse KEYWORDS `ATTACH` | 1 | 144 → 1 | 8.4 → 0.2 |
| k15 | teradata KEYWORDS `BYTEINT` | 1 | 73 → 1 | 1.9 → 0.0 |
| k16 | mysql parser NO_PAREN_FUNCTIONS entry | 1 | 32 → 2 | 6.1 → 2.0 |
| k17 | mysql parser FUNCTIONS lambda | 4 | 4 → 4 | 0.3 → 0.3 |
| k18 | sqlite KEYWORDS `ATTACH` | 1 | 113 → 1 | 5.7 → 0.1 |
| k19 | duckdb KEYWORDS: swap two lines (order only) | 0 | 437 → 437 | The rule never reaches the state diff (name rule fallback). ORDER is covered by a unit test |

## 4. P/Q A/B
These are the patch sets now in /shared/PQ: P has 600 patches and Q has 1000. The numbers are summed simulated seconds, main → moon-21.

- **P:** 4891.3 → 4816.4, **−74.9 s, −5.0 s per 40.**
  - Biggest drops: 5ney −30.0, s368 −10.0, 93uz −9.3; 7vsj, 8txe, h0bv, s4g0 and m6k4 −4.0 each.
- **Q:** 7866.8 → 7738.9, **−127.9 s, −5.1 s per 40.**
  - Biggest drops: b0be −30.0; 0dp3, 5s4o and 8du4 −11.6 each; 2vm8 −9.3; nyj4 −6.9.
- **Rises:** three commits rise by +0.4 s each (21mb, bpr6, 9xlc), which is one cheap extra test. I did not chase them.
  The selection per entry is ⊆ main's, but a different route through the rule (no Fallback on an empty
  key-level set) can change packing.
- **Estimate:** expect about −5 s per 40 practice-style commits. The labeled practice_like set gives −22 s per 40, but it has 2
  tokenizer-hook commits.

## 5. Recommendation
- Merge `moon-21`: `selector/state.py`, `selector/select.py`, `selector/maps.py`, `maps/keys.json.gz`,
  `mapbuild/keytrace.py`, `mapbuild/keymerge.py`, and the tests (`tests/test_keylevel.py`, one expectation in
  `tests/test_state.py`). The unit tests pass: 185 in the pinned image.
- I did not run `docker build` plus the sample under RUN_FLAGS. Do that before shipping. The map is read from `/app/maps`, and
  `state.pkl.keys` sits next to `state.pkl` in `/app/index`.
- **Rebuild the map whenever the pinned image or the suite changes:** run `research/moonshots/21-tools/trace2.sh <repo>` on
  the EC2 host. It runs both traces through drun and merges them.
- **Next step:** whitelist base `Tokenizer.KEYWORDS`. It is now cache-fed only because some test that writes a dict also
  iterates it. Tracking data flow from the whole read to the write would recover k05/k06-type edits: ALL → tens of tests.
  The flag is per test, not a proof of flow.
