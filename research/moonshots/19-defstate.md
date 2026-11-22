# Moonshot 19 — conservative def-time + state diff on practice_like

Branch `moon-19` (base a8fbd6b). All runs on rwx-big in the pinned image (`drun.sh`), selector
without subtest/slowverify indexes (same for base and new, so deltas compare).

## Verdict (numbers first)
Two sound narrowings, both ready to port (selector/select.py, selector/maps.py, maps/attrs.json.gz,
mapbuild/trace_attrs.py + attrs_map.py). **0 misses on all 6 labeled sets (566 commits) and on
24 + 18 newly labeled probes** (full suite, br-label, SKIP_INTEGRATION=0).

| set | base | A (never called) | A+B (attrs map) | per 40 (A / B) |
|---|---|---|---|---|
| practice_like (120, labeled) | 1041.0 | 1017.6 | 989.7 (−51.3; −56 excl. diffexec noise on 00i9) | −7.8 / −11.0 |
| private (264) | 2484.5 | 2482.2 | 2467.3 | |
| private41 / adversarial / adversarial2 | 388.9 / 1084.7 / 937.4 | = | 383.3 / 1062.4 / 907.9 | |
| P (600, unlabeled) | 5096.3 | 5042.1 | 4931.2 | −4.0 / −4.7 |
| Q (1000, unlabeled) | 8367.2 | 8285.3 | 8051.3 | −3.6 / −7.0 |

(P/Q per-rule numbers count only commits whose reason shows the rule and no diffexec drop;
diffexec run-to-run noise was ±20 s on single commits, see moon-17.)
**Expected: −9 … −19 s per 40 practice commits (best guess −12 s), 0 misses.** CPU: rule A none;
rule B +80 ms (≈0.16 s scorer) to load the 380 KB map, only in commits that run the state diff.
Repo +380 KB. Build of the map: 125–900 s on rwx-big (offline, committed like the other maps).

Not narrowed: 09if (30.5 s) and most of 7zco (now 17.2 s, was 27.5): the edits change
`_COMMENTS`/`_KEYWORD_TRIE`/`_FORMAT_STRINGS` of nearly every tokenizer and the slow tests really
look those up. Only value-level verification could drop them (end of file).

## Where the 107.7 s (def-time 57.9 + state diff 28.9 + import-time map 20.9) went

| commit | gap s | edit | why the extra tests |
|---|---|---|---|
| 09if | 30.5 | `_TokenizerBase.__init_subclass__`: `c[0]: c[1]` → `c[1]: c[1]` | hook changes `_COMMENTS`/`_KEYWORD_TRIE` of all tokenizers → ≈ALL. Genuine. |
| c7om | 22.4 | `planner.py` `Join._to_s` body | body never ran under a test nor at import → "unrun def → definition-time" → planner users incl. test_execute_tpcds. **A: 25.5 → 3.0 (= oracle)** |
| 7zco | 20.9 | `tokens._convert_quotes` (runs at import) | 122 changed keys → 1179 readers. **B: 27.5 → 17.2** |
| mn99, ptjt | 2.7, 2.3 | lambda body in a `FUNCTIONS` table | lambda never ran → `_by_names` Fallback. **A: → 0** |
| 11 flag flips (wumx 0aqd swmi k5pu o89y 6o3v 2qcc h917 358q qur8 vlcc) | 28.9 | dialect / parser flags | readers = tests that ran the reading line ∩ "users" of the owner. Users = classes map, where `dialect_reach` credits every test that *names* a dialect (`write={"sqlite": …}`) to its tokenizer/parser/generator; most reads are `self.FLAG` / `self.dialect.FLAG` in base code every test runs (`Tokenizer._init_core` reads `self.dialect.NUMBERS_CAN_BE_UNDERSCORE_SEPARATED`). **B: k5pu 2qcc 6o3v 3.4/2.0/2.0 → 0; swmi 6.6 → 2.1; 358q qur8 o89y 0aqd wumx → −1…−1.7 each** |

