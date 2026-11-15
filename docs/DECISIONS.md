# Consequential decisions

Earlier scientific and provisional decisions remain in `docs/PROVISIONAL.md`, `docs/ML_DECISION.md` and `docs/SCIENCE_METHODS.md`;
this file records decisions from 2026-11-15 onward.

| ID | Date | Decision | Rationale | Consequence |
|---|---|---|---|---|
| D-001 | 2026-11-15 | Submission override: today's scope is a stable, honest, demonstrable core; the broad master spec items are recorded as deferred with entry points, not started | Deadline 17:00 IST; unfinished infrastructure would weaken the demo | `docs/BACKEND_MASTER_SPEC.md` lists every deferred item |
| D-002 | 2026-11-15 | A deadline ends the active attempt, not the request: deadline-expired jobs are re-checked by a service-owned policy (default every 6 h for 7 days, 30-min attempts); `must_resubmit` becomes `true` only after that window or a cancellation | The previous constant `must_resubmit: false` was untrue after a deadline | `job.auto_recovery` in the status contract; no DB migration (window derived from `job_events`) |
| D-003 | 2026-11-15 | The recovery window starts at the first deadline after the last *user* submission; automatic attempts do not extend it | Prevents an unbounded retry loop against a dead archive | Retention is bounded and visible |
| D-004 | 2026-11-15 | The deployment worker is a single process per state directory (`data worker`), supervised by systemd/launchd examples that HEIMDALL does not install | SQLite store is single-node; installing services was not authorized | Multi-worker scaling remains the documented PostgreSQL path |
| D-005 | 2026-11-15 | Frontend transport: a dependency-free stdlib HTTP JSON server over `Engine` rather than a web framework | No new dependency on submission day; the existing contracts are already JSON | See `docs/FRONTEND_HANDOFF.md` |
