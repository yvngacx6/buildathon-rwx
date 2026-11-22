#!/bin/bash
# moon-05 job 2: calibrate the store's CPU to the laptop-built main store, rerun both modes.
cd /w/repo
export BR_SQLGLOT=/shared/sqlglot
mkdir -p /w/out2
cp index/subtest.zip /w/out2/subtest_ec2_loaded.zip
python /w/calib.py index/subtest.zip /w/old_subtest.zip > /w/out2/calib.log 2>&1
for set in R:/w/R practice_like:/shared/eval_data/practice_like private:/shared/eval_data/private; do
  n=${set%%:*}; d=${set#*:}
  for mode in 0 1; do
    BR_SUBTEST_STAGE=$mode BR_DATA=$d python -m selector > /w/out2/$n.$mode.out 2> /w/out2/$n.$mode.err
  done
  echo "$n done" >> /w/out2/status
done
