import os, sys, time, importlib
os.chdir('/home/aditya/rwx-research-robust/sqlglot'); sys.path.insert(0, '.')
os.environ['SKIP_INTEGRATION'] = '0'
def c():
    t = os.times(); return t.user + t.system
t0 = c(); import unittest; t1 = c()
import sqlglot; t2 = c()
for m in sys.argv[1:]:
    importlib.import_module(m); print(m, round(c() - t2, 3)); t2 = c()
print('unittest', round(t1 - t0, 3), 'sqlglot', round(t2 - t1, 3) if not sys.argv[1:] else '', 'total', round(c(), 3))
