#!/bin/bash
# OFF and gate-wide-open runs of every set (input to eval/rv_sim.py).
# usage (repo root, EC2): eval/rv_open_run.sh IMAGE OUTDIR
IMAGE=$1; OUT=$2; mkdir -p "$OUT"
E=$HOME/shared/eval_data; PQ=$HOME/shared/PQ
F="--fixed BR_RUNVERIFY_GATE=100 --fixed BR_RUNVERIFY_BATCH_S=100000 --reps 1"
(python3 eval/rv_ab.py $IMAGE $OUT $PQ/P $F > $OUT/a.log 2>&1) &
(python3 eval/rv_ab.py $IMAGE $OUT $PQ/Q $F > $OUT/b.log 2>&1) &
(python3 eval/rv_ab.py $IMAGE $OUT $PWD/sample $E/practice_like $E/private41 $E/adversarial \
   $E/adversarial2 $E/private $F > $OUT/c.log 2>&1) &
wait
echo DONE
