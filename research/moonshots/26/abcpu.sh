#!/bin/bash
# Usage: abcpu.sh <base-ref> <new-ref> — idle-box (rwx) sequential A/B: one container at a time, scorer
# flags, practice_like / private / the final_check 60-commit P and Q batches (seed 23), for:
# base, new, new with BR_SUBTEST_GEN=0, new with BR_SUBTEST_GEN=0 BR_SUBTEST_RULES=0.
# Prints sim (sum of per-commit cost lines) and container child CPU per (config, set).
set -u; BASE=$1; NEW=$2; W=~/work/ab26; rm -rf $W; mkdir -p $W
for pq in P Q; do
  b=$W/batch$pq; mkdir -p $b/commits; cp ~/shared/PQ/$pq/tests.json $b/
  ls ~/shared/PQ/$pq/commits | python3 -c "import random,sys; c=sorted(sys.stdin.read().split()); random.Random(23).shuffle(c); print('\n'.join(c[:60]))" \
    | while read f; do cp ~/shared/PQ/$pq/commits/$f $b/commits/; done
done
for r in base:$BASE new:$NEW; do
  t=${r%%:*}; ref=${r#*:}; d=$W/src-$t
  git clone -q https://github.com/yvngacx6/buildathon-rwx.git $d && (cd $d && git checkout -q $ref)
  sg docker -c "DOCKER_BUILDKIT=0 docker build -q -t ab26-$t $d" > /dev/null 2>&1 || { echo "BUILD FAILED $t"; exit 1; }
done
one() {  # one <label> <image> <set dir> <extra docker args>
  L=$1; I=$2; S=$3; shift 3
  sg docker -c "docker run --rm --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp $* -v $S/commits:/data/commits:ro -v $S/tests.json:/data/tests.json:ro --entrypoint python $I -c 'import subprocess,os,sys; subprocess.run([\"python\",\"-S\",\"-m\",\"selector\"]); t=os.times(); print(\"CHILDCPU\", round(t.children_user+t.children_system,2), file=sys.stderr)'" > $W/$L.$(basename $S).out 2> $W/$L.$(basename $S).err
  python3 - "$W/$L.$(basename $S).err" "$L" "$(basename $S)" <<'PY'
import re, sys
txt = open(sys.argv[1]).read()
c = [float(m[1]) for m in re.finditer(r"^\[\S+\] \d+ tests, \d+ workers, ([\d.]+)s ::", txt, re.M)]
cpu = re.search(r"CHILDCPU ([\d.]+)", txt)
print(f"{sys.argv[2]:8} {sys.argv[3]:14} {len(c):4} commits  sim {sum(c):8.1f}  cpu {cpu[1] if cpu else '?'}", flush=True)
PY
}
for S in ~/shared/eval_data/practice_like ~/shared/eval_data/private $W/batchP $W/batchQ; do
  one base ab26-base $S
  one new ab26-new $S
  one gen0 ab26-new $S -e BR_SUBTEST_GEN=0
  one off ab26-new $S -e BR_SUBTEST_GEN=0 -e BR_SUBTEST_RULES=0
done
echo "load: $(cut -d' ' -f1-3 /proc/loadavg)"
