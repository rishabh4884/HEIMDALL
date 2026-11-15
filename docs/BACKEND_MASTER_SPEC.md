# Backend master specification: scope, requirement IDs, states, deferrals

Source: the master backend specification and the "submission-ready override" given on 2026-11-15. The override takes precedence for
today: deliver a stable, honest, demonstrable core by 17:00 IST and record everything else precisely. Requirement-level evidence lives in
`docs/REQUIREMENTS_LEDGER.md` (REQ-001..); this file maps the master spec's sections (S-xx) to those requirements and states what is
done, partial, or deferred. States: **verified** (tested + real-data evidence), **implemented** (tested, scientifically unvalidated
or synthetic-only), **partial**, **blocked** (names the missing input), **deferred** (not started; entry point given).

Historical requirements from earlier conversations that are not in the repository could not be re-read; only what is recorded in
`docs/` and the code is used.

| ID | Master-spec section | State today | Evidence / where | Gap or next action |
|---|---|---|---|---|
| S-02 | Execution continuity | implemented | `EXECUTION_LEDGER.md`, `RESUME_BACKEND.md`, `DECISIONS.md` | — |
| S-03 | Incremental commits/pushes | implemented | branch `backend/submission-2026-11-15` | no CI exists in the repository |
| S-06 | Readiness on every ordinary workflow | partial | targets (named + coordinate) via `targets/prepare.py`; `Engine.coverage` / CLI `coverage` answer from the local index or a durable INDEX job (`--live` is the explicit operator diagnostic); HTTP API readiness-only | the blind field pipeline (`Engine.investigate`, CLI `investigate`) still reads the archive adapter directly; entry point: run it on `LocalStoreSource` after `DataService.prefetch`. Cancelling a parent job does not cancel children it already created |
| S-07 | Durable jobs, automatic recovery | implemented | REQ-027; recovery policy + `data worker` (D-002, D-004) | multi-worker fencing beyond leases, Retry-After parsing, disk-full drills: deferred |
| S-08 | All-sky metadata inventory | deferred | per-region inventory exists (`dataservice/inventory.py`) | needs a partitioned crawl (HEALPix tiles over SIA2 with truncation detection) + pilot measurement; entry point `DataService.ensure_index` per tile |
| S-09 | Mirror capability | deferred | prefetch mode exists (`data prefetch`) | mirror planning/capacity estimate needs measured product sizes per collection |
| S-10 | Integrity and reproducibility | implemented | content-addressed store, SHA-256 + FITS checks, frozen manifests, `latest_attempt.json` vs `latest.json` | manifest-read enforcement during analysis is by construction (`LocalStoreSource`), not audited per read |
| S-11 | SPHEREx fidelity, all six detectors | partial | `docs/SPHEREX_FIDELITY.md` | per-detector validation campaign deferred |
| S-12 | First-class comparability record | partial | per-series record `COMPARABLE / COMPARABLE_WITH_LIMITATIONS / NOT_COMPARABLE` with sampling, passband, processing versions, registration, PSF, noise model, blending, pixel flags, saturation (`target_variability` 1.2.0); `NOT_COMPARABLE` series cannot produce change significance; epoch comparisons carry a status | motion per-image pairs still use exclusion reasons; no PSF matching or background-treatment record |
| S-13 | Detection families | partial | REQ-007..011 | morphology, deblending, period search deferred |
| S-14 | Multidimensional pattern model | partial | REQ-012 | deferred |
| S-15 | Hypothesis-driven follow-up loop | partial | REQ-015 (hypotheses + next step text) | automated acquisition loop deferred; thermal inference blocked (REQ-016: needs model grid, passband integration, absolute calibration) |
| S-16 | Catalogue context, ML | implemented (ML evaluated, not adopted) | REQ-013, REQ-017, `docs/ML_DECISION.md` | real labelled sample does not exist |
| S-17 | Statistical validation | partial | synthetic controls (REQ-014) | real-image injection-recovery deferred |
| S-18 | Backend product contracts + transport | implemented (M2) | `docs/FRONTEND_HANDOFF.md`, `docs/OUTPUT_SCHEMA.md`, `docs/UI_BACKEND_REFERENCE.md` | auth integration point only (no identity provider) |
| S-19 | Performance/operations | partial | `data status` metrics, worker heartbeat | measured budgets and clean-install test deferred |
| S-20 | Auditable conclusions | implemented for target investigations | `heimdall.claims/1.0` (REQ-031): claim types, defensibility gate with downgrades, evidence paths; versions via `/targets/{id}/versions` (supersession links) | field-level cross-version diff deferred; blind-run investigations keep their own interpretation contract |
| S-21 | Benchmark / metamorphic tests | deferred | — | entry point: `tests/` with frozen manifests from the store |

Deferred items are not counted as done anywhere. Re-plan from this table after the submission.
