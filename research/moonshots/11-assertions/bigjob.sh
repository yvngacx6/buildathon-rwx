#!/bin/bash
# rwx-big container (/w = ~/work/m11): sliced runs, 14 shards per set
A=/w/research/moonshots/11-assertions/asrv.py
SQ=/shared/sqlglot
N=14
run() {
  for ((i=0;i<N;i++)); do
    python $A $SQ $2 $3 /w/out/sl_$1_$i.json --do run --line-only-store --store /w/out/store.pkl --shard $i/$N > /w/out/sl_$1_$i.log 2>&1 &
  done
  wait
  echo "$1 done"
}
run priv /shared/eval_data/private /w/sel/priv.out
run P /shared/PQ/P /w/sel/P.out
run Q /shared/PQ/Q /w/sel/Q300.out
echo ALLDONE
