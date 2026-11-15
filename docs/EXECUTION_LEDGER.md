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
| M1 | Ledger/plan docs; resolve `must_resubmit` vs deadline contradiction (service-owned recovery); long-running worker with heartbeat, graceful stop, deployment examples | Spec §7: a queue that only moves when someone runs a command is not automatic; `must_resubmit:false` was untrue after a deadline | done, pushed `c4a4751` |
| M2 | Stdlib HTTP JSON transport over `Engine` (status, targets, submit/reconnect, results, jobs, cancel, coverage from the local index) with bounded inputs | Frontend integration tonight needs a transport; none existed | done, pushed `91ee79d` |
| M3 | Live/cached verification of the end-to-end path through the service (one real workflow; offline repeat) | DoD 3/4 | in progress (see below); fixes found by it pushed |
| M4 | NASA target records: honesty review of saved JSON, regenerate matrix from saved JSON only, acceptance doc | DoD 5/6 | done, pushed `05940e6` |
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
- Commit: `c4a4751` pushed to `origin/backend/submission-2026-11-15` (verified with `git ls-remote`). No CI exists.

### M2 — HTTP transport, readiness-first coverage, worker heartbeat thread (REQ-029, S-18)

- Files: `heimdall/server.py` (new), `heimdall/__main__.py` (`serve [--with-worker]`), `heimdall/dataservice/service.py` (`coverage`;
  heartbeat pulse thread so a long job step does not look like a dead worker), `heimdall/targets/prepare.py` + `heimdall/api.py`
  (`analyse_inline=False`: requests never compute inline), `tests/test_server.py` (new, 7), `tests/test_dataservice_targets.py` (+1),
  `docs/FRONTEND_HANDOFF.md` (new), README and `docs/UI_BACKEND_REFERENCE.md` pointers, REQ-029.
- Schema: new contract `heimdall.http/1.0` (transport envelope only; result and status schemas unchanged).
- Tests: `python3 -W error -m unittest discover -s tests -t .` -> 482 OK (5 live skipped).
- Live check in progress at commit time: coordinate request `job:target_investigation:c03487f9482a` (RA 150.1, Dec 2.2, VARIABLE_STAR,
  8 observations) submitted 07:53:30Z, worker pid 89947 (`data worker`); evidence `.heimdall/verification/2026-11-15-submission/`.

### M4 — honesty review of the five target records (REQ-030, D-006)

- Finding: NASA-02's `VARIABILITY_DETECTED_WITH_CAVEATS` came from one 3-point series (dof 2, 5.65 sigma formal) with only 2 completed
  control series (median reduced chi2 14.4 vs the target's 17.9); method 1.0.0 counted the *absent* empirical false-alarm comparison as
  passed. Fixed in `targets/variability.py` 1.1.0 (`SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS`, reason states the reference is missing).
- All saved target JSON scanned: only NASA-02 affected. Re-evaluated from saved measurements (no new data):
  `HEIMDALL_OFFLINE=1 python3 -m heimdall target reevaluate NASA-02 --reason "..."` -> `tinv:NASA-02:0c714738` supersedes
  `tinv:NASA-02:c3c6c224` (sha256 `4fa93189...` verified unchanged; backup and output in `.heimdall/verification/2026-11-15-submission/`).
  NASA-02's motion result, hypotheses and conclusion are unchanged (built from the motion analysis).
- Two synthetic tests asserted a detection without control stars (encoding the defect); they now assert the corrected verdict and the
  > 5 sigma formal recovery. The 12-control positive-control test still detects.
- Regenerated `docs/NASA_TARGET_MATRIX.md` from saved JSON (`target matrix --write`); fixed stale `docs/NASA_ACCEPTANCE.md` row 1
  (claimed NASA-05 blocked; it completed) and annotated the NASA-02 row in `docs/VERIFICATION_REAL_DATA.md`.
- Tests: full suite 485 OK (5 live skipped).

### M3 — live verification and the defects it exposed

Fixes found by running the real service (each with a regression test, full suite green before each push):

| Commit | Defect observed live | Fix |
|---|---|---|
| `1c0e47d` | Gaia outage (IRSA TAP and ESA both timing out): the worker's inner HTTP retries held the single worker ~12 min per attempt; other jobs starved | worker catalogue calls make one bounded attempt (no inner retries, 30 s timeout); the job owns retries (`CATALOGUE_PENDING` -> `PREPARING_DATA`) |
| `b37b785` | — | `scripts/demo_submission.sh` (offline/cached demo + HTTP API) |
| `51ff231` | `serve --with-worker` on a fresh state directory: `database is locked` when two connections initialised SQLite at once | busy timeout, bounded init retry, conditional WAL/user_version writes, API service opened before the worker |
| `441bb64` | a prefetch parent showed 62 "attempts" while only polling its child | polls of a waiting child are not attempts |

Live observations (UTC, evidence `.heimdall/verification/2026-11-15-submission/`):

- 07:52: `irsa-status`: TAP timeout (45.8 s), SIA2 OK (1.2 s), product cutout OK (3.95 MB FITS, 15.7 s), calibration OK.
- 07:53:30: coordinate request (RA 150.1, Dec 2.2, VARIABLE_STAR, 8 observations) accepted immediately as
  `job:target_investigation:c03487f9482a` (`coord_submit.json`, exit 0, `must_resubmit: false`).
- 08:05:54: after the catalogue outage the job moved to `PREPARING_DATA` with `CATALOGUE_PENDING` ("reference astrometry not yet
  available (SERVICE_FAILED: request to gea.esac.esa.int timed out)"); never recorded as "no match".
- 08:06:01: worker 1 (pid 89947) killed with SIGKILL mid-job (crash drill); worker 2 started 08:06:22; the in-flight INDEX job of the
  prefetch `job:prefetch:e00cff225e3a` keeps its lease until expiry and is then resumed by `resume()` (lease-based recovery).
- 08:14:11: worker 2 stopped with SIGTERM: `stopped_gracefully: true`, final heartbeat `stopped: true`; worker 3 (pid 92286) started.
- Offline HTTP smoke on the real state: `POST /investigations {"target_id":"NASA-01"}` -> 200 in 22 ms with
  `served_from: verified local data (no archive request)`, manifest `man:e8ea091b52f6041c`; `GET /coverage` (NASA-01 field) -> 368
  indexed observations (243 DR1, 125 QR3) from the local index as of 06:27:38Z.

## Deferred (explicitly out of today's scope; see `docs/BACKEND_MASTER_SPEC.md` for entry points)

All-sky metadata crawl and mirror (spec §8–9), new ML models (§16), per-detector fidelity campaign (§11), first-class comparison records
(§12), new detection families and pattern graph (§13–14), automated follow-up loop (§15), benchmark campaign (§21), PostgreSQL migration.

## Active jobs / workers

Worker pid 92286 (`python3 -m heimdall data worker --max-idle 10`, started 08:14Z; stop gracefully with `kill -TERM 92286`). Open jobs: `job:target_investigation:c03487f9482a`, `job:prefetch:e00cff225e3a`. Inspect safely: `python3 -m heimdall data jobs`, `python3 -m heimdall data status` (read-only).
