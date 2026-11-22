#!/bin/bash
# Subtest store build time / size: cand-a (/w/base) vs moon-25 (/w/repo), same container,
# sequential, twice each (interleaved).
O=/w/out/buildab.txt
for r in base repo base repo; do
  cd /w/$r
  TIMEFORMAT="$r real %R user %U sys %S"
  { time PYTHONHASHSEED=0 python -m selector.subtest build /shared/sqlglot /tmp/st_$r.zip > /dev/null 2>&1 ; } 2>> $O
  echo "$r zip bytes $(stat -c %s /tmp/st_$r.zip)" >> $O
done
echo DONE >> $O
