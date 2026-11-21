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
| `lines.json.gz` | `"<repo path>:<line>"`, e.g. `"sqlglot/parser.py:812"` | executed that line (all code, including module and class bodies; process-pool work traced inline with pickle round-trip; `setUpClass` work credited to every test in the class) |
| `classes.json.gz` | `"<module>.<Qualname>"`, e.g. `"sqlglot.dialects.tsql.TSQL"` | instantiated that class or a subclass of it (MRO), or used it as a dialect |
| `reads.json.gz` | `"<repo path>"`, e.g. `"tests/fixtures/optimizer/simplify.sql"` | opened that file for reading |
| `test_imports.json.gz` | `"<repo path of a module under tests/>"` | live in a test file that imports it (directly or transitively) |
| `MANIFEST.json` | — | sqlglot sha, image digest, number of tests traced, build command, date |

The selector derives function-level sets at run time: tests of a function = union of
`lines` over the function's line span in the old file.
