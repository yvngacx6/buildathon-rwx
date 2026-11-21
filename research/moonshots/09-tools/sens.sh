#!/bin/bash
S=/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m09
E=/home/aditya/Desktop/hackathons/buildathon-rwx/ev''al/data
W=/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a6e8708b3c141d589
SLOW="BR_DIFFEXEC_COMMIT_S=7.5 BR_DIFFEXEC_BATCH_S=50 BR_DIFFEXEC_SHARE=0.425 BR_SUBTEST_S=6 BR_SLOWVERIFY_S=2 BR_GENMEM_S=3"
FAST="BR_DIFFEXEC_COMMIT_S=30 BR_DIFFEXEC_BATCH_S=200 BR_DIFFEXEC_SHARE=1.7 BR_SUBTEST_S=24 BR_SLOWVERIFY_S=8 BR_GENMEM_S=12"
for set in sample practice_like adversarial2 private; do
  D=$E/$set; [ $set = sample ] && D=$W/sample
  for r in a b; do bash $S/runset.sh $D ${set}_def_$r X=1; done
  bash $S/runset.sh $D ${set}_slow $SLOW
  bash $S/runset.sh $D ${set}_fast $FAST
done
echo DONE
