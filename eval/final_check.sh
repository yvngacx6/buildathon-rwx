#!/usr/bin/env bash
# Final-entry check: is commit <sha> safe to submit / pick as the final entry?
#
# Runs on EC2 (rwx: 4 cores, near idle = the scorer's build box shape; rwx-big works too but its
# load makes the timings meaningless). Never on the laptop. It uses nothing from the checkout
# it is run from: it clones the PUBLIC GitHub repo anonymously (as the scorer does), checks
# out <sha>, and does everything from that clone.
#
#   usage:  bash eval/final_check.sh <sha> [--quick]
#   remote: ssh rwx 'rm -rf ~/fc23 && git clone -q -b moon-23 https://github.com/yvngacx6/buildathon-rwx.git ~/fc23 \
#                    && nohup bash ~/fc23/eval/final_check.sh <sha> > ~/fc-<sha>.log 2>&1 < /dev/null &'
#           (drop "-b moon-23" once moon-23 is merged into main)
#
# Checks (FAIL → exit 1; WARN → look, but not blocking):
#   C1 public clone has the commit; which remote branches contain it
#   C2 repo limits: git archive ≤ 20 MB, no submodules / LFS, Dockerfile at top
#   C3 build sources: FROM pinned by digest; every URL / downloader in the Dockerfile and the
#      files it runs at build is allowed (sqlglot archive at the pinned sha, PyPI)
#   C4 clean build: --no-cache, linux/amd64, 4 CPUs (--cpu-quota), 8 GB, 10 min cap
#      (FAIL > 600 s, WARN > 300 s: the scorer's CPU is ~2x slower than c7i)
#   C5 make sample x3 with the Makefile's RUN_FLAGS and the image's own CMD: same stdout md5,
#      rc 0, stdout ≤ 2 MB, score.py accepts, 0 misses
#   C6 every labeled set in $DATA/eval_data: sim s, misses (must be 0), container CPU, wall,
#      deadline guard (must not fire)
#   C7 60-commit synthetic batch(es) from $DATA/PQ/{P,Q}: wall vs the final's 4.5 min (x2 for
#      the scorer's slower CPU), container CPU, guard must not fire, format ok
# Results: $OUT (default ~/final_check/<sha12>/), summary at the end and in summary.txt.
#
# Env: DATA (default ~/shared), REPO_URL, OUT, KEEP_IMAGE=1, SETS="private41 adversarial" to limit
# C6, BATCH_SEED (default 23).
set -uo pipefail

