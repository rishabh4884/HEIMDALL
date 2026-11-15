# SPHEREx data service (readiness-first access, durable acquisition)

Code: `heimdall/dataservice/` (+ `heimdall/targets/prepare.py`). CLI: `python3 -m heimdall data ...`. API: `Engine.data_service()`,
`Engine.investigate_target(...)`, `Engine.investigation_status(job_id)`.

## The guarantee, and its limit

A user submits an investigation **once**. HEIMDALL, not the user, owns acquisition, retries, provenance and the eventual terminal status.
The request either (a) runs immediately from verified local data, (b) is accepted and prepared asynchronously while the service keeps
retrying the archive, or (c) ends in a durable, precise terminal state. An archive outage does not propagate as a user-facing error and
does not require resubmission while the service still owns the request (see "Deadline and automatic recovery"). `must_resubmit` is `true`
only after a cancellation, or after the automatic-recovery window of a deadline-expired request has ended.

**What this does not do:** it does not eliminate the external outage. Data that were never ingested cannot be produced while *every*
upstream route is down; such a request stays `PREPARING_DATA` (with its next scheduled attempt visible) until the archive recovers or the
configured deadline gives `UNAVAILABLE_AFTER_DEADLINE`. Only regions/products already in the local store are served with an archive
outage in progress. Nothing is claimed about all-sky availability: the inventory covers only regions that have been indexed.

## Architecture and responsibilities

| Component | File | Responsibility |
|---|---|---|
| Persistence | `db.py` | One SQLite file (WAL): inventory, syncs, product index, pins, manifests, jobs, job events, route events, counters. |
| Inventory | `inventory.py` | Observation records (id, release/collection, detector, footprint polygon, time/spectral bounds, route, access reference, archive checksum, product version, source version, origin, supersession) and **syncs** (completed archive discoveries). Region queries are answered locally from a covering sync; no covering sync means "not indexed", never "no observations". |
| Product store | `productstore.py` | Content-addressed (`objects/<sha[:2]>/<sha256>.fits`), atomic, verified before visibility (SHA-256, FITS-openable with every HDU read, optional archive MD5 match). Deduplicates identical bytes, quarantines rejects, pins objects used by manifests, reports quota. |
| Jobs | `jobs.py` | Durable idempotent jobs with the state machine below, leases, persisted history, exponential backoff with jitter. |
| Service | `service.py` | Readiness checks (no network), `ensure_index`, `ensure_products`, `prefetch`, `sync`, `tick`/`work` (the worker), manifests, metrics, freshness. |
| Local source | `localsource.py` | A `DataSource` that serves only inventory-indexed observations and verified store objects. The scientific engine sees this, not IRSA. |
| Target layer | `targets/prepare.py` | Readiness-first investigation: resolve -> index -> plan -> acquire -> freeze manifest -> analyse offline. Registers the `TARGET_INVESTIGATION` job handler. |
| IRSA adapter | `data_access/irsa_spherex.py` | Route logic (SIA2 first for DR1/QR3, bounded TAP fallback), per-route timeouts, jittered backoff, circuit breakers, cached discovery. Only workers use it. |

Request path (no archive access): `readiness` -> inventory + store. Worker path (the only code that touches IRSA): discovery, download,
validation, registration. The scientific engine runs from a **frozen, content-hashed input manifest** (`heimdall.input_manifest/1.0`):
every result carries `input_manifest` with each product's SHA-256, release, route, access reference, retrieval time and the index sync used.

## State machine

```
QUEUED -> DISCOVERING -> DOWNLOADING <-> VALIDATING -> COMPLETE
   \            \             \
    \            +--------------> PREPARING_DATA  (upstream failed; next attempt scheduled; back to DISCOVERING/DOWNLOADING when due)
     +--> NO_COVERAGE (a *successful* discovery found nothing)       PREPARING_DATA --(deadline)--> UNAVAILABLE_AFTER_DEADLINE
     +--> INVALID_SOURCE_DATA (retrieved data failed validation)     any non-terminal --> CANCELLED
     +--> FAILED (software/request error: request errors at once, unexpected errors after 3 attempts; never reported as an outage)
```

