#!/bin/bash
# Ground truth (inside drun): $1 = file of "dir/commit" lines. Apply each patch to a copy of
# pinned sqlglot with the selector's pure-Python patcher, run the three TPC executor tests.
P=$1
mkdir -p /w/out5
O=/w/out5/truth_$(basename $P).txt
for line in $(cat $P); do
  d=${line%/*}; c=${line##*/}
  sg=/tmp/sg_$(basename $P)
  rm -rf $sg && cp -r /shared/sqlglot $sg
  python /w/repo/research/moonshots/22/apply22.py $sg "$d/commits/$c.patch" || { echo "$c APPLY-FAIL" >> $O; continue; }
  r=$(cd $sg && SKIP_INTEGRATION=0 PYTHONPATH=$sg timeout 1200 python -m pytest -q -p no:cacheprovider -rA \
    tests/test_executor.py::TestExecutor::test_execute_tpcds tests/test_executor.py::TestExecutor::test_execute_tpch \
    tests/test_executor.py::TestExecutor::test_optimized_tpch 2>&1 | grep -E "^(PASSED|FAILED|ERROR)|passed|failed" | tr '\n' ' ')
  echo "$c $r" >> $O
done
echo DONE >> $O
