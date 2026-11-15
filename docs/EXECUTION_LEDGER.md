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

### Final review — operational fixes (this commit)

- New terminal state `FAILED` (readiness `FAILED`):
  - request/configuration errors fail at once;
  - unexpected software errors are retried at most 3 times;
  - such errors are never left to run into the archive deadline and be reported as an outage;
  - `must_resubmit: true`.
- `reproduce`/`validate` use the original run's access path: manifest-based runs replay from local data, older runs use the archive
  adapter's cache.
- Readiness-path blind runs:
  - an explicit `obs_ids` list that differs from the service plan is refused (use `--live`);
  - the bounded default of 16 observations is recorded as a result warning.
- Coverage `per_detector` is computed over the whole indexed set.
- `POST /runs` answers 202 only while preparing; a completed duplicate job returns its run.
- `scripts/demo_submission.sh` shows claims, local-index coverage and field-run reuse.
- Tests: full suite (see commit message).

### Final verification — cancellation, archive rejections, side-effect-free demo (this commit)

- Found by the final demo run against the live state: see `docs/VERIFICATION_REAL_DATA.md`, "Unintended real acquisition, recorded".
- Fixes:
  - `JobQueue.cancel` propagates to the child the parent waits on unless another open job shares it;
  - a product rejected twice with a permanent 4xx is excluded (`ARCHIVE_REJECTED`) rather than retried into the deadline;
  - the demo sends its uncached example to a throwaway state directory with no worker. Verified: 15 real-state jobs before and after
    the demo.
- Tests: full suite 524 OK (5 live skipped); new `CancelAndRejectionTests`.

## Run 2 (2026-11-15, from 15:27 IST): audit defects, contracts, all-sky inventory

Start state: `main` = `origin/main` = `5ac6d2d`; identity `Rishabh Patel <rishabhjpatel@gmail.com>`; clean tree; worker pid 98970 running.

The owner's independent audit reported:
- 524 tests collected: 519 passed and 5 opt-in live tests skipped;
- a separate live run: 2 passes, 2 failures, 1 error (reported, not re-run here).

Schedule (Asia/Kolkata): defects A–D to about 16:00; frontend contracts to about 16:45; partitioned all-sky inventory to about 17:25;
17:30–18:00 is verification only.

### R2-M1 — audit defects A–D (this commit)

| Defect | Reproduction | Fix | Regression evidence |
|---|---|---|---|
| A: incomplete artefact checks could pass the detection gate | Under the old code (`5ac6d2d`), NASA-02's saved result with a forced `MOTION_DETECTED_INCONSISTENT_WITH_CATALOGUE`, `blend_confusion` removed and `registration_artifact` set to `DISFAVOURED` gave `DETECTION` with the gate passing | Claims 1.2.0: every expected artefact hypothesis of the analysis (motion: `registration_artifact`, `blend_confusion`; variability: `calibration_artifact`, `blend_contamination`) must be accounted for. Missing, unknown, malformed, conflicting-duplicate or unassessed states count as unresolved; `NOT_APPLICABLE` counts as excluded only with a `not_applicable_reason`. Neighbouring checks were audited: assertive claims need evidence items; duplicate evidence ids fail; uncertainties must be finite and positive; an assertive motion claim needs measured track points; every observation used needs a product record | `ClaimGateAuditTests` (5) covering positive and rejection cases. All five saved targets give the same claim types as before |
| B: the fresh-state demo failed without saved state | `scripts/demo_submission.sh` with an empty state directory | `heimdall/evidence.py` and CLI `evidence export\|verify\|import`: a checksummed portable bundle; import re-roots paths, prunes rows whose objects are absent and cancels open jobs. The demo gained `--bundle`, prerequisite checks (exit 3 with exact instructions) and a fresh measurement. Also fixed: store paths are now content-addressed from the store root (portable), and stored-result reuse is version-exact (it used to read `latest.json`, returning a newer version for an older key) | `tests/test_evidence.py` (2). Real bundle `evb:1f137a21a94c0c43`: 111 files, 88 MB, 19 genuine products, 3 manifests, kept outside Git. Fresh-state demo with the network disabled exits 0: NASA-01 labelled `STORED_RESULT_REPLAY` with the bundle id; a fresh blind run on the imported products has a results hash identical to the bundled run. A corrupted bundle is refused with nothing written; missing evidence exits 3 |
| C: `check_access` raised `RETRIEVAL_FAILED` on 502s, which the contract forbids | Both routes returned 502 | `access_error()`: 401→`AUTHENTICATION_FAILED`, 403→`ACCESS_DENIED`, all timeouts→`ARCHIVE_TIMEOUT`, 5xx/connection/unexpected→`ARCHIVE_UNREACHABLE`; the `classification`, `http_status` and per-route detail are kept. The HTTP API maps outage codes to 503 with `category: UPSTREAM_OUTAGE`; discovery keeps `RETRIEVAL_FAILED` for item-level failures | `test_access_errors_follow_the_contract_and_keep_the_outage_classification` and `IrsaContractUnderOutageTests` (deterministic transport) |
| D: HTTP error responses leaked resources | Real-socket test against a local server: 25×502 + 1×429 gave 26 `ResourceWarning`s (`Implicitly cleaning up <HTTPError 502>`) | `urllib_transport` closes the `HTTPError` on every path. Also, a server `Retry-After` now reaches the durable job's next attempt (bounded to 1 h) | `tests/test_http_transport.py` (0 leaks across error, 429, success and timeout); `RetryAfterTests` |

