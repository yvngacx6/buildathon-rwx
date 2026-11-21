#!/bin/bash
# usage: runset.sh <set dir> <tag> [ENV=VAL ...]
cd /home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a6e8708b3c141d589
S=/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m09
D=$1; TAG=$2; shift 2
/usr/bin/time -f "$TAG %e wall %U user %S sys" env "$@" BR_DATA=$D BR_SQLGLOT=/home/aditya/rwx-research-robust/sqlglot \
  /home/aditya/rwx-research-robust/venv/bin/python -S -m selector > $S/$TAG.out 2> $S/$TAG.err
tail -1 $S/$TAG.err
md5sum $S/$TAG.out | cut -c1-8
