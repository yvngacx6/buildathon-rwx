#!/bin/bash
# Ground truth for one part file: $1 = part name. Runs in the drun container (/w, /shared).
P=$1
mkdir -p /w/out
for c in $(cat /w/$P); do
  p=/w/R/commits/$c.patch
  sg=/w/sg_$P
  rm -rf $sg && cp -r /shared/sqlglot $sg
  python /w/apply.py $sg "$p" || { echo "$c APPLY-FAIL" >> /w/out/truth_$P.txt; continue; }
  r=$(cd $sg && SKIP_INTEGRATION=0 PYTHONPATH=$sg timeout 900 python -m pytest -q -p no:cacheprovider \
    tests/test_executor.py::TestExecutor::test_execute_tpcds tests/test_executor.py::TestExecutor::test_execute_tpch \
    tests/test_executor.py::TestExecutor::test_optimized_tpch 2>&1 | tail -1)
  echo "$c $r" >> /w/out/truth_$P.txt
done
echo DONE >> /w/out/truth_$P.txt
