#!/bin/bash
# inside container: run base (/w/m21/../m21base mounted? no) -> we run both revs from /w (m21) and /w/m21base (m21base)
set -u
export PYTHONHASHSEED=0
E=$BR_EVAL_DATA
mkdir -p /w/m21/ev
run() { r=$1; d=$2; n=$3; (cd $d && BR_DATA=$4 python -m selector > /w/m21/ev/$r.$n.out 2> /w/m21/ev/$r.$n.err); python /w/m21/score.py $4 $4/answers.json /w/m21/ev/$r.$n.out > /w/m21/ev/$r.$n.score 2>&1; }
for s in $E/practice_like $E/private $E/private41 $E/adversarial $E/adversarial2; do
  n=$(basename $s)
  run new /w/m21 $n $s &
  run base /w/m21base $n $s &
  wait
done
for s in /w/m21/sub /w/m21/probes; do n=$(basename $s); (cd /w/m21 && BR_DATA=$s python -m selector > /w/m21/ev/new.$n.out 2> /w/m21/ev/new.$n.err) & (cd /w/m21base && BR_DATA=$s python -m selector > /w/m21/ev/base.$n.out 2> /w/m21/ev/base.$n.err) & wait; done
echo evals-done