* `COMPLETE`: all required verified local data exist (a *readiness* of `READY`). `NO_COVERAGE`, `UNAVAILABLE_AFTER_DEADLINE`,
  `INVALID_SOURCE_DATA`, `CANCELLED`, `FAILED` are terminal; illegal transitions raise. A route failure is **never** `NO_COVERAGE`.
* A terminal `UNAVAILABLE_AFTER_DEADLINE`/`CANCELLED` job is revived (generation + 1, history kept) only by a *new* submission; a `COMPLETE`
  job is reopened only if its data are no longer present locally.
* Composite jobs: `PREFETCH` and `TARGET_INVESTIGATION` own child `INDEX`/`ACQUIRE` jobs, wait on them (`PREPARING_DATA` with the child's next
  attempt time) and finish with the same terminal vocabulary.
* Idempotency: the job id is a hash of (kind, canonical payload), so the same request returns the same active or completed job; a duplicate
  never starts a second download. Content already in the store is not downloaded again.
* Crash recovery: running states hold a lease; `resume()` (called by `work`) returns expired-lease jobs to `QUEUED`. Per-product progress is
  persisted, so finished downloads are not repeated.
* Retries: exponential backoff `base * 2^(n-1)` capped, plus up to 25 % jitter (`HEIMDALL_DS_BACKOFF_*`), never earlier than an open circuit
  breaker's remaining cooldown; route switching and breakers live in the adapter; the finite deadline (`HEIMDALL_DS_DEADLINE_S`, default 6 h)
  bounds everything. A user request never runs an unbounded network loop; `--wait` runs the worker inline for at most the stated seconds.

## Deadline and automatic recovery (resolves the old `must_resubmit` contradiction)

Before 2026-11-15 (afternoon) a request that reached its deadline could only be restarted by a new submission, while the status still said
`must_resubmit: false`. Now the deadline ends the *active attempt* and publishes `UNAVAILABLE_AFTER_DEADLINE` honestly, and the request is
retained under a service-owned recovery policy (`JobQueue.recover_due`, run by every worker tick):

* every `HEIMDALL_DS_RECOVERY_INTERVAL_S` (default 6 h) the job is revived (generation + 1, event `automatic recovery check (service-owned
  policy; no user request)`) with a short deadline `HEIMDALL_DS_RECOVERY_ATTEMPT_S` (default 30 min);
* this continues for `HEIMDALL_DS_RECOVERY_WINDOW_S` (default 7 days) after the *first* deadline since the last user submission; failed
  recovery attempts do not extend the window; `0` disables recovery;
* `job.auto_recovery` reports `{active, next_check_at, until, policy}`; `must_resubmit` is `false` while `active`, `true` afterwards;
* a new submission of the same request still revives it at once (generation + 1) and starts a fresh window.
* a parent job that needs a deadline-expired child again revives it with the short attempt deadline and does **not** reset its window;
  map coverage requests (`DataService.coverage`) only report the state and never revive it;
* `cancel` on a deadline-expired job moves it to `CANCELLED` and stops automatic recovery (`must_resubmit: true`).
* Cancelling a parent (target/prefetch/field) job also cancels the child it was waiting on, unless another open job waits on the same
  child (shared work is never cancelled). Limit: only the child the parent currently waits on is tracked.
* A product request the archive rejects twice with a permanent 4xx (not 408/425/429), e.g. HTTP 422 for a cutout outside the image, is
  excluded (`kind: ARCHIVE_REJECTED`, listed with the quarantined products in the manifest) instead of being retried into the deadline.

## Long-running worker (deployment mode)

`python3 -m heimdall data worker [--max-idle 30]` runs until SIGTERM/SIGINT: it resumes interrupted (lease-expired) jobs, runs due jobs and
recovery checks, writes an atomic heartbeat (`<state>/dataservice/worker_heartbeat.json`: pid, time, ticks, jobs run, open jobs), sleeps until
the next scheduled attempt (at most `--max-idle` s), and on a signal finishes the current bounded step and exits with a final `stopped: true`
heartbeat. A hard kill is recovered by lease expiry (`HEIMDALL_DS_LEASE_S`) and `resume()` on the next start. `data status` reports
`metrics.worker` (running, heartbeat age). Supervision examples (not installed by HEIMDALL): `deploy/heimdall-worker.service` (systemd)
and `deploy/com.heimdall.worker.plist` (launchd). One worker per state directory: the SQLite store is single-node (see Storage).