Also fixed: stored products and calibrations are re-hashed when read for analysis, so a same-size corruption raises `DATA_MALFORMED`
instead of being analysed. Full suite: 535 OK (5 live skipped).

### R2-M2 — scientist-facing contracts (this commit)

- Claims 1.3.0: method validation changes strength and wording. `VALIDATED_METHODS` is empty: no target method has real-data
  validation.
  - An unvalidated DETECTION becomes `CANDIDATE` (`downgraded_from`).
  - Other assertive claims carry `strength: PROVISIONAL_UNVALIDATED_METHOD` and a "Provisional (method not validated on real data)"
    statement.
  - `quick_answer` is regenerated from the admissible headline claim; the saved text is kept as `saved_quick_answer`.
  - `docs/NASA_TARGET_MATRIX.md` was regenerated and leads with the claim-derived answers.
- `heimdall/scientist.py`, served by the API, the CLI and the HTTP server:
  - `capabilities` (`heimdall.capabilities/1.0`);
  - `compare` (`heimdall.comparison/1.0`, with a comparability verdict; `NOT_COMPARABLE` pairs give no change statement);
  - `lookup` (`heimdall.evidence_lookup/1.0`, the reverse traversal).
  - Routes: API `GET /capabilities`, `/compare`, `/evidence/lookup`; CLI `scientist capabilities|compare|lookup`.
- HTTP coverage answers from the exact requested cone when it is already indexed, and snaps to the grid only when an index job is
  needed. Before this, the snap enlarged the cone and needlessly queued a job (found by the example client).
- `scripts/frontend_example.py` is the executable contract walk-through. It passed against a fresh state imported from bundle
  `evb:1f137a21a94c0c43` with the server offline.
- Real data:
  - NASA-02 `c3c6c224` vs `0c714738` is `COMPARABLE_WITH_LIMITATIONS` (method versions differ) and shows the variability change and
    the supersession;
  - NASA-01 vs NASA-02 is `NOT_COMPARABLE`;
  - the CLI and API field runs are `COMPARABLE` with an identical hash;
  - reverse lookup of `2025W17_4B_0017_1D2` returns both NASA-02 versions and their claims.
- Tests: full suite 543 OK (5 live skipped).

### R2-M3 — all-sky inventory, mirror planning, follow-up loop, pattern record (this commit)

- **Discovery truncation.** SIA2 discovery sends an explicit `MAXREC` and records per-collection counts and `truncated`. The schema v2
  migration adds `syncs.truncated` and `allsky_partitions`. It was verified on a disposable copy of the live database: schema 1→2, and
  indexed regions answer identically (63 / 40 / 341 observations).
- **`dataservice/allsky.py`:**
  - partitions with polar caps and circumscribing cones;
  - bounded steps through durable INDEX jobs;
  - truncation subdivision, failed-partition retry and refresh;
  - accounting that keeps metadata, downloaded and used coverage apart;
  - `mirror_plan` with measured-size estimates, refusal when nothing is measured, and pin protection.
- **Real pilot.** Domain `T2:148,152,0,4` (4 partitions, 15.99 deg²) was planned and submitted at 10:18:56Z. IRSA discovery answered
  HTTP 502 on both routes at 10:18Z and 10:24Z, and the circuit breakers opened. All 4 partitions stay `QUEUED` behind INDEX jobs that
  retry automatically; none is mislabelled as searched or empty. Evidence: `.heimdall/verification/2026-11-15-submission/allsky_pilot_*.json`.
- **`heimdall/followup.py`.** Discriminating actions with hypotheses distinguished, predictions, cost from measured sizes, availability
  and expected value. Saturation lowers "more epochs" below "other detectors". Actions are persisted with deterministic ids and never
  repeated; there is a budget and a stopping reason. Real plans:
  - NASA-01 → `other_detectors` first, then `more_epochs` (LOW expected value);
  - NASA-02 → no runnable action (deblending and more control stars are unavailable, with reasons).
