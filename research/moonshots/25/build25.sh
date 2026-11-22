#!/bin/bash
# moon-25 build (inside drun): the image's index steps, subtest timed; then calibrate the store's
# CPU numbers to the laptop-built reference store (/w/old_subtest.zip).
cd /w/repo
export BR_SQLGLOT=/shared/sqlglot
mkdir -p /w/out index
python -m compileall -q selector diffexec >/dev/null 2>&1
python -m selector.maps build maps index/maps > /w/out/build.log 2>&1
python -m selector.nameindex build /shared/sqlglot index/names.pkl >> /w/out/build.log 2>&1
( time PYTHONHASHSEED=0 python -m selector.subtest build /shared/sqlglot index/subtest.zip ) > /w/out/sub.log 2>&1
PYTHONHASHSEED=0 python -m selector.slowverify build /shared/sqlglot index/slowverify.pkl >> /w/out/build.log 2>&1
python -m selector.diffexec resources /shared/sqlglot index/resources.pkl >> /w/out/build.log 2>&1
cp index/subtest.zip /w/out/subtest_raw.zip
python research/moonshots/25/calib25.py index/subtest.zip /w/old_subtest.zip > /w/out/calib.log 2>&1
ls -la index >> /w/out/build.log
echo BUILD-DONE >> /w/out/build.log
