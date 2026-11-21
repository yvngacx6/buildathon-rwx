# mapbuild

Builds `maps/` (format: [`maps/SCHEMA.md`](../maps/SCHEMA.md)) from sqlglot at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, in the image the organizers labeled with
(`python:3.13-slim@sha256:e48f59cd…`, `pip install -r requirements.lock`, `SKIP_INTEGRATION=0`).
Nothing here reads `sample/answers.json` or any patch; only `sample/tests.json` (the test list).

## Rebuild (one command)

```sh
mapbuild/build.sh            # needs Docker; 560 s on an idle 4-core EC2 box (~35 min when shared)
```

It builds `mapbuild/Dockerfile`, traces all 1620 tests, runs `import_time.py`, and writes
`maps/{lines,classes,reads,test_imports,import_time}.json.gz` and `maps/MANIFEST.json`.
The build stops with `FATAL:` if any check fails.

Check the result against the sample (reads `sample/answers.json`, so it is a check only and
never feeds the maps):

```sh
docker run --rm -v "$PWD:/repo" blast-mapbuild \
  python /mapbuild/validate.py --maps /repo/maps --sample /repo/sample --sqlglot /opt/sqlglot
```

## Files

| File | Role |
|---|---|
| `Dockerfile` | Pinned image + `requirements.lock` + sqlglot tarball at the pinned sha (precompiled). |
| `build.sh` | The one command. |
| `run_tracing.py` | Collection check, runs `trace_one.py` per test in parallel, outcome check, export. |
| `trace_one.py` | Runs one test id in a fresh interpreter with all hooks; writes one trace. |
| `import_time.py` | `import_time.json.gz`: per module under `sqlglot/` and `tests/`, the lines its import runs in a fresh interpreter (see its docstring). |
| `convert_prototype.py` | Legacy: converted the interim laptop-built prototype maps. Not used by `build.sh`. |
| `export.py` | Merges traces into the map files (deterministic gzip) and `MANIFEST.json`. |
| `validate.py` | Sample check: every failing sample test must link to its changed region. |

## How tracing works, and why

**One interpreter per test.** Every test id runs as `pytest <id>` in its own process. All
work in that process is credited to that test: `import sqlglot`, collection of the test file,
module and class bodies, dialects loaded lazily by name, caches filled on first use,
`setUpClass`, and the test itself. So `setUpClass` work is credited to every test of its
class (each test runs it), and no test gets "free" imports because an earlier test in the
same file paid for them. Cost: about 1 s of startup per test, which is fine for an offline build.

**Lines** (`sys.monitoring` LINE events, tool `COVERAGE_ID`). Each callback records
`(file, line)` for files under `sqlglot/` and `tests/` and returns `DISABLE`, so each location
costs one callback per process. Module-level and class-body lines are included (they run at
import, inside the test's process). Monitoring starts before `pytest` or `sqlglot` is imported.

**Process pool.** `tests/test_optimizer.py` and `tests/test_executor.py` use a spawn
`ProcessPoolExecutor`. `trace_one.py` replaces it (in `concurrent.futures` and
`concurrent.futures.process`, before test modules import it) with `InlinePool`, which runs jobs
synchronously and pickle-round-trips `initargs`, arguments and results. This keeps
`sqlglot.serde` / `__reduce__` on the traced path, which the real pool exercises.

**Classes.** A meta-path hook wraps `__init__` of `Expr`, `Tokenizer`, `Parser`, `Generator`
and `Dialect` right after their modules execute (we never import sqlglot ourselves, so tests
that do not import it are not credited with its import). The wrapper records `type(self)`;
at the end every class in its MRO whose module is `sqlglot*` is recorded as
`module.Qualname`. If a wrapped class did not own `__init__` the wrapper would defer with
`super(cls, self).__init__` (report 05 pitfall: calling the base's `__init__` directly skips
overrides such as `TimeUnit.__init__`); at this commit all five own their `__init__`.
Dialect lookups by name (`Dialect["x"]`, `Dialect.get`, `Dialect.get_or_raise`) also record
the resolved class, so a dialect used by name counts even without an instance.

**Reads.** `sys.addaudithook` on `open`. Records paths inside the repo opened for reading
(write modes, directories and `dir_fd`-relative opens such as `shutil.rmtree` internals
are dropped; relative paths resolve against the cwd at the time of the open). Python sources under `sqlglot/` and `tests/` and `.pyc` files are
left out: `lines` and `test_imports` cover those. Every test reads `pyproject.toml` and
`setup.cfg` (pytest config discovery); they are kept, the selector decides.

**Test imports.** At the end of each process, every module in `sys.modules` whose file is
under `tests/` (the test file itself, `tests/__init__.py`, `tests/helpers.py`,
`tests/dialects/test_dialect.py`, ...). This is the transitive import closure of the test
file plus anything imported while the test ran.

## Checks (the build fails loudly)

1. `pytest --collect-only` ids minus `tests.json["skipped"]` equal the 1620 `tests.json` ids.
2. Every traced test has outcome `passed` under the hooks (subtest failures count as failures).
3. Every trace belongs to the id it was launched for; the exported `tests` array is
   `tests.json` in number order, so a map index equals the test number.

Determinism: `PYTHONHASHSEED=0`, sorted keys and lists, gzip `mtime=0`. `MANIFEST.json`
carries `json_sha256` of each map's JSON; two builds give the same digests (only
`built_at` and `trace_wall_seconds` differ).

## Known blind spots

- `tests/dialects/test_dialect.py::TestDialect::test_lazy_load` runs `python -c "import sqlglot"`
  in a subprocess. That child is not traced; its work (importing sqlglot) is the same as the
  parent's import, which is traced.
- Code run through `exec` of generated strings has no repo file name and is not recorded.
- Hash seed: maps use `PYTHONHASHSEED=0`. The labelers ran with random seeds; code whose path
  depends on set/dict-of-str iteration order could differ. Unioning builds over a few seeds
  would cover that (not done; the sample shows no need).
- A class instantiated without `__init__` (e.g. `object.__new__` in `copy`) is seen only via
  the lines map.

## Last build

- 1620/1620 tests traced, all `passed` under the hooks; collection check ok (+1 skipped id).
- Sizes: lines 1.60 MB, import_time 0.10 MB, classes 0.06 MB, reads 0.02 MB,
  test_imports 0.02 MB (1.8 MB total).
- Determinism: lines, classes and test_imports are byte-identical across 4 full builds; reads
  across builds of the same code; import_time across 2 runs.
- `validate.py` on the sample: 73/73 failing tests explained (63 by a changed line, 10 by a
  fixture read), 0 unexplained.

Note for the selector: because each test runs in its own interpreter, module- and class-body
lines are credited to every test whose process imports that module (for example a
`Snowflake.Tokenizer` class attribute maps to every test that loads the Snowflake dialect).
That is the true import-time blast radius; narrowing such edits (by attribute name, the
classes map, or import_time) is the selector's job.
