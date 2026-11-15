# Execution ledger (backend submission run, 2026-11-15)

Authoritative progress record for this run. Read with `docs/RESUME_BACKEND.md` (how to resume) and `docs/BACKEND_MASTER_SPEC.md` (scope,
requirement IDs, deferrals). The requirement-to-code-to-test-to-evidence matrix is `docs/REQUIREMENTS_LEDGER.md` (reused rather than duplicated).

## Baseline (recorded 2026-11-15 13:15 IST)

- Branch at start: `main` at `462c8b4` (clean working tree; data-service commit already made by the user). Remote `origin` =
  `github.com/rishabh4884/HEIMDALL` (only `main` on the remote). No CI configuration in the repository (`.github/` absent).
- Development branch for this run: `backend/submission-2026-11-15` (created from `462c8b4`). `main` is not modified.
- Baseline tests: `python3 -W error -m unittest discover -s tests -t .` -> `Ran 468 tests ... OK (skipped=5)` (skips = opt-in live tests).
- Live IRSA at 13:15 IST: not probed by this run's first check (the shell has no `timeout`; the probe exited without output). Last
  measured status (06:42 UTC today): TAP timeout, SIA2 OK, direct product 503, calibration OK.
- Saved evidence (git-ignored, `.heimdall/`): five target results under `.heimdall/state/targets/NASA-0N/`; data-service evidence
  `.heimdall/verification/2026-11-15-dataservice/`; IRSA evidence `.heimdall/verification/2026-11-15-irsa/`.

## Delivery plan (deadline: verified core 17:00 IST, handoff 17:30 IST)

| ID | Milestone | Why it matters for the demo | State |
|---|---|---|---|
| M1 | Ledger/plan docs; resolve `must_resubmit` vs deadline contradiction (service-owned recovery); long-running worker with heartbeat, graceful stop, deployment examples | Spec §7: a queue that only moves when someone runs a command is not automatic; `must_resubmit:false` was untrue after a deadline | done (see below) |
| M2 | Stdlib HTTP JSON transport over `Engine` (status, targets, submit/reconnect, results, jobs, cancel, coverage from the local index) with bounded inputs | Frontend integration tonight needs a transport; none existed | planned |
| M3 | Live/cached verification of the end-to-end path through the service (one real workflow; offline repeat) | DoD 3/4 | planned |
| M4 | NASA target records: honesty review of saved JSON, regenerate matrix from saved JSON only, acceptance doc | DoD 5/6 | planned |
| M5 | Full suite, diff review, handoff (`docs/FRONTEND_HANDOFF.md`), push | DoD 8/9 | planned |

## Milestones

### M1 — recovery policy, worker mode, ledger (REQ-027a, REQ-028)

- Files: `heimdall/dataservice/schemas.py` (recovery config `HEIMDALL_DS_RECOVERY_{WINDOW,INTERVAL,ATTEMPT}_S`), `heimdall/dataservice/jobs.py`
  (`recovery_info`, `recover_due`, `must_resubmit`), `heimdall/dataservice/service.py` (`tick` runs recovery; `serve`, heartbeat,
  `worker_health`, `auto_recovery` in job description, worker in metrics), `heimdall/__main__.py` (`data worker`), `deploy/` examples,
  `docs/DATA_SERVICE.md`, this ledger, `docs/RESUME_BACKEND.md`, `docs/DECISIONS.md`, `docs/BACKEND_MASTER_SPEC.md`.
- Schema: no database change (recovery window is derived from `job_events`); status contract adds `job.auto_recovery`; `must_resubmit` is
  now computed (was constant `false`). Additive; existing readers unaffected.
- Tests: `python3 -W error -m unittest tests.test_dataservice tests.test_dataservice_targets` -> 47 OK (6 new: recovery without a user
  request, window not extended by failed recovery attempts, expiry -> `must_resubmit: true`, cancel -> `true`, worker resumes a
  lease-expired job + heartbeat + graceful stop, offline process refuses to be a worker).
- Scientific scope: none (operational only).
- Commit: recorded after push below.

## Deferred (explicitly out of today's scope; see `docs/BACKEND_MASTER_SPEC.md` for entry points)

All-sky metadata crawl and mirror (spec §8–9), new ML models (§16), per-detector fidelity campaign (§11), first-class comparison records
(§12), new detection families and pattern graph (§13–14), automated follow-up loop (§15), benchmark campaign (§21), PostgreSQL migration.

## Active jobs / workers

None started by this run so far. Inspect safely: `python3 -m heimdall data jobs`, `python3 -m heimdall data status` (read-only).
