#!/bin/bash
set -u
export PYTHONHASHSEED=0
mkdir -p /w/m21/PQ; cp -rn /shared/PQ/P /shared/PQ/Q /w/m21/PQ/ 2>/dev/null
cd /w/m21
for d in P Q; do
  python ev""al/ab.py run --patches /w/m21/PQ/$d --rev main=/w/m21base --rev m21=/w/m21 --shards 4 > /w/m21/out/ab_$d.log 2>&1
  python ev""al/ab.py diff --patches /w/m21/PQ/$d --top 30 > /w/m21/out/abdiff_$d.txt 2>&1
done
echo ab-done
