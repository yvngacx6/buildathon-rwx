# Map schema (contract between mapbuild/ and selector/)

Every map is a gzipped JSON file built **only** from sqlglot at the pinned commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, in the pinned image, with `SKIP_INTEGRATION=0`.
Nothing in this folder may come from labeled patches.

Test ids are pytest node ids exactly as in `tests.json` `"id"` (for example
`tests/test_parser.py::TestParser::test_union`). To save space, every file stores them once:

```json
{"tests": ["<test id 0>", "<test id 1>", ...], "map": {"<key>": [<index into tests>, ...]}}
```

| File | Key | Value: tests that … |
|---|---|---|
| `lines.json.gz` | `"<repo path>:<line>"`, e.g. `"sqlglot/parser.py:812"` | executed that line **while the test ran** (from the start of its setup: `setUpClass`, `setUp`, body, teardown; process-pool work traced inline with pickle round-trip), **outside any module-body execution**. Lines that ran only because a module was imported (at startup, at collection, or lazily inside the test) are left out: use `import_time` and `classes` for those |
| `lines_phase.json.gz` | same | like `lines`, but also keeps lines run by imports that happened during the test (lazy dialect loads). Reference only: **not shipped** (repo size limit); `mapbuild/build.sh` writes it, nothing at build or run time reads it |
| `lines_full.json.gz` | same | everything the test's own interpreter executed, including `import sqlglot` and collection. Reference only: **not shipped**, as `lines_phase` |
| `classes.json.gz` | `"<module>.<Qualname>"`, e.g. `"sqlglot.dialects.tsql.TSQL"` | instantiated that class or a subclass of it (MRO), or used it as a dialect |
| `classes_run.json.gz` | same as `classes` | like `classes`, minus objects made while a sqlglot/ module body executed (import-time templates / tables; the state snapshot's `holders` lists the entries that hold those). Built by `mapbuild/classes_run.py` from the same traces |
| (both `classes` maps) | | then `mapbuild/dialect_reach.py`: a test recorded for a dialect class is also recorded for every sqlglot class in the MRO of that dialect's `parser_class` / `generator_class` / `tokenizer_class` / `jsonpath_tokenizer_class` (inherited ones included), since reading their class attributes (`anonymize`: `parser_class.FUNCTION_PARSERS`) makes no instance |
| `reads.json.gz` | `"<repo path>"`, e.g. `"tests/fixtures/optimizer/simplify.sql"` | opened that file for reading |
| `test_imports.json.gz` | `"<repo path of a module under tests/>"` | live in a test file that imports it (directly or transitively) |
| `import_time.json.gz` | `"<repo path>:<line>"` | **not tests**: here `"tests"` lists module names ("roots"; root 0 is `sqlglot`) and values index them: the modules whose import, in a fresh interpreter, executes that line. A line run by `import sqlglot` is stored as `[0]` only. Built by `mapbuild/import_time.py`. |
| `imports.json.gz` | `"<repo path of a module>"`, e.g. `"sqlglot/dialects/duckdb.py"`; plus `"<isolation-failure>"` | loaded that module when run alone in a fresh pytest process (its file's collection-time import closure + run-time lazy loads, subprocesses and pool workers included). `<isolation-failure>`: tests that failed alone (their record may be incomplete). Top-level lists `"collection"` (repo modules loaded by `pytest --collect-only tests`) and `"collection_imported"` (the same minus test_*.py files only pytest itself imports). Built by `mapbuild/import_map.py`. |
| `MANIFEST.json` | — | sqlglot sha, image digest, number of tests traced, build command, date |

The selector derives function-level sets at run time: tests of a function = union of
`lines` over the function's line span in the old file.