- **`heimdall/patterns.py`.** Motion, temporal, spectral and spatial records with links, guards and missing dimensions. NASA-02 has all
  four present; NASA-04 is missing motion (not run) and spectral (not assessable).
- Tests: full suite 550 OK (5 live skipped).

### R2-M4 — catalogue-outage policy and external-service evidence (this commit)

- **Policy (`targets/prepare.py`).** Catalogue context is retried for `CATALOGUE_GRACE_ATTEMPTS` (3) failures. After that:
  - an arbitrary-coordinate request proceeds with a coordinate-only reference;
  - any request proceeds without field controls when only the field catalogue is missing;
  - a named target keeps waiting (its identity and proper motion are essential), so it stays recoverable.
- **How narrowing works.** Degraded runs record `degraded_context` in the job result and data status, use a distinct reuse key (never
  served once context returns), and make single bounded catalogue attempts. Conclusions narrow automatically: no controls means no
  registration and no control-star false-alarm reference, so the claims gate allows at most INCONCLUSIVE.
  Test: `CatalogueOutagePolicyTests`.
- **`heimdall/context/health.py` and CLI `context-status`.** One bounded probe per service, in parallel, classified from evidence.
  Live at 10:27Z, with no proxy configured: Gaia@IRSA HTTP 502, CatWISE@IRSA HTTP 502, Gaia@ESA timeout (40 s), JPL reachable. The
  long Gaia wait is therefore a real upstream outage at both providers (`context_status_live.txt`).
- **Live.** Job `job:target_investigation:c03487f9482a` (RA 150.1, Dec 2.2) has 9 catalogue failures, so its next attempt (10:40:57Z,
  worker pid 7250) takes the narrowed path. It then needs an index of its field, which waits for IRSA discovery (SIA2/TAP 502 at 10:24Z).
- Tests: full suite 552 OK (5 live skipped).

### R2-M5 — portable report and worker safety (this commit)

- **`heimdall/report.py`** (`heimdall.report/1.0`): JSON plus Markdown with data (SHA-256 per product, manifest), methods and versions,
  validation statement, claims with gates, patterns, hypotheses, limitations, versions and reproduction. A SUPPORTED hypothesis is
  qualified by its unexcluded alternatives. Served by API `GET /targets/{id}/report` and CLI `target report X --write F.md`. Real report:
  `.heimdall/verification/2026-11-15-submission/report_NASA-02.{md,json}`.
- **Stale-worker fencing:** the attempt counter is the token; a re-claimed job refuses the late worker's publication (`StaleClaim`).
- **Single-worker lock:** a second worker on the real state was refused with a clear message.
- **Bounded queue:** `QUEUE_FULL` (429), while identical requests still join.
- Tests: full suite 562 OK (5 live skipped); new `WorkerSafetyTests` (3) and `ReportApiTests`.
- Worker 10 (pid 7948) runs this code.
- **Moving-object recovery (spec §7): investigated, not changed.** The saved 100-body injection runs (56/100 and 59/100 recovered; 30 and
  27 bodies with no track) keep aggregate per-body outcomes, not per-stage counters. Any linker change also needs a fresh 100-experiment
  validation (about 2–3 h per run here) to show recall does not rise at the cost of false links, which does not fit today. Next action:
  add per-body stage counters (detections near truth per epoch → associated → tracklet → linked) to `validation.moving_object_*`, rerun
  `python3 -m heimdall validate <run>` on the cached QR2 field with seed 20261029, attribute the losses, then change the responsible
  stage with positive and negative regression cases.

### R2-M6 — live verification under the IRSA/Gaia outage and the fixes it exposed (this commit)

- **Alternate analysis with a material input change (real).** `POST /runs` for the 150.1/2.2 field with `max_observations` 6:
  - acknowledged in 1.3 ms (202);
  - worker 10 acquired 4 new genuine DR1 products through the product route while discovery was down, and completed in 60 s:
    run `R20261115T103457-def358a0`, manifest `man:3845400908334a59`;
  - `GET /compare` against the 4-observation run: `COMPARABLE_WITH_LIMITATIONS` (2 shared observations, 6 different, parameter
    `max_observations` 4 → 6);
  - detections 103 → 162, sources 33 → 35;
  - motion candidates 3 → 1, interpretations {EXPLAINED 1, INCONCLUSIVE 2} → {EXPLAINED 1}.

  The two inconclusive motion candidates of the 4-observation run did not recur with more data. That is recorded as non-recurrence,
  not as proof they were spurious. Evidence: `alternate_run.txt`.
