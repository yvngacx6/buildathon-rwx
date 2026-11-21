#!/bin/bash
# usage: label.sh <sqlglot> <patch> <outdir> : apply patch to a scratch copy, run the full suite, save short summary
SG=$1; P=$2; OUT=$3; N=$(basename $P .patch); W=$OUT/work_$N; VENV=${VENV:-$HOME/rwx-research-robust/venv}
rm -rf $W; mkdir -p $W; (cd $SG && git archive HEAD | tar -x -C $W); cd $W && git init -q && git apply $P || { echo applyfail > $OUT/$N.txt; exit; }
SKIP_INTEGRATION=0 PYTHONPATH=. timeout 900 $VENV/bin/python -m pytest -q -rfE -n ${NJ:-6} -p no:cacheprovider tests > $OUT/$N.txt 2>&1
cd $OUT && rm -rf $W
