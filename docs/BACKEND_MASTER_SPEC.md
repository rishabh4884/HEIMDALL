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
| S-06 | Readiness on every ordinary workflow | implemented (with stated limits) | targets (named + coordinate) via `targets/prepare.py`; `Engine.coverage`/CLI `coverage` and `Engine.investigate`/CLI `investigate` (blind field pipeline) prefetch, freeze a manifest and run only on verified local data, or return a durable job; HTTP API readiness-only; `--live` is the explicit operator diagnostic; `reproduce`/`validate` keep the original access path for hash comparability | cancellation propagates only to the child a parent currently waits on; the CLI runs an explicit blind investigation inline once data are local (the HTTP API never computes inline) |
| S-07 | Durable jobs, automatic recovery | implemented | REQ-027; recovery policy + `data worker` (D-002, D-004) | multi-worker fencing beyond leases, Retry-After parsing, disk-full drills: deferred |
| S-08 | All-sky metadata inventory | implemented (mechanism); population: real pilot only | `dataservice/allsky.py`: partitions, MAXREC truncation detection with subdivision, failed-partition retry, refresh, accounting; schema v2 | not populated beyond the pilot; the whole sky at 2° is 10,524 partitions × 4 collections of SIA2 queries, needing a sustained worker run while IRSA discovery is up |
| S-09 | Mirror capability | partial | `mirror_plan` (measured sizes, budget, pins); prefetch mode for execution | no full-frame size measured yet, so execution is refused by design; storage for a full mirror not provisioned |
| S-10 | Integrity and reproducibility | implemented | content-addressed store, SHA-256 + FITS checks, frozen manifests, `latest_attempt.json` vs `latest.json` | manifest-read enforcement during analysis is by construction (`LocalStoreSource`), not audited per read |
| S-11 | SPHEREx fidelity, all six detectors | partial | `docs/SPHEREX_FIDELITY.md` | per-detector validation campaign deferred |
| S-12 | First-class comparability record | partial | per-series record `COMPARABLE / COMPARABLE_WITH_LIMITATIONS / NOT_COMPARABLE` with sampling, passband, processing versions, registration, PSF, noise model, blending, pixel flags, saturation (`target_variability` 1.2.0); `NOT_COMPARABLE` series cannot produce change significance; epoch comparisons carry a status | motion per-image pairs still use exclusion reasons; no PSF matching or background-treatment record |
| S-13 | Detection families | partial | REQ-007..011 | morphology, deblending, period search deferred |
| S-14 | Multidimensional pattern model | partial | `heimdall/patterns.py` (`/targets/{id}/patterns`): per-dimension records, links, dependency groups, missing dimensions | blind-run cross-dimension patterns and incremental updates not implemented |
| S-15 | Hypothesis-driven follow-up loop | partial | `heimdall/followup.py`: discriminating actions, persisted, never repeated, budgeted, stopping reason; runs as durable jobs | only re-acquisition actions (more epochs, other detectors) are automatic; deblending and larger control samples are listed as unavailable; thermal inference is blocked (REQ-016) |
| S-16 | Catalogue context, ML | implemented (ML evaluated, not adopted) | REQ-013, REQ-017, `docs/ML_DECISION.md` | real labelled sample does not exist |
| S-17 | Statistical validation | partial | synthetic controls (REQ-014) | real-image injection-recovery deferred |
| S-18 | Backend product contracts + transport | implemented (M2) | `docs/FRONTEND_HANDOFF.md`, `docs/OUTPUT_SCHEMA.md`, `docs/UI_BACKEND_REFERENCE.md` | auth integration point only (no identity provider) |
| S-19 | Performance/operations | partial | `data status` metrics, worker heartbeat | measured budgets and clean-install test deferred |
| S-20 | Auditable conclusions | implemented for target investigations | `heimdall.claims/1.0` (REQ-031): claim types, defensibility gate with downgrades, evidence paths; versions via `/targets/{id}/versions` (supersession links) | field-level cross-version diff deferred; blind-run investigations keep their own interpretation contract |
| S-21 | Benchmark / metamorphic tests | deferred | — | entry point: `tests/` with frozen manifests from the store |

Deferred items are not counted as done anywhere. Re-plan from this table after the submission.
