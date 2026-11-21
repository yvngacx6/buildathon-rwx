import subprocess,sys,time,resource
t=time.time()
r=subprocess.run(sys.argv[1:])
u=resource.getrusage(resource.RUSAGE_CHILDREN)
print(f"wall={time.time()-t:.2f} cpu={u.ru_utime+u.ru_stime:.2f} maxrss={u.ru_maxrss/1024:.0f}MB rc={r.returncode}",file=sys.stderr)
