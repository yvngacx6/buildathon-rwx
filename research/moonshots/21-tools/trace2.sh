#!/bin/bash
# trace2.sh <workdir>: both key-lookup traces (forward seed 0; reversed seed 1) in the pinned image,
# then merge into <workdir>/maps/keys.json.gz. Run from the EC2 host.
W=$1
run() { ~/shared/drun.sh $W "cp -a /shared/sqlglot /tmp/sg && cd /tmp/sg && env KEYTRACE_REVERSE=$1 PYTHONHASHSEED=$2 SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 BR_SQLGLOT=/tmp/sg PYTHONPATH=/w/mapbuild:/tmp/sg python -m pytest -q -p no:cacheprovider -p keytrace --keytrace-out /w/out/$3.json.gz tests > /w/out/$3.log 2>&1"; }
run 0 0 ktA & run 1 1 ktB & wait
F=$(grep -hE "^(FAILED|SUBFAILED|ERROR)" $W/out/ktA.log $W/out/ktB.log | grep -oE "tests/[^ ]+::[^ \[]+" | sort -u)
python3 $W/mapbuild/keymerge.py $W/out/ktA.json.gz $W/out/ktB.json.gz $W/maps/keys.json.gz $F
