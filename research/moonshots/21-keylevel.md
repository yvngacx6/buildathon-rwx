# 21 — Key-level import-state diff + key-lookup map

DRAFT (numbers pending)

## Mechanism
- `selector/state.py`: build-time per-key tables for every exact-dict state entry
  (`index/state.pkl.keys`, 4.4 MB, image only), recursing into nested dicts (tries). At run time the
  diff subprocess reports, per changed dict entry, the (nested path, key) pairs that differ
  (`found["keys"]`). Keys are hash-free ids (`state.kid`).
- `mapbuild/keytrace.py`: pytest plugin, pinned commit. Wraps every import-time dict (and nested
  dicts) in a recording `dict` subclass and swaps every reference (gc pass). Records per test:
  key reads (hits and misses), whole reads, writes.
- `selector/select.py::_state_readers`: for a changed entry with key-level info, take the
  map's tests instead of the static reader sites. The site loop still runs for its Fallbacks.
