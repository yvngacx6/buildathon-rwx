"""Make a subset dir R/ of the budget-refused commits of P, Q, private, practice_like."""
import json, os, re, shutil
D = os.path.dirname(os.path.abspath(__file__))
S = os.path.dirname(D)
src = {"P": f"{S}/P", "Q": f"{S}/Q", "private": f"{D}/private", "practice_like": f"{D}/practice_like"}
out = f"{D}/R"
shutil.rmtree(out, ignore_errors=True)
os.makedirs(out + "/commits")
answers = {}
ids = []
for name, d in src.items():
    err = open(f"{D}/{name}.err").read().splitlines()
    for l in err:
        m = re.match(r"\[(\w+)\]", l)
        if m and "subtest: keep (est" in l:
            c = m.group(1)
            ids.append((name, c))
            shutil.copy(f"{d}/commits/{c}.patch", f"{out}/commits/{name[:2]}_{c}.patch")
            a = f"{d}/answers.json"
            if os.path.exists(a):
                answers[f"{name[:2]}_{c}"] = json.load(open(a))["commits"][c]
shutil.copy(f"{src['private']}/tests.json", out + "/tests.json")
json.dump({"commits": answers}, open(out + "/labels.json", "w"))
print(len(ids), "commits;", len(answers), "labeled")