- **Fix: run comparison read a non-existent field.** It used `attention_level`; it now reports candidate categories and the
  investigation interpretation counts. Test: `RunComparisonTests`.
- **Catalogue-outage policy, live.** Job `job:target_investigation:c03487f9482a`, past its grace, took the narrowed path at 10:42:00Z.
  It stopped waiting on the catalogue, used a coordinate-only reference, and moved to its INDEX child `job:index:77e5ad7b5665`, which
  waits on IRSA discovery (still 502).
- **Fix: polling held the worker.** Each parent poll re-tried Gaia (about 60 s of timeouts). There is now a per-job negative cache: after
  a degraded attempt finds the catalogue down, catalogue calls are skipped for `CATALOGUE_SKIP_S` (900 s). Verified live after the
  restart: the 10:46:46Z poll took under a second. Test: `CatalogueSkipTests`.
- **Capabilities.** Per-detector implementation evidence (indexed observations and stored genuine products per detector), kept separate
  from scientific validation (NOT_VALIDATED for all six).
- Tests: full suite 565 OK (5 live skipped).
- Worker 11 (pid 8756) runs this code.

## Integration record (D-007; approved by the owner on 2026-11-15)

The full individual commit history is kept; nothing is squashed. Steps and verification:

1. Local-only backup: `backup/submission-2026-11-15-pre-cleanup` at `86f6caa` (not pushed to `origin`). It is kept until the owner
   confirms the GitHub `main` history.
2. Co-author trailer lines were removed from the development-branch commit messages with
   `git filter-branch --msg-filter` over `462c8b4..HEAD` (23 commits). Verification:
   - every commit's tree hash, author name/email/date and committer name/email/date are identical to the backup's;
   - every message is identical apart from the removed trailer lines, and none remain;
   - the final tree equals the backup's;
   - the base is unchanged (`462c8b4`);
   - full suite 524 OK (5 live skipped).
3. `git push --force-with-lease=backend/submission-2026-11-15:86f6caa origin backend/submission-2026-11-15` updated the remote from
   `86f6caa` to `1264fcb` (lease held: nobody else had pushed).
4. This ledger update is the final milestone commit on the branch. Then:
   - `git merge --ff-only` into `main`;
   - full suite;
   - `git push origin main`;
   - verify `main`'s individual history and metadata;
   - delete the remote feature branch.

Commit hashes cited earlier in this ledger and in `docs/DECISIONS.md` are the pre-cleanup hashes. Each maps one-to-one, with identical
content, to the published hashes:

| Pre-cleanup | Published |
|---|---|
| `c4a4751` | `93555bf` |
| `91ee79d` | `bf9ba87` |
| `05940e6` | `82bcf72` |
| `1c0e47d` | `beca17d` |
| `b37b785` | `a0c17fd` |
| `51ff231` | `17e41a0` |
| `441bb64` | `8ae26bb` |
| `93f8b85` | `71b1396` |
| `02d0a4f` | `b4f5984` |
| `28db40c` | `45a44bf` |
| `a808f33` | `7b235a3` |
| `6fd9fa5` | `7d80d7c` |
| `2850606` | `4c0c52d` |
| `80ddc0a` | `20db4a8` |
| `23457c8` | `54a5072` |
| `cbb381c` | `0c0e754` |
| `4096b6f` | `4ac93e6` |
| `e5e286b` | `631db4f` |
| `ee13af3` | `8d2c0a5` |
| `bda5b7d` | `ad83ac8` |
| `dbf9863` | `7cb6e8b` |
| `38a2389` | `357f8f1` |
| `86f6caa` | `1264fcb` |

## Deferred (explicitly out of today's scope; see `docs/BACKEND_MASTER_SPEC.md` for entry points)

All-sky metadata crawl and mirror (spec §8–9), new ML models (§16), per-detector fidelity campaign (§11), first-class comparison records
for motion pairs and PSF/background matching (§12), new detection families and pattern graph (§13–14), automated follow-up loop (§15), benchmark campaign (§21), PostgreSQL migration.

## Active jobs / workers

- Worker pid 98970 (`python3 -m heimdall data worker --max-idle 10`), started 09:17Z on the final code (tree identical to `1264fcb`). Stop it gracefully with
  `kill -TERM 98970`. Open jobs survive a stop and continue when any worker starts.
- Open job `job:target_investigation:c03487f9482a` (coordinate RA 150.1, Dec 2.2) is `PREPARING_DATA` / `CATALOGUE_PENDING`: Gaia is
  unreachable at IRSA and ESA. Its deadline is 13:53:30Z; after that it is re-checked every 6 h for 7 days.
- Inspect read-only with `python3 -m heimdall data jobs` and `python3 -m heimdall data status`.
