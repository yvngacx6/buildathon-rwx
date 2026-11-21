#!/bin/bash
# usage: runall.sh SET NSHARDS   (SET = sample | priv | P)
M=/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m11
O=/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m02
A=/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a8cb1457210c6ab49/research/moonshots/11-assertions/asrv.py
PY=/home/aditya/rwx-research-robust/venv/bin/python
SQ=/home/aditya/rwx-research-robust/sqlglot
case $1 in
  sample) DATA=/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a8cb1457210c6ab49/sample ;;
  priv) DATA=/home/aditya/Desktop/hackathons/buildathon-rwx/eval/data/private ;;
  P) DATA=/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/P ;;
  Q) DATA=/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/Q ;;
esac
SEL=$O/$1.out
[ -n "$3" ] && SEL=$3
STORE=$M/store.pkl
for ((i=0;i<$2;i++)); do
  nohup $PY $A $SQ $DATA $SEL $M/sl_$1_$i.json --do run --store $STORE --shard $i/$2 > $M/sl_$1_$i.log 2>&1 &
done
wait
echo done
