#!/bin/bash
# moon-25 runs (inside drun): each set with the generate checkpoint off (0) and on (1), the two
# modes in parallel, sets one after another. Args: name:dir pairs.
cd /w/repo
export BR_SQLGLOT=/shared/sqlglot
O=${OUT:-/w/out}
mkdir -p $O
for set in "$@"; do
  n=${set%%:*}; d=${set#*:}
  for mode in 0 1; do
    ( BR_SUBTEST_GEN=$mode BR_DATA=$d python -m selector > $O/$n.$mode.out 2> $O/$n.$mode.err; echo "$n $mode done" >> $O/status ) &
  done
  wait
done
echo ALL-DONE >> $O/status
