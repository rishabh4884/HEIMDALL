# Resuming backend work

1. Read `docs/EXECUTION_LEDGER.md` (state, last milestone, next action) and `docs/BACKEND_MASTER_SPEC.md` (scope and deferrals).
2. `git status && git log --oneline -5 && git branch -vv` — work happens on `backend/submission-2026-11-15`; never on `main`.
   Uncommitted edits are listed in the ledger's latest milestone entry if a session was interrupted mid-milestone.
3. Tests: `python3 -W error -m unittest discover -s tests -t .` (about 70 s; 5 live tests skip unless `HEIMDALL_REAL_DATA_TESTS=1`).
4. Data service (read-only inspection): `python3 -m heimdall data status`, `python3 -m heimdall data jobs`, `python3 -m heimdall data job JOB_ID`.
   Open jobs continue when a worker runs: `python3 -m heimdall data worker` (Ctrl-C stops it gracefully) or a bounded
   `python3 -m heimdall data work --max-seconds 600`. Never delete `.heimdall/` — it holds the only copies of the saved evidence.
5. IRSA health: `python3 -m heimdall irsa-status` (bounded; reports each route separately). An outage is not a reason to stop: the
   cached/offline path is `HEIMDALL_OFFLINE=1 python3 -m heimdall target run NASA-01`.
6. Target documentation is regenerated only from saved JSON: `python3 -m heimdall target matrix --write docs/NASA_TARGET_MATRIX.md`.
7. Before committing: inspect `git diff`, stage explicit paths, run the proportionate tests, update the ledger, push to the development branch.
