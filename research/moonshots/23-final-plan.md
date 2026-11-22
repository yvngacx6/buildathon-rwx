# 23 — Final-entry plan and checklist (freeze until Sun 08:00 PST)

## Verdict
- **Tool:** `eval/final_check.sh <sha>`. It runs on EC2 `rwx` (4 cores, idle, the same shape as the scorer's build box).
  - It never uses the laptop or a local checkout.
  - It clones the public GitHub repo anonymously, as the scorer does.
  - It exits non-zero on any FAIL. Logs go to `~/final_check/<sha12>/` on rwx.
- **main b4c3591: VERDICT PASS** (13/13). Full run took 5.5 min on rwx. Build 171 s (×2 = 341 s of the 600 s cap); sample 115.057 s, 0 misses, md5 678ca0e3 ×3; 0 misses on all 536 labeled commits; 60-commit batches take 14 s wall (×2 = 28 s of the 270 s limit).
- **Hard facts:**
  - 1 submission per hour (PLAN.md; SUBMISSIONS.md shows them ~62 min apart).
  - Ties within 1 s go to the earlier submission.
  - If nobody picks, the final uses the *best practice* submission, which is not necessarily the safest one. **Pick explicitly.**

## What the tool checks (C1–C7)
| # | Check | Pass rule |
|---|---|---|
| C1 | Anonymous `git clone https://github.com/yvngacx6/buildathon-rwx.git` contains the sha | FAIL if the commit is not pushed. WARN if it is not on main. |
| C2 | `git archive <sha>` size; no `.gitmodules`; no LFS; Dockerfile at top | ≤ 20 MB (main: 14.9 MB, 12.5 MB of it `maps/diffexec.zip`, so **5 MB headroom**) |
| C3 | FROM pinned by digest; every URL in the Dockerfile and in COPY'd build files; downloaders; network code other than `docker/fetch_sqlglot.py`; `ARG SQLGLOT_SHA` = pinned | Only the sqlglot archive at eb11aec and PyPI are allowed |
| C4 | `DOCKER_BUILDKIT=0 docker build --no-cache --platform linux/amd64 --cpu-quota 400000 --memory 8g`, `timeout 600` | FAIL if > 600 s, WARN if > 300 s (the scorer's CPU is ~2× slower) |
| C5 | Sample run 3×, with the Makefile `RUN_FLAGS` verbatim. Run 1 uses the image's own CMD with no wrapper; runs 2–3 wrap it to read cgroup CPU. | Same stdout md5 in all 3, rc 0, `score.py` accepts the output, 0 misses, ≤ 2 MB, deadline guard silent |
| C6 | Every labeled set in `~/shared/eval_data` (private 264, private41, practice_like, adversarial, adversarial2), each with its own tests.json | 0 misses, guard silent (if it fires, WARN: cost blow-up) |
| C7 | 60 commits sampled from `~/shared/PQ/P` and from `PQ/Q` (seed 23), with dummy answers so `score.py` checks the format | Wall ×2 ≤ 270 s (WARN above 135 s), guard silent, format ok |

Not covered:
- The base image is already cached on rwx, so the scorer also pays for a pull, which takes a few seconds.
- The real scorer machine's speed. The ×2 rule stands in for it.
- Unseen kinds. Those are covered only as far as our labeled sets go.

## How to run (identical whether or not the laptop is alive)
```sh
# from anywhere with ssh access (phone/EC2 console works too):
ssh rwx 'rm -rf ~/fc23 && git clone -q -b moon-23 https://github.com/yvngacx6/buildathon-rwx.git ~/fc23 &&
  nohup bash ~/fc23/eval/final_check.sh <SHA> > ~/fc-<SHA>.log 2>&1 < /dev/null &'
ssh rwx 'tail -30 ~/fc-<SHA>.log'          # done when "VERDICT:" appears (~6 min on idle rwx)
# quicker (no labeled sets, P batch only):  ... final_check.sh <SHA> --quick
# subset: SETS="private41 adversarial2" bash ~/fc23/eval/final_check.sh <SHA>
```
The tool lives on branch `moon-23` until it is merged. After the merge, drop `-b moon-23` and clone main, which lacks the script until then.

## Freeze timeline (PST; cutoff Sun 22 Nov 08:00)
| When | What |
|---|---|
| Sat 16:00 → Sun 00:00 | Development window. Every hourly slot goes to an improved main **only after** `final_check` PASS. Keep merges small and labeled-set-verified. |
| **Sun 01:00** | **Last safe merge of a new mechanism.** After this point, only bug/safety fixes and tuning that drops selections are allowed, and each needs a full PASS. |
| **Sun 03:00** | **Candidate freeze.** Tag `final-candidate` on main, run the full `final_check`, and submit at the next slot. |
| **Sun ~04:00** | **Final candidate submitted** (moon-09: 3–4 slots of margin). Board must show valid, 0/40 misses. Then **Pick as our final entry** on the track page (explicit, manual, human). |
| Sun 04:00–07:00 | Only switch if a later commit gains **> 2 s expected on the final**, has a full `final_check` PASS, and has 0 misses on every set. Re-pick explicitly after it scores. Otherwise leave it alone. |
| **Sun 07:00** | **Last submission** (gives one slot of slack before 08:00, and the board needs time to run). Nothing after this. |
| Sun 07:30 | Verify on the track page that the picked entry is the intended commit hash. Screenshot it. |

The pick:
- Pick the **earliest submission of the winning tree**. Never resubmit an identical tree under a new hash, because that only moves the timestamp later.
- If two candidates are within 2 s on practice, pick the one with fewer mechanisms changed since the last full regression.

## Risk policy for late merges
1. **Gate (every merge to main after Sun 00:00):**
   - `final_check` PASS: C4 build < 300 s, C5 identical md5, C6 0 misses on all five sets, C7 wall ×2 < 135 s.
   - Run on the **merged** sha as pushed, not on a branch tip.
2. **Gain threshold:** a merge must show an expected final gain > 2 s. Measure it on private264 + practice_like + P/Q sim, not on the sample only.
3. **What a late change may do:**
   - Allowed: only *remove* work (narrow selections through existing verified mechanisms) or *add* safety.
   - Not allowed: new import-time behaviour, new subprocesses, a new store that grows the repo above 18 MB, or Dockerfile edits after Sun 03:00. The Dockerfile is rebuilt after the cutoff, so a flaky build step is fatal.
4. **Any miss anywhere or any WARN on guard/timing:** don't merge. Revert to the last PASS sha. Never "fix forward" after 03:00.
5. **Rebuild risk:** the final rebuilds every image after the cutoff. `fetch_sqlglot.py` retries 6× and checks the sha256. No PyPI installs are in the build (C3), so the only network dependency is GitHub's archive.

## If the laptop crashes again (it lost 11:00–15:00 on Saturday)
- **Everything must be pushed.** Push every branch after every commit. Unpushed work is lost work.
- Labeled data and P/Q live on EC2 under `~/shared/` (both hosts). The image builds from the public repo. So the whole check-and-submit loop needs only **ssh + a browser**:
  1. From any machine, `ssh rwx` and run the command above with the sha from `git ls-remote https://github.com/yvngacx6/buildathon-rwx.git main`.
  2. Submit on the track page: repo URL plus the sha.
  3. Pick the final entry.
- If ssh keys live only on the laptop: keep the EC2 instance-connect / AWS console path ready (`aws login --profile dcb-admin`). That needs the AWS credentials too, so **write down how to get console access now**.
- Long jobs on EC2 run under `nohup`, so a dropped ssh session doesn't kill them.
- `~/final_check/<sha12>/summary.txt` is the record of what passed.
- Don't keep the final decision in a chat window. Record the chosen sha in `experiments/SUBMISSIONS.md`, push it, and also write it to `~/FINAL_SHA` on rwx.

## Run on main (b4c3591)
Rwx 4 cpu, load 1–2 (one other agent's container running), 2026-11-21 15:57–16:03 PST.
```
PASS  C1 public clone              b4c3591947f9 (11-21 15:51 Moonshot 17 verdict; start 23); on origin/main
PASS  C2 repo size                 git archive 14.90 MB ≤ 20 MB (largest: maps/diffexec.zip 12.5MB; maps/lines.json.gz 0.6MB)
PASS  C2 submodules/LFS/Dockerfile none; Dockerfile at top
PASS  C3 build sources             FROM digest-pinned; URLs: https://github.com/tobymao/sqlglot/archive/{sha
PASS  C4 clean build               170.7s no-cache, 4 cpu, 8 GB (cap 600; x2 scorer = 341s); image 90 MB
PASS  C5 sample x3 (scorer flags)  sim 115.057 s, 0 misses, md5 678ca0e3 x3, cpu 4.51 4.51 s, format ok
PASS  C6 adversarial (46)          sim 1063.238 s, misses 0, cpu 12.15s, wall 12.4s, guard lines 0, rc 0
PASS  C6 adversarial2 (65)         sim 937.356 s, misses 0, cpu 16.42s, wall 16.6s, guard lines 0, rc 0
PASS  C6 practice_like (120)       sim 989.558 s, misses 0, cpu 22.83s, wall 23.0s, guard lines 0, rc 0
PASS  C6 private (264)             sim 2343.527 s, misses 0, cpu 53.96s, wall 54.2s, guard lines 0, rc 0
PASS  C6 private41 (41)            sim 388.873 s, misses 0, cpu 6.19s, wall 6.4s, guard lines 0, rc 0
PASS  C7 batch 60 from P           wall 14.1s (x2 scorer ≈ 28s vs 270), cpu 13.93s, sim 388.502 s (no labels), guard 0, rc 0, format ok
PASS  C7 batch 60 from Q           wall 13.8s (x2 scorer ≈ 28s vs 270), cpu 13.62s, sim 641.636 s (no labels), guard 0, rc 0, format ok
VERDICT: PASS (no FAIL; read any WARN)
```
Notes:
- **The build is the tightest margin.**
  - 171 s now, against 64 s at E011. The growth comes from the subtest, slowverify and resources build steps.
  - At 2× scorer slowness that is 341 s. A new build step must keep the measured time under 300 s, which is the C4 WARN threshold.
  - Compare the measured `build.log` time to this baseline before any Dockerfile change.
- **Repo size:** 14.9 MB of 20 MB. The diffexec store is 12.5 MB, so don't grow it.
- **Wall time:** random 60-commit batches use about 10 % of the final's limit. The worst-case tail (moon-09: the 60 heaviest commits ≈ 210 s on the scorer) is handled by the in-process deadline guard, which C6/C7 confirm stays silent on normal sets.
- **CPU anchor:** sample container CPU is 4.5 s on rwx; with the scorer about 2× slower, that is roughly 9 s on the board.
