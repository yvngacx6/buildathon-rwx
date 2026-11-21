# eval/: measuring recall on a private labeled patch set

`eval/evaluate.py` scores the selector on any set in the `sample/` format
(`commits/<id>.patch`, `tests.json`, `answers.json`). Besides `sample/`, we keep a
**private** set of synthetic patches in `eval/data/private/` (gitignored).

**Rule.** The private set exists only to measure recall. Nothing derived from its patches or
labels (no table, no statistic, no per-file or per-kind rule) may go into the selector or its
image. The selector's data comes only from the unmodified pinned sqlglot commit.
Do not commit `eval/data/`.

```sh
python3 eval/evaluate.py sample private
```

## Generator: `eval/mutants.py`

Deterministic from `--seed` (default 20261120). It edits a scratch clone of sqlglot @
`eb11aec4` and takes `git diff --cached --abbrev=7`, so patches look exactly like the
sample's (`diff --git`, `index`, a/ b/ prefixes; new/deleted-file headers when needed).
Every patch is checked with `git apply --check` against the pristine tree.

| Kind | n | What |
| --- | --- | --- |
| code | 90 | one-token mutant (and/or, ==/!=, is/==, is not/is, </<=, >/>=, int ±1, True/False, drop `not`) over all of `sqlglot/`; ~65% in function bodies, ~22% class-body/module-level definitions, ~13% continuation lines of multi-line conditions |
| comment | 20 | one of the sample's five template comments inserted before a code line |
| fixture | 26 | one-token corruption of a data line in `tests/fixtures/**` (70% optimizer fixtures) |
| multi | 16 | 2–3 of the above stacked in different files |
| docstring | 10 | word typo / insert / upper-case inside a docstring (some doctest lines) |
| whitespace | 6 | blank lines, trailing spaces |
| testedit | 12 | string/token edits or an inserted `assert`/`return` in `tests/test_*.py`, `tests/dialects/test_*.py` |
| helper | 10 | `tests/helpers.py`, `Validator` in `tests/dialects/test_dialect.py`, `tests/leakcheck.py` |
| importerr | 6 | import of a missing name / nonexistent module |
| syntaxerr | 6 | dropped colon / paren, bad indent (mostly sqlglot, some test files) |
| newfile | 6 | new module, dialect, fixture, helper, test file, doc |
| deletefile | 7 | delete a dialect/generator/parser/typing/optimizer/executor module, fixture, test file or doc |
| rename | 9 | rename a def only, rename def + all refs in its file, rename a test method |
| config | 6 | `pyproject.toml` (incl. added `[tool.pytest.ini_options]`), `setup.py`, `setup.cfg`, `Makefile`, `MANIFEST.in` |
| fixturemeta | 9 | `#` metadata lines in optimizer fixtures (`execute:`, `dialect:`, `title:`) |
| executor | 8 | one-token mutants in `sqlglot/executor/*` |
| string | 9 | string-literal edits in sqlglot code |
| dictentry | 9 | one entry removed from a class-body / module-level dict or set literal |

`meta.json` next to the generated patches records each patch's kind and the exact edit.

## Labeling: `eval/label/`

Each patch is applied (`git apply`) to a fresh copy of the pinned tree and the **full** suite
runs in the organizers' image (`python:3.13-slim@sha256:e48f59cd…` + `pip install -r
requirements.lock`, sqlglot on `PYTHONPATH`, `SKIP_INTEGRATION=0`), with the real tests and
their real spawn process pool (not replaced).

- `Dockerfile`, `outcomes_plugin.py`: labeling image; plugin logs every test report
  (incl. pytest 9 subtest reports and collection errors) on the xdist controller.
- `label_one.sh`: one patch, one container, pinned with `--cpuset-cpus` to 2 CPUs (the tests
  size their pool from CPU affinity), `pytest -n 2 --continue-on-collection-errors
  --timeout 120 --timeout-method=thread --max-worker-restart=100000`: a hung test kills only
  its xdist worker and is reported failed.
- `run_queue.py`: runs many containers in parallel, resumable (`<id>.done` markers).
- `parse.py`: a tests.json test fails if any report for it failed (failure, error, timeout,
  crashed worker, failed subtest), if its file had a collection error, or if it never
  reported passed/skipped (not collected: import error, deleted/renamed test).
- `assemble.py`: writes the set in `sample/` format.
- `setup_host.sh`: bootstraps a labeling host.

**Fidelity check:** labeling the 30 sample patches this way reproduces `sample/answers.json`
exactly (30/30, including 3utz's 37 timeouts/failures), and three unpatched baseline runs
have 0 failures and every one of the 1620 tests reports.
