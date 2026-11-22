#!/bin/bash
# Usage: pqrun.sh <image tag> [<label> <docker -e args...>] — run image ic-<tag> on /shared/PQ/{P,Q} with the scorer's flags
# (P and Q side by side, 2 containers). Output: ~/work/m26/<tag>.<set>.{out,err}; summary line
# per set: sim s (sum of the selector's per-commit cost lines), container child CPU.
set -u; TAG=$1; LAB=${2:-$1}; shift; shift 2>/dev/null; EXTRA="$*"; O=~/work/m26; mkdir -p $O
one() {
  S=~/shared/PQ/$1
  sg docker -c "docker run --rm --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp $EXTRA -v $S/commits:/data/commits:ro -v $S/tests.json:/data/tests.json:ro --entrypoint python ic-$TAG -c 'import subprocess,os,sys; subprocess.run([\"python\",\"-S\",\"-m\",\"selector\"]); t=os.times(); print(\"CHILDCPU\", round(t.children_user+t.children_system,1), file=sys.stderr)'" > $O/$LAB.$1.out 2> $O/$LAB.$1.err
  python3 - "$O/$LAB.$1.err" "$1" "$LAB" <<'PY'
import re, sys
txt = open(sys.argv[1]).read()
c = [float(m[1]) for m in re.finditer(r"^\[\S+\] \d+ tests, \d+ workers, ([\d.]+)s ::", txt, re.M)]
cpu = re.search(r"CHILDCPU ([\d.]+)", txt)
print(f"{sys.argv[3]} {sys.argv[2]}: {len(c)} commits, sim {sum(c):.1f}s, cpu {cpu[1] if cpu else '?'}s")
PY
}
one P & one Q & wait
