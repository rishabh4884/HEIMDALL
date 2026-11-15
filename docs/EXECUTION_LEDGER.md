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
| M3 | Live/cached verification of the end-to-end path through the service (one real workflow; offline repeat) | DoD 3/4 | done for the data path (crash + 502 survived, 4 real products, offline repeat); new real *measurement* blocked by the Gaia outage (last real measurement: NASA-01 06:20Z) |
| M4 | NASA target records: honesty review of saved JSON, regenerate matrix from saved JSON only, acceptance doc | DoD 5/6 | done, pushed `05940e6` |
| M5 | Full suite, diff review, handoff (`docs/FRONTEND_HANDOFF.md`), push | DoD 8/9 | done (see below) |

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
- 08:20:54: worker 3 resumed the interrupted INDEX job on lease expiry; 08:22:53 SIA2 502 -> rescheduled; 08:24:18 COMPLETE via SIA2
  (40 observations); 08:24:50-08:25:59 four real DR1 D2 cutouts acquired and verified; 08:25:59 prefetch READY, manifest
  `man:d32d96d3e47d157d`; offline repeat READY with the same manifest. No resubmission at any point.
- The coordinate investigation stays `PREPARING_DATA` (`CATALOGUE_PENDING`): Gaia is unreachable at IRSA and ESA; it is retried by the
  job (deadline 13:53:30Z, then the recovery policy). Not a scientific result.
- Offline HTTP smoke on the real state: `POST /investigations {"target_id":"NASA-01"}` -> 200 in 22 ms with
  `served_from: verified local data (no archive request)`, manifest `man:e8ea091b52f6041c`; `GET /coverage` (NASA-01 field) -> 368
  indexed observations (243 DR1, 125 QR3) from the local index as of 06:27:38Z.

### M5 — review, hardening, handoff

- Code review of `462c8b4..HEAD` found 10 issues. 8 are fixed in `a808f33`:
  - a negative `Content-Length` bypassed the body limit;
  - internal re-submissions and coverage browsing reset a sub-job's recovery window;
  - cancel could not stop automatic recovery;
  - a stale child-wait progress froze retry backoff;
  - coverage created one job per float coordinate (now snapped to a 0.01° grid);
  - the RA `% 360` edge case;
  - a non-constant-time token comparison;
  - corrupt records were reported as 400.
- Also in `a808f33`: re-evaluation now updates `latest_attempt.json` when it pointed at the superseded version.
- Recorded rather than fixed (no correctness risk for the demo):
  - the worker loop re-reads the jobs table about 5 times per wake; this is O(history) and needs SQL filters plus pruning of
    terminal jobs;
  - a completed target job's `result.investigation_id` keeps naming the version that job produced, even after a later
    re-evaluation (`GET /targets/{id}/versions` shows the link).
- Added `GET /targets/{id}/versions` (`02d0a4f`) and `scripts/demo_submission.sh` (`b37b785`). The final full suite gave 498 OK
  (5 live tests skipped).
- Commits on `backend/submission-2026-11-15`, all pushed:
  - `c4a4751` M1
  - `91ee79d` M2
  - `05940e6` M4
  - `1c0e47d` catalogue single attempt
  - `b37b785` demo script
  - `51ff231` concurrent DB open
  - `441bb64` polls are not attempts
  - `93f8b85` ledger
  - `02d0a4f` versions endpoint
  - `28db40c` verification
  - `a808f33` review fixes
  - plus this ledger commit
- No CI exists in the repository. `main` is unchanged at `462c8b4`.

### M6–M8 (post-submission-core, master-spec continuation; commits `80ddc0a`, `cbb381c`, this commit)

- M6 `80ddc0a`: worker scheduling filtered in SQL, plus indexes on `jobs(state)` and `job_events(job_id, seq)`.
  - The indexes are additive (`IF NOT EXISTS`, no schema-version change).
  - Verified on a disposable copy of the live database: about 0.03 ms per wake.
  - With 5000 finished jobs the per-wake cost is unchanged (test).
- M7 `cbb381c`: `heimdall/targets/claims.py`, contract `heimdall.claims/1.0` (REQ-031).
  - Each claim has a claim type and runs an 8-check defensibility gate; a failed check downgrades the claim.
  - Exposed through the API (`/targets/{id}/claims`, `/investigations/{id}/claims`) and the CLI (`target claims`).
  - Applied to all five saved targets: NASA-02 motion is `CONSISTENT_WITH` with all checks passing. Every other analysis claim is
    `NOT_ASSESSABLE` or `INCONCLUSIVE`, alongside `MEASUREMENT` claims for the forced photometry.
- M8 (this commit): first-class per-series comparison records (`target_variability` 1.2.0, REQ-005, S-12).
  - `NOT_COMPARABLE` series are excluded from the verdict.
  - The claims carry the record of their series.
  - Re-running variability on every saved target's measurements reproduces each saved verdict exactly (no rule change). On NASA-02
    the strongest series is `COMPARABLE_WITH_LIMITATIONS` (noise model, pixel flags), and its other 11 series are `NOT_COMPARABLE`.
- Tests: full suite 507 OK (5 live skipped).

### M9 — readiness for the coverage entry point (S-06)

- `Engine.coverage` / CLI `coverage --source irsa-spherex` answer from the local index (`access: LOCAL_INDEX`, `index_as_of`) or return a
  durable INDEX job (`outcome: PREPARING`, never an archive error, never `NO_OBSERVATIONS` without a successful search). `--live` / `live=True`
  is the explicit operator diagnostic (also used for non-current collections such as QR2); the opt-in live test now states `live=True`.
