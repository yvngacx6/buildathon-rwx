"""Rescale the new store's CPU numbers (built on a loaded box) to the old store's (main index)."""
import pickle, sys, zipfile, shutil
new, old = sys.argv[1], sys.argv[2]
mo = pickle.loads(zipfile.ZipFile(old).read("meta.pkl"))
zn = zipfile.ZipFile(new)
mn = pickle.loads(zn.read("meta.pkl"))
tot_old = tot_stage = 0
for s, qs in mn["suites"].items():
    for qn, qo in zip(qs, mo["suites"][s]):
        assert qn["sql"] == qo["sql"]
        if qn["execute"] and qn["cpu_exec"] > 0:
            f = qo["cpu_exec"] / qn["cpu_exec"]
            qn["outs"]["cpu_stage"] *= f
            tot_old += qo["cpu_exec"]
            tot_stage += qn["outs"]["cpu_stage"]
        qn["cpu_exec"], qn["cpu_opt"] = qo["cpu_exec"], qo["cpu_opt"]
print("tpcds exec cpu (old store)", round(tot_old, 2), "stage cpu calibrated", round(tot_stage, 2))
tmp = new + ".tmp"
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
    for it in zn.infolist():
        data = pickle.dumps(mn) if it.filename == "meta.pkl" else zn.read(it.filename)
        zo.writestr(it.filename, data)
shutil.move(tmp, new)
