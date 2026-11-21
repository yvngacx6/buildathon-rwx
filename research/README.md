# research/

| File | Content |
|---|---|
| `CONTEXT.md` | The brief that every research agent received. |
| `FINDINGS.md` | **Start here.** Cross-validated, ranked methods. |
| `PLAN.md` | The build plan for the rank-1 method. |
| `agent-reports/01-literature.md` | Regression test selection literature and industry practice. |
| `agent-reports/02-sqlglot-structure.md` | sqlglot layout, line-coverage prototype, sample analysis per commit. |
| `agent-reports/03-execute-to-decide.md` | Running tests at run time: measured on EC2 in Docker. |
| `agent-reports/04-scoring-math.md` | Oracle, packing, risk thresholds, leaderboard analysis. |
| `agent-reports/05-robustness-rules.md` | Unseen change kinds, rules compliance, private validation set. |
| `agent-reports/<nn>-*/` | Each agent's prototype scripts. They are reference code, not production code. |

## Data outside the repo

The prototype data is large. It stays out of git (repo limit 20 MB):

- `~/rwx-research/`: sqlglot clone, venv, line map `covmap2.json.gz` (agent 02).
- `~/rwx-research-robust/`: sqlglot clone, line/use/read maps, private mutants and labels (agent 05).
- EC2 `rwx:~/agent-exec/`: Docker prototype, `map.pkl` (agent 03).