- Real check (offline, local index): Barnard field detector 2 radius 0.02 -> 63 observations in 4 epochs, identical to the earlier live
  SIA2 answer recorded in `docs/VERIFICATION_REAL_DATA.md`.
- Observed: the demo script's cancelled coordinate request had already spawned an INDEX child (worker picked it up within
  milliseconds); cancellation does not propagate to children (recorded as a known limit; one harmless real SIA2 index of RA 10, Dec -5).
- Tests: full suite (see commit message).

### M10 — readiness-first blind field investigation (completes S-06 apart from stated limits)

- `Engine.investigate` / CLI `investigate --source irsa-spherex`:
  - prefetch the region and freeze a manifest;
  - run the blind pipeline on `LocalStoreSource` with exactly the manifest's observations, catalogue context from cache only;
  - when the data are not local, return the data status and a durable job id (never an archive error, never a non-detection);
  - `--live` keeps the direct path;
  - `reproduce`/`validate` keep the original access path so their results hashes stay comparable.
- Real check, offline from verified local data: run `R20261115T085320-fce9172d` on the 4 genuine DR1 images of `man:d32d96d3e47d157d`
  (details in `docs/VERIFICATION_REAL_DATA.md`).
- Tests: full suite 509 OK (5 live skipped); new `test_blind_investigation_is_readiness_first`.

### M11 — blind field runs as durable jobs over HTTP

- New files and changes:
  - `heimdall/fieldruns.py`: the `FIELD_INVESTIGATION` job kind, the worker handler and `request_field_run`, which reuses a stored run
    keyed by request, config and manifest;
  - `DataService.prefetch_status`: a readiness check with no side effects;
  - prefetch submissions made from a worker are marked internal;
  - `POST /api/v1/runs` and `GET /api/v1/runs/{run_id}`.
- Real check:
  - submit acknowledged in 2.8 ms (202);
  - the worker completed it from `man:d32d96d3e47d157d`;
  - resubmission reused the stored run (200, 10.5 ms);
  - the `results_hash` is identical to the earlier CLI run.
- Worker 5 (pid 97013) runs this code.
- Tests: full suite 516 OK (5 live skipped); new `FieldRunApiTests` and `FieldRunHandlerTests`.

### Final review — scientific corrections (D-008; this commit, rollback-safe on its own)

- A review of `a808f33..bda5b7d` raised 10 candidates. 5 are scientific semantics and are fixed here:
  - comparison limitations are now verdict caveats;
  - sampling follows the configuration;
  - the artefact gate is analysis-specific and status-aware;
  - the measurement claim lists only valid flux plus uncertainty;
  - a null analyses field is guarded.
- Verified on all five saved targets: every variability verdict reproduces unchanged. Claims now say NOT_ASSESSABLE for NASA-01 and
  NASA-05 forced photometry, which was UNUSABLE in every image.
- Tests: full suite 521 OK (5 live skipped).
- The other 5 candidates are operational and go into the next commit.

## Integration plan (D-007, revised; pending, not executed)

No squash. No rewrite, force-push or merge until the owner explicitly approves the verified plan.

1. Gate:
   - all milestones are committed and pushed;
   - the full suite is green;
   - `scripts/demo_submission.sh --tests` passes;
   - review is done and the ledger is final.
2. Verify the cleanup in a disposable clone first:
   - `git filter-branch --msg-filter` removes only the co-author trailer lines (and trailing blank lines);
   - for every commit, compare tree hashes, author name/email/date and committer date with the originals; all must be identical;
   - remaining message text must be unchanged and no trailer may remain;
   - the full suite passes on the rewritten head.
3. Present the verification to the owner and ask for explicit approval.
4. After approval:
   - push a backup branch of the current head;
   - rewrite the development branch locally;
   - `git push --force-with-lease=backend/submission-2026-11-15:<current head> origin backend/submission-2026-11-15`;
   - verify with `git ls-remote`.
5. Merge into `main` with a normal merge that keeps every commit:
   - `git switch main && git pull --ff-only && git merge --no-ff backend/submission-2026-11-15`, or `--ff-only` if the owner prefers;
   - run the full suite;
   - `git push origin main` (a normal push).

## Deferred (explicitly out of today's scope; see `docs/BACKEND_MASTER_SPEC.md` for entry points)

All-sky metadata crawl and mirror (spec §8–9), new ML models (§16), per-detector fidelity campaign (§11), first-class comparison records
(§12), new detection families and pattern graph (§13–14), automated follow-up loop (§15), benchmark campaign (§21), PostgreSQL migration.

## Active jobs / workers

- Worker pid 97013 (`python3 -m heimdall data worker --max-idle 5`), started 08:59Z on the M11 code. Stop it gracefully with
  `kill -TERM 97013`. Open jobs survive a stop and continue when any worker starts.
- Open job `job:target_investigation:c03487f9482a` (coordinate RA 150.1, Dec 2.2) is `PREPARING_DATA` / `CATALOGUE_PENDING`: Gaia is
  unreachable at IRSA and ESA. Its deadline is 13:53:30Z; after that it is re-checked every 6 h for 7 days.
- Inspect read-only with `python3 -m heimdall data jobs` and `python3 -m heimdall data status`.
