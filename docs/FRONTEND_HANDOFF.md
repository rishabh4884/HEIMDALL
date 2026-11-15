# Frontend handoff (backend contracts as of 2026-11-15)

## Start the backend

```
python3 -m heimdall serve --with-worker            # HTTP JSON API on http://127.0.0.1:8765/api/v1 + an in-process data worker
# or, separately (recommended for long sessions):
python3 -m heimdall serve                          # API only
python3 -m heimdall data worker                    # worker (acquisition, retries, analysis); Ctrl-C stops it gracefully
HEIMDALL_OFFLINE=1 python3 -m heimdall serve       # cached-only demo: nothing touches the network; uncached requests wait as jobs
```

Run exactly one worker per state directory: either `serve --with-worker` or a separate `data worker`, not both (decision D-004).
`scripts/demo_submission.sh` exercises the offline/cached path and the API end to end.

CORS: `http://localhost:<any port>` and `http://127.0.0.1:<any port>` are allowed by default; set `HEIMDALL_API_CORS_ORIGIN` for another
origin. Optional auth hook: set `HEIMDALL_API_TOKEN` and send `Authorization: Bearer <token>`. The server binds to 127.0.0.1 unless `--host`
is given. Implementation: `heimdall/server.py` (stdlib only); tests: `tests/test_server.py`.

## Endpoints (`/api/v1`, contract `heimdall.http/1.0`)

| Method + path | Returns | Notes |
|---|---|---|
| `GET /health` | versions, `offline`, `worker {running, heartbeat_age_s, heartbeat}` | show a banner when no worker is running: jobs will not progress |
| `GET /status` | `metrics` (queue, routes, cache, bytes, worker) + `freshness` | operator panel; `freshness.all_sky_complete` is always `false` |
| `GET /targets` | five NASA targets + saved-result summary | |
| `GET /targets/{id}` | latest **usable** result (`heimdall.target_investigation/1.0`) | 404 if never completed |
| `GET /targets/{id}/attempt` | latest attempt (can be a blocked attempt that did not replace the usable result) | 404 if none |
| `GET /targets/{id}/claims` (also `/investigations/{tinv}/claims`) | `heimdall.claims/1.0`: `quick_answer`, `headline_claim`, `claims[]` with `claim_type` (MEASUREMENT, DETECTION, NON_DETECTION, UPPER_LIMIT, CONSISTENT_WITH, INCONCLUSIVE, NOT_ASSESSABLE, BLOCKED, LIMITED_BY_DATA), `values[]` with units and uncertainties, `method`, `evidence_refs` (JSON paths), `observations_used`, `does_not_establish`, `gate.checks[]` (8 defensibility checks), `downgraded_from` | the Quick Answer -> Evidence -> Deeper Dive path; show `claim_type` verbatim |
| `GET /targets/{id}/versions` | every saved version, newest first: `status`, `supersedes`, `superseded_by`, `supersession_reason`, latest flags | use to show why a conclusion changed |
| `GET /investigations/{tinv:...}` | one immutable saved version | older versions stay readable |
| `GET /coverage?ra&dec&radius[&detectors][&offset&limit]` | `heimdall.data_status/1.0` + `observations[]` with footprints from the local index | never runs an analysis; the cone is snapped to a 0.01° grid (`queried_cone`, always containing the requested cone); unindexed -> `PREPARING` + job, `observations: null` |
| `POST /investigations` | body `{"target_id":"NASA-01"}` or `{"ra":..,"dec":..,"category":"MOVING_OBJECT"|"VARIABLE_STAR"}`, optional `options` (`max_observations` 1–64, `detectors`, `mjd_start`, `mjd_end`, `cutout_size_deg` 0.01–0.5, `blind_pipeline`, `context`) | **200** completed result (a stored identical result) or **202** data status with `job.job_id` |
| `GET /jobs/{job_id}` | target jobs: status (+`events[]`) while preparing; the full result once complete | reconnect at any time; never resubmit |
| `GET /jobs?state=&kind=&limit=` | job list | |
| `POST /jobs/{job_id}/cancel` | status after cancellation | |
| `GET /manifests/{man:...}` | frozen input manifest (products, SHA-256, release, route, retrieval time, index sync) | |

