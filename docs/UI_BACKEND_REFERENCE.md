# Backend interface reference for the UI

Everything the UI needs is already computed. The UI must **display** these structures and must not recompute significances, statuses, hypotheses,
positions or uncertainties. Field-level detail: `docs/OUTPUT_SCHEMA.md`. Method limits: `docs/SCIENCE_METHODS.md`, `docs/PROVISIONAL.md`.

HTTP transport and the readiness-first submission flow (target investigations): `docs/FRONTEND_HANDOFF.md`.

## Entry points (Python: `heimdall.api.Engine`)

| Call | Returns |
|---|---|
| `Engine.from_environment()` | engine using `.env` and `HEIMDALL_*` variables |
| `investigate(InvestigationRequest, progress=cb, save=True)` -> `(result, run_dir)` | runs the whole pipeline (long for real data; `progress(stage, message)` is called per stage) |
| `runs()` | list of saved runs |
| `load(ref)` | full `investigation.json` (`ref` = run id or directory) |
| `inspect(ref, section, item_id=None)` | one section; `section="investigations"` gives summaries, or one full investigation by `investigation_id`, `subject id` or `candidate_id` |
| `skymap(ref, viewport=(ra, dec, fov_deg))` | `heimdall.skymap/1.0`; with a viewport, detections are items or tile counts |
| `playback(ref, mjd_start, mjd_end)` | time-ordered real frames; excluded frames listed with reasons |
| `reinvestigate(ref, out=None)` | investigations rebuilt offline from saved results (no detection, no download) |
| `export(ref, what, fmt)` | CSV/JSON tables |
| CLI | `python3 -m heimdall investigate|runs|inspect|reinvestigate|skymap|playback|export|reproduce|validate|demo` (`--json` on most) |

Run directory (`<state_dir>/runs/<run_id>/`): `investigation.json`, `skymap.json`, `skymap.svg`, `events.jsonl`, `manifest.json`. Poll `events.jsonl`
for live progress. `data_origin` is `REAL` or `SYNTHETIC`; when SYNTHETIC the sky-map has `watermark: "SYNTHETIC - NOT REAL DATA"` and the UI must show it.

## What to draw from where

| UI element | Source | Notes |
|---|---|---|
| Sky background, footprints | `skymap.layers.coverage[].polygon {ra[], dec[]}`, `coverage_depth` | real detector footprints; `fraction_uncovered` is real |
| Detections, sources | `layers.detections[]`, `layers.sources[]` with `ellipse_1sigma {a_arcsec, b_arcsec, pa_deg}` | PA east of north; east left, north up |
| Candidates | `layers.candidates[]` | position, uncertainty, `attention_level`, `validity`, `interpretation_status`, `investigation_id`, `evidence_refs`, `provenance_refs` |
| Candidate detail | `inspect(run, "investigations", investigation_id)` | evidence, dependency groups, hypotheses with predictions, missing evidence, follow-ups, Planet X status |
| Candidate links/tracks | `layers.associations[]`, `layers.tracks[]` | tracks are candidate linkages, not orbits; the field is 0 tracks on the saved real run |
| Known stars / Solar System bodies | `external_layers.star_catalogue`, `.solar_system_ephemerides` | real query results only; `status` says AVAILABLE / PARTIAL / NOT_AVAILABLE with `reason` |
| Time slider / playback | `playback.frames[]` (MJD ascending, no interpolation), `layers.time` | each frame has a footprint and detections |
| Spectra | `sources[].spectrum.points[]` (`wavelength_um`, `bandwidth_um`, `flux_ujy`, `flux_sigma_ujy`, `wavelength_source`, `detector`, `quality_grade`), `.coverage[]` | descriptive only; draw gaps (`coverage` states) as gaps; never join points across detectors without saying so |
| Light curves | `sources[].series[]`, `.variability[]` | series are per comparable wavelength |
| Validity / limits panel | `candidate.cell_validity`, `validation.runs[]`, `unavailable_analyses[]`, `investigation.upstream` | |

## Status vocabularies (never mix them)

Computation (`status.execution`, `stages[].status`) is not scientific. Attention level is a review priority. Validity is the trust of the statistic's scope.
`interpretation.status` is the scientific state of a subject; `hypotheses[].status` is per hypothesis; `CONSISTENT` means "not contradicted", never "supported".
Planet X is a status (`CONSISTENT_REQUIRES_CONFIRMATION`, `INCONSISTENT`, `UNCONSTRAINED`, `NOT_ASSESSABLE`, `NOT_APPLICABLE`), never a probability or a location.
Table in `docs/OUTPUT_SCHEMA.md` ("Which status means what"). Show `NOT_ASSESSABLE`/`INCONCLUSIVE` as such; do not render them as "none found".

## Empty, partial, unavailable, failed

`status.outcome`: `NO_OBSERVATIONS` (query succeeded, nothing there), `PARTIAL` (see `failed_items[]`), `INSUFFICIENT_OBSERVATIONS`. A stage with
`NOT_APPLICABLE`/`INSUFFICIENT_INPUTS` carries `reasons[]`. A failed investigation stage yields `investigations.status == "FAILED"` with `reason`
and leaves all other sections intact. Unavailable data is `null` + `reason`, an `unavailable_analyses[]` entry, or `NOT_AVAILABLE` in `external_layers`.

## Provenance traversal

Candidate -> `investigation_id` -> `evidence[].ref` (a path into `investigation.json`, for example `sources[S00012].motion`) and `basis.det_ids/obs_ids`
-> `detections[]` / `observations[]` (checksum, access reference, release, pipeline version) -> `manifest.json`. Versions: `investigation.versions`
and `methods`. `results_hash` identifies the scientific core.

## Known interface limits

Positions of a track-subject investigation use its first candidate's position. `follow_up.rank` is not a probability. Spectral wavelengths come from
`wavelength_source` (some are not the calibrated CWAVE/CBAND values) and the UI should show that field. The sky-map has no morphology, no photospheric
reference and no Planet X location; do not draw any.
