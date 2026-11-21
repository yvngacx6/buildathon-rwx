#!/bin/bash
# labelfull.sh <patch> <outdir>: run the FULL sqlglot suite on the patched pinned tree
# (inside the pinned image, via drun) and write <outdir>/<name>.txt = failing/erroring test ids.
set -u
P=$1; O=$2; N=$(basename $P .patch)
[ -s $O/$N.txt ] && exit 0
W=/tmp/lab_$N; rm -rf $W; cp -a /shared/sqlglot $W
if ! python /w/applyp.py $W $P; then echo "APPLYFAIL" > $O/$N.txt; rm -rf $W; exit 0; fi
(cd $W && SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$W timeout 1500 python -m pytest -q -p no:cacheprovider -rfE tests > $O/$N.log 2>&1)
echo "rc=$?" > $O/$N.tmp
grep -E '^(FAILED|ERROR) ' $O/$N.log | sed -E 's/^(FAILED|ERROR) //; s/ - .*$//' >> $O/$N.tmp
tail -1 $O/$N.log >> $O/$N.tmp
mv $O/$N.tmp $O/$N.txt
rm -rf $W
