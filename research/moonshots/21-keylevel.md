# 21 — Key-level import-state diff + key-lookup map (built, validated, branch `moon-21`)

## Verdict and numbers
- **Works, with 0 misses.** Key-level narrowing of changed dict entries in the import-state diff,
  backed by a key-lookup map built from the pinned commit (`maps/keys.json.gz`, **0.90 MB**).
  It is shippable as is. It is a Δ on top of main 904f48c, which sits beneath every other rule.
- **Labeled sets (simulated s, main → moon-21, 0 misses on both sides):**

| set | main | moon-21 | Δ |
|---|---|---|---|
| practice_like (40) | 989.6 | 967.3 | **−22.2** |
| private | 2343.5 | 2312.0 | −31.5 |
| private41 | 388.9 | 388.9 | 0 |
| adversarial | 1063.2 | 1031.4 | −31.8 |
| adversarial2 | 937.4 | 887.9 | −49.5 |

- **Biggest commit wins:**
  - practice_like: 7zco −11.6, 09if −9.3
  - private: dv9g −9.3 (ALL → 432 tests), pw9b −6.5, rywc −5.1, p3ow −4.1
  - adversarial: imp_gen_dispatch −16.4, imp_coerces_to −13.9
  - adversarial2: a2_closure_duckdb_sort −24.8
- **Probes:** 18 keyword / token-type / comment / dispatch-table entry edits (`21-tools/mkprobes.py`). Each was labeled with the
  FULL suite in the pinned image (`21-tools/labelfull.sh`).
  - **0 misses**, and main also has 0. See the table in §3.
  - Typical cut: hook / `_COMMENTS` edits go from ALL (103 s of test time) to 24–47 tests (1.4–4.7 s).
  - Dialect `KEYWORDS` entries go from 73–429 tests to 1–17 tests.
- **P/Q A/B (unlabeled, `eval/ab.py`):** P (600) **−3.8 s/40**, Q (1000) **−4.8 s/40**; see §4.
- **Cost:**
  - Map build: one pytest run of the suite with recording dicts, ~2.5–3.5 min wall on the loaded EC2 box (4.5 CPU-min).
    It is committed, not built in Docker.
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
  - Parity: the traced run gives 1620 passed, 1 skipped, the same as untraced.
- **Entries never narrowed** ("bad", 495 in total):
  - leaky entries
  - entries mutated at run time
  - entries whose value the run itself changed (state snapshot after the session vs before)
  - **cache-fed** entries: read whole in a context that also wrote some import-time dict. This catches
    `_DISPATCH_CACHE ← dict(TRANSFORMS)` and also takes out base `Tokenizer.KEYWORDS` / `SINGLE_TOKENS`.
- **Selector (`_state_readers`)**
  - The per-entry site loop still runs, so its Fallbacks still guard: memo / cached code, special names, TypedDict.
  - For an entry with key-level info, its readers are
    (tests that looked up a changed key at that path ∪ whole readers of that dict and of every dict above it)
    **∩** the entry's site-reader bound. Both are sound upper bounds, so the result is never worse than main.
    When the static scan sees no reader, the map alone is used, since it records every read of the object.
  - `BR_KEYLEVEL=0` disables it.

## 2. Soundness argument and limits
- **Why it holds.** A test that never looked up a changed key, never read the dict or one of its ancestors whole, and
  ran no changed function executes identically up to its first divergence. Every read of the object goes
  through the recording wrapper, because no unswapped reference is left; otherwise the entry is leaky.
- **The remaining trust assumption is cross-test caches** that copy a table into new run-time state that is not an
  import-time dict. Two defences cover them:
  - the static memo guard, as on main;
  - the run-state diff plus the cache-fed rule, for caches that are import-time dicts.
- **Set-valued entries keep today's rule.** A set subclass is unsound: `set(s)`, `frozenset & s`,
  `x |= s` and `issubset` read the set's internals without any Python call. So jtc6 / bc18 / 7evr (sets and scalars) do not change.
- **Scalar entries keep today's rule.** Example: `HEX_START`, copied by the hook.

## 3. Probes (full-suite labels; `checkprobes.py`)
The columns are: failing test ids, then tests selected (main → moon-21), then summed test seconds (main → moon-21). Misses: 0 for
moon-21 and 0 for main on all 18.

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

## 4. P/Q A/B
These are the patch sets now in /shared/PQ: P has 600 patches and Q has 1000. The numbers are summed simulated seconds, main → moon-21.

- **P:** 4891.3 → 4834.8, **−56.5 s, −3.8 s per 40.**
  - Biggest drops: 5ney −30.0, s368 −10.0, 93uz −9.3, m6k4 −4.0.
- **Q:** 7866.8 → 7747.8, **−119.0 s, −4.8 s per 40.**
  - Biggest drops: b0be −30.0; 0dp3, 5s4o and 8du4 −11.6 each; 2vm8 −9.3; nyj4 −6.9.
- **Rises:** three commits rise by +0.4 s each (21mb, bpr6, 9xlc), which is one cheap extra test. I did not chase them.
  The selection per entry is ⊆ main's, but a different route through the rule (no Fallback on an empty
  key-level set) can change packing.
- **Estimate:** expect about −4 s per 40 practice-style commits. The labeled practice_like set gives −22 s per 40, but it has 2
  tokenizer-hook commits.

## 5. Recommendation
- Merge `moon-21`: `selector/state.py`, `selector/select.py`, `selector/maps.py`, `maps/keys.json.gz`,
  `mapbuild/keytrace.py`.
- **Rebuild the map whenever the pinned image or the suite changes:**
  `PYTHONHASHSEED=0 SKIP_INTEGRATION=0 PYTHONPATH=<repo>/mapbuild:<sqlglot> python -m pytest -q -p no:cacheprovider -p keytrace --keytrace-out maps/keys.json.gz tests`
  (cwd = the pinned sqlglot, BR_SQLGLOT set).
- **Next step:** whitelist base `Tokenizer.KEYWORDS`. It is now cache-fed only because some test that writes a dict also
  iterates it. Tracking writes per table instead of per context would recover k05/k06-type edits: ALL → tens of tests.
