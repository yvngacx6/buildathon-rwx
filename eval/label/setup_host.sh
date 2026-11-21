#!/bin/bash
# One-time setup of a labeling host (Ubuntu + docker + git). Run from the repo root on the laptop:
#   eval/label/setup_host.sh <ssh-host> <pinned sqlglot git checkout>
# Copies the harness, builds the labeling image (pinned python:3.13-slim + requirements.lock)
# and a pristine tarball of sqlglot @ pinned commit (git archive: tracked files only).
set -eu
H=$1; SG=$2
ssh "$H" 'mkdir -p ~/label/harness'
git -C "$SG" archive --format=tar eb11aec4be8cb2122afe713a4816662c3f0fb2fa | ssh "$H" 'cat > ~/label/pristine.tar'
scp -q eval/label/* requirements.lock "$H":label/harness/
ssh "$H" 'cd ~/label/harness && sudo docker build -q -t br-label .'
# then:  ssh H 'cd ~/label && python3 harness/run_queue.py <patch_dir> <out_dir> <slots>'
#        ssh H 'cd ~/label && python3 harness/parse.py <tests.json> <out_dir>' > labels.json
#        eval/label/assemble.py <gen_dir> labels.json sample/tests.json eval/data/private