## User-facing readiness contract (`heimdall.data_status/1.0`)

`readiness` is one of `READY`, `PREPARING`, `NO_COVERAGE`, `UNAVAILABLE_AFTER_DEADLINE`, `INVALID_SOURCE_DATA`, `CANCELLED`, `FAILED`, with a user-safe
`message`, the `job` (state, attempts, next attempt time and seconds, last error, progress, `auto_recovery`), `must_resubmit` (see above), and for target
investigations a `status.execution` compatible with the existing result schema (`PREPARING_DATA` while preparing). Structured technical
detail (HTTP status, route, error class) stays in `job.last_error`, the job `events`, and `data status`. A completed investigation is the
normal `heimdall.target_investigation/1.0` result plus `data_status` and `input_manifest`.

* All data local: the analysis runs offline (zero archive calls) or a stored identical result (same target, options, config, manifest) is returned.
* Otherwise: a durable job id is returned immediately.
* Offline process (`HEIMDALL_OFFLINE=1`): same contract; the job waits `QUEUED` for a worker with archive access.
* A stale index (older than `HEIMDALL_DS_INDEX_MAX_AGE_S`, default 7 days) still answers immediately and queues a background refresh job.
* Catalogue references (Gaia/CatWISE) and the Gaia field-star query are cached by their own services; an uncached one makes the request
  wait for a worker (it is never silently skipped). A worker makes one bounded catalogue attempt per job run (no inner HTTP retries,
  30 s timeout); on failure the job is `PREPARING_DATA` with `last_error.code = CATALOGUE_PENDING` and the job's backoff owns the retries. JPL/other context is read from cache offline and recorded as unavailable if absent.

## Freshness and completeness

