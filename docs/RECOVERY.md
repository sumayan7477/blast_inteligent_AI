# Recovery & demo guide (local only)

Everything runs on this machine. Nothing is sent to cloud services; Ollama, the
backend and the frontend bind to `127.0.0.1`.

## Run this

```powershell
cd C:\Users\Sumiya\projects\blast-intelligence-ai
.\backend\venv\Scripts\Activate.ps1
powershell -ExecutionPolicy Bypass -File scripts\stop-local.ps1   # backend must not hold the database

python backend\recovery.py check          # 1. what exists, what is missing (counts only)
python backend\recovery.py validate       # 2. strict automatic relationship + primary-key decisions
python backend\recovery.py prepare-demo   # 3. typed views + analytics mapping + analytics run
python backend\recovery.py test           # 4. synthetic tests + frontend tests + production build

powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1  # 5. Ollama + backend + frontend
python backend\recovery.py check          # 6. confirms /api/health and /api/ai/status
```

Open http://127.0.0.1:5173. Stop everything with `scripts\stop-local.ps1`
(`-IncludeOllama` also stops Ollama).

## What the automation decides (and what it refuses)

| Area | Approved automatically only if | Otherwise |
|---|---|---|
| Relationship | validator status *passed*, target unique & non-null, 0 orphans, 100 % match, >= 100 matched values, >= 20 distinct keys, source <= 50 % null, many-to-one/one-to-one, not ambiguous | stays **disabled**; no join view |
| Primary key | best analyzer candidate that is unique and non-null in DuckDB | no PK (views still built) |
| View column | scalar profile role, values in >= 5 % of rows, <= 1 % conversion failures | kept as text, or excluded (free text never included) |
| Analytics role | passes value checks (type, coverage, plausible ranges/cardinality) | role unavailable; metric shows the reason |
| Durations | valid start/end pair, or a duration column whose name states its unit | duration metrics unavailable |
| Completion rate | a boolean "completed"-style status | unavailable (status values are never guessed) |
| Problems | a dataset whose name says problem/issue/incident/fault/failure/defect | problem metrics unavailable (comments/events are not assumed to be problems) |

Decisions, reason codes and aggregate evidence: `local_reports\recovery_report.md`.

## Your decisions win

Anything you set by hand is never changed: remove the `decided_by: recovery-auto` key from a
relationship, primary key, column or analytics entity you edited, then re-run
`recovery.py prepare-demo`. A relationship you confirm by hand still gets a join view only if it
passes validation.

## When something is off

* `prepare-demo` says the backend is running -> `scripts\stop-local.ps1`, then retry.
* Dashboard panels say *unavailable* -> expected for features the data cannot support;
  the reason is shown on the panel and in `recovery_report.md`.
* Chat says "template answer" -> Ollama or the model is not available; answers are still built
  from the database. Install the model: `ollama pull deepseek-r1:1.5b`.
* After any change to the database, views or mapping: restart the backend
  (`stop-local.ps1`, then `start-local.ps1`).