SHA_ARG=${1:?usage: final_check.sh <sha> [--quick]}
QUICK=${2:-}
REPO_URL=${REPO_URL:-https://github.com/yvngacx6/buildathon-rwx.git}
DATA=${DATA:-$HOME/shared}
BATCH_SEED=${BATCH_SEED:-23}
PINNED_SQLGLOT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
LIMIT_REPO_BYTES=$((20 * 1024 * 1024))

if ! docker ps >/dev/null 2>&1; then
  if [ -z "${FC_SG:-}" ]; then
    # docker group not active in this session: re-run under sg (the script may come on stdin)
    SELF=$(mktemp); cat "${BASH_SOURCE[0]:-/dev/stdin}" > "$SELF" 2>/dev/null || true
    exec sg docker -c "FC_SG=1 bash $SELF $SHA_ARG $QUICK"
  fi
  echo "docker unavailable"; exit 2
fi

WORK=$(mktemp -d /tmp/fc.XXXXXX); chmod 755 "$WORK"
trap 'rm -rf "$WORK"' EXIT
declare -a RESULTS
FAILS=0
res() {  # res STATUS CHECK DETAIL
  RESULTS+=("$(printf '%-4s  %-28s %s' "$1" "$2" "$3")")
  [ "$1" = FAIL ] && FAILS=$((FAILS + 1))
  echo ">>> $1  $2  $3"
}
now() { date +%s.%N; }
secs() { python3 -c "import sys; print(f'{float(sys.argv[2]) - float(sys.argv[1]):.1f}')" "$1" "$2"; }

echo "== final_check $SHA_ARG on $(hostname) ($(nproc) cpus) at $(date -u +%FT%TZ) / $(TZ=America/Los_Angeles date '+%a %H:%M %Z')"
echo "== load: $(uptime)"

# ---------------------------------------------------------------- C1 public clone
SRC=$WORK/src
if ! GIT_TERMINAL_PROMPT=0 git -c credential.helper= clone -q "$REPO_URL" "$SRC" 2>"$WORK/clone.err"; then
  res FAIL "C1 public clone" "$(tail -1 "$WORK/clone.err")"; printf '%s\n' "${RESULTS[@]}"; exit 1
fi
cd "$SRC" || exit 2
if ! SHA=$(git rev-parse --verify -q "$SHA_ARG^{commit}"); then
  res FAIL "C1 commit on GitHub" "$SHA_ARG not in any pushed branch of $REPO_URL"; printf '%s\n' "${RESULTS[@]}"; exit 1
fi
git checkout -q "$SHA"
SHORT=${SHA:0:12}
OUT=${OUT:-$HOME/final_check/$SHORT}; mkdir -p "$OUT"
BRANCHES=$(git branch -r --contains "$SHA" | sed 's/^ *//' | grep -v HEAD | tr '\n' ' ')
if git merge-base --is-ancestor "$SHA" origin/main; then
  res PASS "C1 public clone" "$SHORT ($(git log -1 --format='%cd %s' --date=format:'%m-%d %H:%M' | cut -c1-60)); on origin/main"
else
  res WARN "C1 public clone" "$SHORT reachable only from: $BRANCHES (not on main)"
fi

# ---------------------------------------------------------------- C2 repo limits
ARCH=$(git archive --format=tar "$SHA" | wc -c)
BIG=$(git ls-tree -r -l "$SHA" | sort -k4 -n -r | head -3 | awk '{printf "%s %.1fMB; ", $5, $4/1048576}')
if [ "$ARCH" -gt "$LIMIT_REPO_BYTES" ]; then
  res FAIL "C2 repo size" "git archive $((ARCH / 1048576)) MB > 20 MB ($BIG)"
else
  res PASS "C2 repo size" "git archive $(python3 -c "print(f'{$ARCH/1048576:.2f}')") MB ≤ 20 MB (largest: $BIG)"
fi
C2=PASS; MSG=""
[ -f Dockerfile ] || { C2=FAIL; MSG+="no Dockerfile at top; "; }
[ -f .gitmodules ] && { C2=FAIL; MSG+=".gitmodules present (not fetched); "; }
grep -qs 'filter=lfs' .gitattributes && { C2=FAIL; MSG+="LFS files (not fetched); "; }
res $C2 "C2 submodules/LFS/Dockerfile" "${MSG:-none; Dockerfile at top}"

# ---------------------------------------------------------------- C3 build sources
C3=PASS; MSG=""
FROM=$(grep -E '^\s*FROM ' Dockerfile)
echo "$FROM" | grep -qE '@sha256:[0-9a-f]{64}' || { C3=WARN; MSG+="FROM not digest-pinned: $FROM; "; }
grep -nE '^\s*ADD\s+https?://' Dockerfile && { C3=FAIL; MSG+="ADD <url>; "; }
# Files the build runs: Dockerfile + everything COPY'd into the image (scripts may download).
BUILD_FILES=$(printf 'Dockerfile\n'; grep -E '^\s*COPY ' Dockerfile | awk '{for (i = 2; i < NF; i++) if ($i !~ /^--/) print $i}')
URLS=$(for f in $BUILD_FILES; do [ -e "$f" ] && grep -rhoIE "https?://[^\"' )>}]+" "$f"; done | sort -u)
for u in $URLS; do
  case "$u" in
    https://github.com/tobymao/sqlglot/archive/${PINNED_SQLGLOT}*|"https://github.com/tobymao/sqlglot/archive/{sha"*) ;;
    https://pypi.org/*|https://files.pythonhosted.org/*) ;;
    *github.com/tobymao/sqlglot/archive/*|*github.com/tobymao/sqlglot/tarball/*|*codeload*) C3=FAIL; MSG+="unpinned sqlglot download $u; " ;;
    https://github.com/tobymao/sqlglot*|https://docs.*|https://www.python.org/*) ;;  # docs links in comments
    *) C3=FAIL; MSG+="URL $u; " ;;
  esac
done
NET=$(for f in $BUILD_FILES; do [ -e "$f" ] && grep -rlIE 'urlopen|urllib\.request|requests\.(get|post)|http\.client|socket\.create_connection' "$f"; done | sort -u | grep -vx docker/fetch_sqlglot.py)
[ -n "$NET" ] && { [ "$C3" = PASS ] && C3=WARN; MSG+="network code in: $(echo $NET); "; }
# The f-string url in fetch_sqlglot.py takes the sha from the Dockerfile's ARG: it must be the pinned one.
grep -qE "^ARG SQLGLOT_SHA=${PINNED_SQLGLOT}\$" Dockerfile || { grep -q SQLGLOT_SHA Dockerfile && { C3=FAIL; MSG+="SQLGLOT_SHA not pinned; "; }; }
DL=$(grep -nE '(curl|wget|git clone|pip3? install|uv pip|npm install|apt-get install)' Dockerfile || true)
if [ -n "$DL" ]; then
  echo "$DL" | grep -E 'pip3? install' | grep -vqE '(==|--require-hashes|-r requirements)' && { C3=WARN; MSG+="unpinned pip install; "; }
  echo "$DL" | grep -qE '(curl|wget|git clone)' && { C3=FAIL; MSG+="downloader in Dockerfile: $(echo "$DL" | head -1); "; }
fi
res $C3 "C3 build sources" "${MSG:-FROM digest-pinned; URLs: $(echo $URLS | tr ' ' ',')}"

# ---------------------------------------------------------------- C4 clean build
IMG=fc-$SHORT
B0=$(now)
DOCKER_BUILDKIT=0 timeout 600 docker build --no-cache --platform linux/amd64 \
  --cpu-period 100000 --cpu-quota 400000 --memory 8g --memory-swap 8g \
  -t "$IMG" . > "$OUT/build.log" 2>&1
RC=$?
BT=$(secs "$B0" "$(now)")
if [ $RC -ne 0 ]; then
  res FAIL "C4 clean build" "rc=$RC after ${BT}s (124 = >600 s); see $OUT/build.log"
  printf '%s\n' "${RESULTS[@]}" | tee "$OUT/summary.txt"; exit 1
fi
SZ=$(docker image inspect -f '{{.Size}}' "$IMG" | awk '{printf "%.0f MB", $1/1e6}')
if python3 -c "import sys; sys.exit(float('$BT') > 300)"; then
  res PASS "C4 clean build" "${BT}s no-cache, 4 cpu, 8 GB (cap 600; x2 scorer = $(python3 -c "print(round(2*$BT))")s); image $SZ"
else
  res WARN "C4 clean build" "${BT}s no-cache (x2 scorer = $(python3 -c "print(round(2*$BT))")s vs 600 cap); image $SZ"
fi
CMD_JSON=$(docker image inspect -f '{{json .Config.Cmd}}' "$IMG")
echo "image CMD: $CMD_JSON"

# ---------------------------------------------------------------- runs
# The Makefile's RUN_FLAGS, verbatim (checked against the clone's Makefile below).
RUN_FLAGS=(--rm --platform linux/amd64 --network none --cpus 2
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp)
MK=$(sed -n '/^RUN_FLAGS/,/tests.json/p' Makefile | tr -d '\\\n\t' | tr -s ' ')
for f in "--network none" "--cpus 2" "--memory 4096m" "--pids-limit 256" "--read-only" "size=64m" "--cap-drop ALL" "--user 65534:65534"; do
  echo "$MK" | grep -q -- "$f" || res WARN "flags" "Makefile RUN_FLAGS lacks '$f' (kit changed?)"
done
# CPU of every process in the container: cgroup v2 usage after the image's own CMD exits.
WRAP='rc=0; "$@" || rc=$?; echo CPUSTAT $(grep usage_usec /sys/fs/cgroup/cpu.stat) >&2; exit $rc'
read -r -a CMD_ARR < <(python3 -c "import json,sys; print(' '.join(json.loads(sys.argv[1])))" "$CMD_JSON")

# run NAME DATADIR TIMEOUT [plain] → $OUT/NAME.{out,err}; sets RRC WALL CPU
run() {
  local name=$1 d=$2 to=$3 mode=${4:-}
  local t0; t0=$(now)
  if [ "$mode" = plain ]; then  # exactly what the scorer runs: the image's CMD, no wrapper
    timeout "$to" docker run "${RUN_FLAGS[@]}" -v "$d/commits:/data/commits:ro" \
      -v "$d/tests.json:/data/tests.json:ro" "$IMG" > "$OUT/$name.out" 2> "$OUT/$name.err"
  else
    timeout "$to" docker run "${RUN_FLAGS[@]}" -v "$d/commits:/data/commits:ro" \
      -v "$d/tests.json:/data/tests.json:ro" "$IMG" sh -c "$WRAP" sh "${CMD_ARR[@]}" \
      > "$OUT/$name.out" 2> "$OUT/$name.err"
  fi
  RRC=$?
  WALL=$(secs "$t0" "$(now)")
  CPU=$(awk '/^CPUSTAT/ {printf "%.2f", $3/1e6}' "$OUT/$name.err"); CPU=${CPU:--}
}
# score NAME DATADIR ANSWERS → sets SIM MISS OKFMT
score() {
  local s; s=$(python3 "$SRC/score.py" "$2" "$3" "$OUT/$1.out" 2>&1); OKFMT=$?
  echo "$s" > "$OUT/$1.score"
  SIM=$(echo "$s" | awk '/^Simulated:/ {print $2}'); SIM=${SIM:--}
  MISS=$(echo "$s" | awk '/^Misses:/ {print $2}'); MISS=${MISS:--}
  [ $OKFMT -ne 0 ] && echo "$s" | head -3
}
guard() { grep -cE 'time budget|^stopped:|selector unavailable|^signal [0-9]+' "$OUT/$1.err"; }
bytes() { wc -c < "$OUT/$1.out"; }

# ---------------------------------------------------------------- C5 sample x3
declare -a MD5S
C5=PASS; MSG=""
for i in 1 2 3; do
  if [ $i = 1 ]; then run sample$i "$SRC/sample" 180 plain; else run sample$i "$SRC/sample" 180; fi
  score sample$i "$SRC/sample" "$SRC/sample/answers.json"
  M=$(md5sum < "$OUT/sample$i.out" | cut -c1-8); MD5S+=("$M")
  echo "sample run $i: rc=$RRC wall=${WALL}s cpu=${CPU}s sim=$SIM misses=$MISS md5=$M bytes=$(bytes sample$i)"
  [ $RRC -ne 0 ] && { C5=FAIL; MSG+="run$i rc=$RRC; "; }
  [ $OKFMT -ne 0 ] && { C5=FAIL; MSG+="run$i score.py rejected; "; }
  [ "$MISS" != 0 ] && { C5=FAIL; MSG+="run$i misses=$MISS; "; }
  [ "$(bytes sample$i)" -gt 2000000 ] && { C5=FAIL; MSG+="stdout > 2 MB; "; }
  [ "$(guard sample$i)" -gt 0 ] && { C5=FAIL; MSG+="deadline guard fired; "; }
  [ $i -gt 1 ] && CPUS+="$CPU "
done
[ "${MD5S[0]}" = "${MD5S[1]}" ] && [ "${MD5S[1]}" = "${MD5S[2]}" ] || { C5=FAIL; MSG+="stdout differs: ${MD5S[*]}; "; }
res $C5 "C5 sample x3 (scorer flags)" "${MSG:-sim ${SIM} s, 0 misses, md5 ${MD5S[0]} x3, cpu ${CPUS}s, format ok}"

# ---------------------------------------------------------------- C6 labeled sets
if [ "$QUICK" != --quick ]; then
  for d in "$DATA"/eval_data/*/; do
    set=$(basename "$d")
    [ -f "$d/answers.json" ] && [ -f "$d/tests.json" ] && [ -d "$d/commits" ] || continue
    [ -n "${SETS:-}" ] && ! echo " $SETS " | grep -q " $set " && continue
    n=$(ls "$d/commits" | wc -l)
    run "set_$set" "${d%/}" 1800
    score "set_$set" "${d%/}" "$d/answers.json"
    G=$(guard "set_$set")
    ST=PASS; [ $RRC -ne 0 ] || [ $OKFMT -ne 0 ] || [ "$MISS" != 0 ] && ST=FAIL
    [ "$G" -gt 0 ] && [ $ST = PASS ] && ST=WARN
    res $ST "C6 $set ($n)" "sim $SIM s, misses $MISS, cpu ${CPU}s, wall ${WALL}s, guard lines $G, rc $RRC"
  done
fi

# ---------------------------------------------------------------- C7 60-commit batches
for pq in P Q; do
  [ "$QUICK" = --quick ] && [ $pq = Q ] && continue
  src=$DATA/PQ/$pq
  [ -d "$src/commits" ] || { res WARN "C7 batch $pq" "no $src/commits"; continue; }
  b=$WORK/batch$pq; mkdir -p "$b/commits"
  ls "$src/commits" | python3 -c "import random,sys; c=sorted(sys.stdin.read().split()); random.Random($BATCH_SEED).shuffle(c); print('\n'.join(c[:60]))" \
    | while read -r f; do cp "$src/commits/$f" "$b/commits/"; done
  cp "$src/tests.json" "$b/tests.json"
  python3 -c "import json,os,sys; d=sys.argv[1]; json.dump({'commits': {f[:-6]: {'failing': [], 'kind': 'code'} for f in os.listdir(d + '/commits')}}, open(d + '/answers.json', 'w'))" "$b"
  chmod -R a+rX "$b"
  run "batch$pq" "$b" 270
  score "batch$pq" "$b" "$b/answers.json"
  G=$(guard "batch$pq")
  X2=$(python3 -c "print(round(2 * $WALL))")
  ST=PASS
  python3 -c "import sys; sys.exit(2 * $WALL <= 135)" && ST=WARN
  python3 -c "import sys; sys.exit(2 * $WALL <= 270)" && ST=FAIL
  [ $RRC -ne 0 ] || [ $OKFMT -ne 0 ] || [ "$G" -gt 0 ] && ST=FAIL
  res $ST "C7 batch 60 from $pq" "wall ${WALL}s (x2 scorer ≈ ${X2}s vs 270), cpu ${CPU}s, sim $SIM s (no labels), guard $G, rc $RRC, format $([ $OKFMT = 0 ] && echo ok || echo REJECTED)"
done

[ -n "${KEEP_IMAGE:-}" ] || docker rmi -f "$IMG" > /dev/null 2>&1
echo "== load: $(uptime)"
{
  echo "final_check $SHA  host $(hostname)  $(date -u +%FT%TZ)"
  printf '%s\n' "${RESULTS[@]}"
  [ $FAILS = 0 ] && echo "VERDICT: PASS (no FAIL; read any WARN)" || echo "VERDICT: FAIL ($FAILS)"
} | tee "$OUT/summary.txt"
echo "logs: $OUT"
[ $FAILS = 0 ]
