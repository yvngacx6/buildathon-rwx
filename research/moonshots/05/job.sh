#!/bin/bash
# moon-05 EC2 job: build index, selector runs (stage off/on), ground truth on stage drops.
cd /w/repo
export BR_SQLGLOT=/shared/sqlglot
mkdir -p /w/out
python -m compileall -q selector diffexec >/dev/null 2>&1
python -m selector.maps build maps index/maps > /w/out/build.log 2>&1
python -m selector.nameindex build /shared/sqlglot index/names.pkl >> /w/out/build.log 2>&1
PYTHONHASHSEED=0 python -m selector.subtest build /shared/sqlglot index/subtest.zip >> /w/out/build.log 2>&1
PYTHONHASHSEED=0 python -m selector.slowverify build /shared/sqlglot index/slowverify.pkl >> /w/out/build.log 2>&1
ls -la index >> /w/out/build.log
echo BUILD-DONE > /w/out/status
for set in R:/w/R practice_like:/shared/eval_data/practice_like sample:/w/repo/sample private:/shared/eval_data/private; do
  n=${set%%:*}; d=${set#*:}
  for mode in 0 1; do
    BR_SUBTEST_STAGE=$mode BR_DATA=$d python -m selector > /w/out/$n.$mode.out 2> /w/out/$n.$mode.err
  done
  echo "$n done" >> /w/out/status
done
# ground truth on the stage drops (patched copy of sqlglot, the three TPC tests)
for c in $(cat /w/todo.txt); do
  p=/w/R/commits/$c.patch
  rm -rf /w/sg && cp -r /shared/sqlglot /w/sg
  (cd /w/sg && (git apply "$p" 2>/dev/null || patch -s -p1 < "$p")) || { echo "$c APPLY-FAIL" >> /w/out/truth.txt; continue; }
  r=$(cd /w/sg && SKIP_INTEGRATION=0 PYTHONPATH=/w/sg timeout 900 python -m pytest -q -p no:cacheprovider \
    tests/test_executor.py::TestExecutor::test_execute_tpcds tests/test_executor.py::TestExecutor::test_execute_tpch \
    tests/test_executor.py::TestExecutor::test_optimized_tpch 2>&1 | tail -1)
  echo "$c $r" >> /w/out/truth.txt
done
echo ALL-DONE >> /w/out/status
