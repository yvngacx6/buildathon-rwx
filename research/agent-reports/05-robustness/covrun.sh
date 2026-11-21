#!/bin/bash
# usage: covrun.sh <sqlglot-dir> <outdir>  -- per-test-file coverage with test_function contexts
SG=$1; OUT=$2; VENV=${VENV:-$HOME/rwx-research-robust/venv}
cat > $OUT/.coveragerc <<RC
[run]
dynamic_context = test_function
source = sqlglot
RC
cd $SG
find tests -name 'test_*.py' | xargs -P ${JOBS:-16} -I{} sh -c 'f={}; n=$(echo $f | tr / _); SKIP_INTEGRATION=0 PYTHONPATH=. '$VENV'/bin/python -m coverage run --rcfile='$OUT'/.coveragerc --data-file='$OUT'/cov.$n -m pytest -q -p no:cacheprovider $f >/dev/null 2>&1; echo done $f >&2'
