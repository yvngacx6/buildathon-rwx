#!/bin/bash
# Fidelity: every labeled set with the runverify gate wide open (verify all eligible commits).
# usage (repo root, EC2): eval/rv_fid_run.sh IMAGE OUTDIR
IMAGE=$1; OUT=$2; mkdir -p "$OUT"
E=$HOME/shared/eval_data
F="--fixed BR_RUNVERIFY_GATE=100 --fixed BR_RUNVERIFY_BATCH_S=100000 --only-on --reps 1"
(python3 eval/rv_ab.py $IMAGE $OUT $PWD/sample $E/private41 $E/adversarial $F > $OUT/a.log 2>&1) &
(python3 eval/rv_ab.py $IMAGE $OUT $E/private $E/adversarial2 $F > $OUT/b.log 2>&1) &
(python3 eval/rv_ab.py $IMAGE $OUT $E/practice_like $F > $OUT/c.log 2>&1) &
wait
for s in sample private41 adversarial private adversarial2 practice_like; do
  d=$E/$s; [ $s = sample ] && d=$PWD/sample
  python3 eval/rv_fidelity.py $d $OUT/$s.1.err $OUT/$s.1.out
done > $OUT/fidelity.txt 2>&1
echo DONE
