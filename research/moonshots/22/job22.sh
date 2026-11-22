#!/bin/bash
# moon-22 EC2 job (inside drun): calibrate the store, run every set with rule checkpoints off (0 =
# moon-05) and on (1), each set/mode pair in parallel. Sets: name:dir pairs as args.
cd /w/repo
export BR_SQLGLOT=/shared/sqlglot
O=${OUT:-/w/out}
mkdir -p $O
if [ ! -f index/.calibrated ]; then
  cp index/subtest.zip $O/subtest_uncalibrated.zip
  python research/moonshots/22/calib22.py index/subtest.zip /w/old_subtest.zip > $O/calib.log 2>&1 && touch index/.calibrated
fi
for set in "$@"; do
  n=${set%%:*}; d=${set#*:}
  for mode in 0 1; do
    ( BR_SUBTEST_RULES=$mode BR_DATA=$d python -m selector > $O/$n.$mode.out 2> $O/$n.$mode.err; echo "$n $mode done" >> $O/status ) &
  done
done
wait
echo ALL-DONE >> $O/status