Errors: `{"schema":"heimdall.http/1.0","error":{"code","message"}}`, codes `INPUT_INVALID` (400), `UNAUTHORIZED` (401), `NOT_FOUND` (404),
`PAYLOAD_TOO_LARGE` (413), `INTERNAL` (500, no stack trace to the client). Requests never return an archive error: an outage shows up
as `PREPARING` with `job.next_attempt_at`.

## Submission flow (poll; there is no push channel)

1. `POST /investigations` -> 202 `{readiness: "PREPARING", job: {job_id, state, next_attempt_at, ...}, must_resubmit: false, status: {execution: "PREPARING_DATA"}}`.
2. Poll `GET /jobs/{job_id}` every 2–5 s. While preparing: `job.state` (`QUEUED`, `DISCOVERING`, `DOWNLOADING`, `VALIDATING`, `PREPARING_DATA`),
   `job.progress`, `job.reason`, `job.last_error` (technical detail; do not show raw), `events[]` (history).
3. Done: the response *is* the result (has `investigation_id`) plus `data_status`. Terminal non-results: `readiness` = `NO_COVERAGE`
   (archive searched successfully, nothing there), `UNAVAILABLE_AFTER_DEADLINE` (archive outage; **not** a scientific result; see
   `job.auto_recovery`), `INVALID_SOURCE_DATA`, `CANCELLED`. Show `must_resubmit` literally: it is `true` only when the service has stopped.

## Result fields to display (`heimdall.target_investigation/1.0`; full schema `docs/OUTPUT_SCHEMA.md`)

| UI need | Field |
|---|---|
| Quick answer | `conclusion.quick_answer`, `conclusion.test_status`, `conclusion.test_status_reason` |
| Execution vs science | `status.execution` (software: `COMPLETED`/`PARTIAL`/`BLOCKED`/...) is **not** `status.test_status` (scientific: `PASSED`/`LIMITED_BY_DATA`/...) |
| Hypotheses | `hypotheses[]`: `title`, `status`, `summary`, `supporting_evidence[]`, `contradicting_evidence[]`, `tests_run[]`, `tests_unavailable[]`, `assumptions[]`, `limitations[]`, `suggested_next_step` |
| Evidence links | `evidence[]` (ids referenced by hypotheses) -> `measurements[]` (per image: `obs_id`, `mjd`, `wavelength_um`, `forced.flux_ujy` + `forced.flux_sigma_ujy` + `forced.snr`, `centroid`, `flags`, `status`, `reasons`) -> `products[]` (`sha256`, `release`, `retrieved_at`, `access_reference`) |
| Motion / light curve | `analyses.motion` (fitted vs catalogue motion with uncertainties, `verdict`), `analyses.variability` (`verdict`, `verdict_reason`, `light_curve`, `series`, `upper_limits`, `caveats`) |
| Limits | `warnings[]`, `failed_items[]`, `detectability`, `analyses.variability.caveats` |
| Reproducibility | `input_manifest` (`manifest_id`, products with SHA-256), `config_hash`, `methods_version`, `software_version`, `reproduce`, `results_hash` |
| Data origin | `data_origin` (`REAL`/`SYNTHETIC`) — synthetic must be watermarked; products' `origin` (`CACHED` once stored; original route in the manifest) |

Vocabulary rules: `NOT_ASSESSABLE`/`LIMITED_BY_DATA` are not "nothing found"; a forced flux is a measurement, not a detection;
`UNAVAILABLE_AFTER_DEADLINE` is an archive state, never a non-detection; Planet X is never a probability. More: `docs/UI_BACKEND_REFERENCE.md`.

## Known limits of the transport (deliberate for the submission)

* Polling only (no WebSocket/SSE). Progress granularity is per job state/event, not percent.
* `GET /coverage` returns only regions already indexed; a new region queues an INDEX job (returns `PREPARING`).
* Blind field runs are not exposed over HTTP yet. On the CLI/Python they are readiness-first (prefetch -> frozen manifest -> local run, or a
  durable job); `--live` is an operator diagnostic.
* Single-node SQLite state; one worker per state directory.