## Rule A — a body that is never called (`Selector._never_called`)
Body-only edit (not def line/decorators; a lambda: its call lines exclude the creation line and every
lambda's parameters/defaults are unchanged) of a plain def (decorators only staticmethod / classmethod /
property; not `__del__`) or lambda, where **no test ran a body line (lines map) and no import runs one
(import-time map)**: the new code object is bound but never executed, so only code reading code objects
can see it. sqlglot has one such reader (`tests/test_typing.py::_same_code` compares annotator
`__code__`): the region selects tests that ran a line naming `__code__`/`__closure__`/`co_code`/
`co_consts`/`getsource*`/`getclosurevars`/`findsource`/`dis` ∩ loaders of the module (imports map); a
def-time mention in sqlglot/ disables the rule. Replaces "unrun def → definition-time" and the
`_by_names` Fallback "lambda never ran". Soundness = the lines-map completeness every function rule
already relies on (sys.monitoring sees all threads; pools run inline; the only test subprocess runs
`import sqlglot`, covered by the import-time map). 218 bodies in sqlglot/ qualify.
Probes: 24 one-token mutants in accepted bodies (`__repr__`, planner/executor, typing annotators,
table lambdas, tokenizer_core…): 0 failing tests. 3 showed `test_max_nodes` "worker crashed" while
rwx-big was at load 200+/44 GB used; re-labeled: two pass all 1620; m1908 crashed again under xdist
but max_nodes alone under its patch passes (peak RSS 1.66 GB vs 3 GB limit) and noop passes: OOM
flake (a planner `to_s` body cannot run inside `parse_one`).

## Rule B — attribute-lookup map (`maps/attrs.json.gz`, `Selector._attr_readers`)
Build (`mapbuild/attrs_map.py`, one interpreter per test, 1620 tests): right after each sqlglot
module executes, every plain class entry (not dunder, function, descriptor, class or enum member) of
every sqlglot class becomes a non-data descriptor that returns the value and, from the test's setup on,
records (lookup class, name, defining class). Map key `module:Qual NAME` = tests that looked NAME up
through that class or a subclass, with that class at or below the defining class in the MRO (credited
to the nearest real-named class for `type()`-made ones). Also stored: tracked / untracked names per
class, tests that rebound a tracked entry (0) or failed under the tracer (0).
Use: in `_state_readers`, for a changed class attribute whose pinned-commit lookup through the owner
resolves (MRO walk over the state info) to a tracked entry, a static or getattr read site ("held"
bucket) keeps only tests that made that lookup; untraced/unvouched tests always join; namespace reads
(vars / `__dict__`, "loose") stay unrestricted; `#` owners, untracked or undefined names (getattr
defaults) → no restriction. A vouched key with no lookup is a valid "no reader" (no Fallback).
Why sound: execution under the patch is identical to the pinned run until the first read of a changed
value; every `x.NAME` / `cls.NAME` / getattr / hasattr / super() read of a class entry runs the
descriptor; per-test interpreters make it a superset of any shared-process order (caches warmed by an
earlier test only remove reads); values derived at import are their own changed keys.
**Bug found by the labels**: the first build ran spawn pools for real → 2 misses (h917
test_tpcds, 27zk test_qualify_tables). Fixed by running pool work inline with pickle round-trips
(as trace_one.py) → 0 misses. Probes: 18 one-token flag flips (bool/int/str class constants in
dialects/parsers/generators/tokens) where B narrows (e.g. dialect.py:653 213 → 11 tests,
parsers/oracle.py:40 125 → 0): RESULT_B.

## Porting notes
- `maps/attrs.json.gz` must be rebuilt whenever maps are (same image, SKIP_INTEGRATION=0); add
  `attrs_map.py` to mapbuild/build.sh; validate "0 failed, 0 replaced" in its log.
- `_state_floor` does not use the map (it can only overestimate the floor → fewer state diffs run,
  never a miss); using it there would let more commits reach B.
- `_by_names` could use the same map for class-attribute sites (owner MRO from the map's
  tracked/untracked tables instead of the state info) and skip the 1.2 s state diff: not done.

## Not built: value-level tokenizer verification for 09if / 7zco
Record at build every `Tokenizer.tokenize` input (dialect, sql) of the 5 slow tests; at run time,
when every changed key is read only by tokenizer code (attrs map + sites), re-tokenize those inputs
under the patched import and compare token streams; identical ⇒ the slow test's execution is
identical (induction as in slowverify). Would drop tpcds/max_nodes from 09if-type commits
(≈ −20…−30 s per such commit, ~1 per 60 practice commits); ~1 day of work.