`data freshness [--ra --dec --radius]` reports, per indexed region: collections indexed, last successful refresh, newest observation time
represented, route, whether the discovery came from cache, staleness, and whether a refresh is delayed by upstream failure. It states that
the data are a local inventory (cached archive answers), not a live query, that `all_sky_complete` is false, and that completeness holds only for
the listed regions as of their refresh times (IRSA's SIA2 availability itself lags new data by about a day). Data are never labelled live once stored:
provenance origin is `CACHED`, with the original acquisition origin/route in the notes and the manifest.

## Operator workflow

```
python3 -m heimdall data prefetch --ra R --dec D --radius 0.05 [--detectors 2] --cutout-size 0.1 --max-observations 16 [--wait 300]   # explicit ingestion
python3 -m heimdall target run NASA-01 [--wait 60]          # user request: returns the result, or a durable job id (never "retry")
python3 -m heimdall data worker                            # long-running worker (supervise with systemd/launchd); SIGTERM stops it gracefully
python3 -m heimdall data work --max-seconds 1700           # bounded worker run: resume interrupted jobs, run due jobs/retries until idle or budget
python3 -m heimdall data jobs [--state PREPARING_DATA]     # queue; `data job JOB_ID` shows state, attempts, next attempt, events
python3 -m heimdall data sync [--max-age-s S]              # queue refresh jobs for indexed regions older than S (then run `data work`)
python3 -m heimdall data status                            # metrics + freshness: queue depth/age, retries, route health, cache hit rate, bytes, validation failures
python3 -m heimdall data freshness --ra R --dec D --radius X
python3 -m heimdall data manifest man:...                  # the exact inputs of an analysis
python3 -m heimdall data cancel JOB_ID | resume | evict [--target-bytes N] [--dry-run]
```

Modes: **on-demand** (a user request creates jobs), **prefetch** (`data prefetch`), **sync** (`data sync` + `data work`), **offline**
(`HEIMDALL_OFFLINE=1`: everything from the local inventory/store; nothing touches the network). `worker` is the deployment mode;
`work` is the bounded, schedulable variant (`DataService.work`/`tick`). Recovery after an outage needs no operator action beyond
having a worker running; `data jobs --state PREPARING_DATA` shows what is waiting and when it will next be tried.

## Storage, retention, quota, scaling

* Local layout: `<state_dir>/dataservice/service.sqlite3` and `store/{objects,quarantine,tmp}`. The discovery-response cache and the adapter's
  download cache (`<cache_dir>`) remain separate and may be cleared; the verified store is the durable copy.
* Pinning: producing a manifest pins every product and calibration it lists; pinned objects are never evicted. `data evict` removes only unpinned
  objects (least recently validated first) down to `--target-bytes`/`HEIMDALL_DS_QUOTA_BYTES`; a quota is **reported** (`over_quota`), never enforced
  by silent eviction. Saved investigations therefore keep their inputs.
* Integrity: size is re-checked on every read (a modified/truncated file is not served); `verify` re-hashes. Corrupt, truncated, non-FITS or
  checksum-mismatching downloads are quarantined and retried up to `corrupt_retries` times, then excluded (listed in the manifest) or, if nothing
  usable remains, the job ends `INVALID_SOURCE_DATA`.
* Scale: the store is cutout-based (a 0.05-0.3 degree cutout is ~4-10 MB per exposure with its extensions); the SQLite design is for one
  machine/one worker group. Migration path: replace `ServiceDB` with PostgreSQL/PostGIS (footprints as geography, `s_ra/s_dec` + polygon text as a
  spatial index; `syncs.members` as a join table) and `objects/` with an object store keyed by SHA-256 (S3/GCS); `Inventory`, `ProductStore`
  and `JobQueue` are the only classes that issue SQL. Multiple workers need `SELECT ... FOR UPDATE SKIP LOCKED` job claims (the lease columns are in place).
  Mirroring the whole survey is a separate capacity decision; nothing here assumes it.

## Scientific provenance and reproducibility

Every stored product records release, access reference, route, archive checksum (if any), locally computed SHA-256 and retrieval time;
the manifest id is a hash of its content, so identical inputs give an identical id. QR2 is not searchable and is never indexed as current data
(legacy-release rows are rejected from the current inventory); DR1 supersedes QR3 for the same exposure and the superseded row is kept and marked,
never silently mixed. Synthetic fixtures use a separate `synthetic` scope and keep their synthetic origin end to end. Stored results are
provisional pipeline measurements; the data service adds no scientific validation.

## Partitioned all-sky inventory and mirror planning (2026-11-15)

`heimdall/dataservice/allsky.py`; CLI `inventory plan|step|status|mirror-plan`; API `GET /api/v1/inventory/partitions`.

**Discovery and truncation.** SIA2 discovery sends an explicit `MAXREC` (`HEIMDALL_DS_DISCOVERY_MAXREC`, default 20000) and records the
row count per collection.
- An answer reaching the limit is `truncated`. Its observations are registered, but the sync never certifies coverage
  (`syncs.truncated`, schema v2, an additive migration from v1).
- On the TAP fallback truncation cannot be checked; such a partition is `SEARCHED_UNVERIFIED`.

**Partitioning.** The sky is tiled into declination bands of near-equal-area RA cells, plus polar caps. Each tile is searched with one
circumscribing cone (tile size 0.25–5°) as a durable INDEX job: the same retries, breakers, deadlines and recovery as everywhere else.
- `step` reconciles finished jobs, marks stale partitions `NEEDS_REFRESH`, and submits a bounded number of new jobs (`max_new`,
  `max_in_flight`).
- Truncated tiles are subdivided into four children, up to level 6.
- Failed tiles are `FAILED` and retried; an outage never becomes `SEARCHED_EMPTY`.

**Accounting.** Certified area is tracked separately from partitions holding downloaded products and from partitions whose products
were used by a frozen analysis. `all_sky_complete` is true only when every leaf of a whole-sky domain is certified.
- Benchmark: a whole-sky 2° plan has 10,524 partitions, planned in 0.05 s, with accounting in 0.02 s on this machine.

**Mirror planning.** `mirror_plan` covers the products indexed in certified partitions (deduplicated across overlapping cones), the
calibration dependencies, and bytes estimated only from *measured* stored full-frame sizes.
- It checks disk free space, the reserve and the quota, and protects pinned evidence.
- Without a measured full-frame sample the estimate is `UNMEASURED` and execution is refused.
- Measured so far: science cutouts 4.0 MB each (n=32), ePSF 7.9 MB; no full frame has been measured yet.
