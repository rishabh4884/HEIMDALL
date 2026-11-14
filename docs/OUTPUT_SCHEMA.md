# Output schemas (the contract for the future UI)

All outputs are JSON produced by `heimdall.pipeline.results.plain`. Enums are
strings, times are ISO-8601 UTC or MJD (UTC), angles are degrees (ICRS) and
positional uncertainties are arcsec. A `null` number means not computed or not
available; the neighbouring `status`/`reasons` fields say why. Contract records
(`Observation`, `Candidate`, `Validity`, `Check`, `Alternative`, `Statement`,
`ValueObject`) keep their field names from `heimdall.contracts`.

## Run directory

```
<state_dir>/runs/<run_id>/
  investigation.json   schema heimdall.investigation/1.1 (1.0 plus the additive `investigations` section)
  skymap.json          schema heimdall.skymap/1.0
  skymap.svg           minimal rendering (not the product UI)
  events.jsonl         one event per line
  request.json, config.json
  manifest.json        schema heimdall.manifest/1.0: every input (obs_id, access_reference, sha256,
                       retrieval_origin, release, pipeline_version, mjd_avg, included, exclusion_reasons),
                       failed items, calibration products, external queries (status, from_cache),
                       config and results hashes, and the reproduction command
```

## `heimdall.investigation/1.1` (1.0 fields below are unchanged; 1.1 adds `investigations`, see the end)

| Key | Content |
|---|---|
| `schema`, `schema_version`, `run_id`, `created_at` | identity |
| `request` | region, source, filters, `run_validation`, `local_root`, `label`, `context` (`auto`/`none`), `calibration_products` (`epsf`/`full`) |
| `config`, `config_hash` | full engine configuration and its SHA-256 |
| `data_origin` | `REAL` or `SYNTHETIC` (never mixed in one run) |
| `status` | `{execution: COMPLETED/PARTIAL/FAILED, outcome: OK/NO_OBSERVATIONS/INSUFFICIENT_OBSERVATIONS/PARTIAL/null, error}` |
| `stages[]` | `{stage, status: COMPLETED/NOT_APPLICABLE/INSUFFICIENT_INPUTS/FAILED, partial, started_at, duration_s, counts, reasons, error}` |
| `observations[]` | `{obs_id, observation (contract Observation with tagged metadata), product_checksum, product_access_reference, retrieval_origin, included, exclusion_reasons, qc {items[], suitability{detection, photometry, astrometry, spectral, temporal}, suitability_reasons}, meta {detector, release, pipeline_version, mjd_avg, finast, dqa, psf, wavelength_source, solid_angle_source, ...}, footprint {ra[], dec[]}, detection {status, background, noise, nominal_false_positives, n_accepted, ...}}` |
| `failed_items[]` | `{ref, error {code, message, detail}}`: an item failed, not "empty" |
| `epochs[]` | `{index, mjd_start, mjd_end, n_observations, obs_ids}` |
| `detections[]`, `rejected_detections[]` | DetectionRecord: `det_id, obs_id, x, y, ra, dec, cov_sky_arcsec2, sigma_major_arcsec, astrometry_usable, amplitude(_sigma) [MJy/sr], flux_ujy, flux_sigma_ujy, solid_angle_source, snr_peak, snr_fit, chi2_red, masked_fraction, fit_status, peak_flags, footprint_warning_flags, wavelength_um, bandwidth_um, wavelength_source, mjd, detector, quality_grade, quality_reasons, accepted, rejection_reasons, nearest_neighbour_px, neighbours_subtracted` |
| `sources[]` | `{source_id, ra, dec, cov_arcsec2, ellipse_1sigma, n_obs, n_epochs, det_ids, obs_ids, ambiguous, ambiguity_kinds, alternative_groupings, released_from, worst_detection_grade, spectrum, series[], variability[], spectral_comparisons[], motion, parallax, analyses{spectral, variability, motion, parallax: {status, reasons}}, forced[]}` |
| `sources[].spectrum` | `{points[] (SeriesPoint: obs_id, mjd, epoch_index, detector, release, pipeline_version, wavelength_um, bandwidth_um, wavelength_source, flux_ujy, flux_sigma_ujy, measurement, det_id, quality_grade, peak_flags, usable, reasons), coverage[] {detector, nominal_range_um, state: MEASURED/COVERED_NOT_USABLE/NOT_IN_SELECTION, n_measurements, n_usable, wavelength_min/max_um}, band_means[], slope, wavelength_sources, notes}` |
| `sources[].variability[]` | `{series_id, status, reasons, n_points, n_epochs, weighted_mean_ujy, chi2, dof, significance, max_abs_pull, max_pull_obs_id, leave_one_out, peak_to_peak_ujy}` |
| `sources[].motion` | `{status, reasons, n_points, baseline_days, chi2_stationary, chi2_linear, motion (Delta chi2), gof_stationary, gof_linear, mu_arcsec_per_yr, mu_cov, mu_total_*}` |
| `sources[].parallax`, `tracks[].parallax` | `{status, reasons, parallax_arcsec, parallax_sigma_arcsec, mu_arcsec_per_yr, cov (5x5), chi2, dof, gof, condition_number, distance_au, distance_interval_au [lo, hi or null = unbounded], notes}` |
| `associations[]` | contract `Association` (det_ids, ambiguity count, alternative_groupings) |
| `transients[]` | `{source_id, status: TRANSIENT_LIKE/NOT_TRANSIENT/NOT_ASSESSABLE, probe_*, n_references, reference_flux_ujy, z, significance, references[]}` |
| `tracks[]`, `linking` | tracks `{track_id, source_ids, det_ids, obs_ids, epoch_indices, n_points, motion, parallax, ambiguous, shares_sources_with, link_rule, diagnostics {effective_dof, raw_dof, rate_scale_arcsec_day, rule, n_absorbed_chains, absorbed_chains[] {source_ids, epochs, consistent_tracklet, reason}, tracklets[] {source_id, epoch, n_points, rate_sigma_arcsec_day, informative, unavailable_reason}}}`; linking `{n_inputs, n_feasible_pairs, n_evaluations, truncated, r_min_au, n_rejected_low_effective_dof, n_absorbed_chains, rejected_examples[] (same diagnostics plus n_points, p_value, parallax_sigma_arcsec), rate_scale_arcsec_day, not_linked{source: reason}}` |
| `candidates[]` | CandidateAssessment (below) |
| `planet_x` | `{candidates[] {candidate_id, status}, region {status: NOT_ASSESSABLE/COMPLETENESS_LIMITED, reason or statement, injection_validity, completeness {n_injected, n_covered_2_epochs, n_recovered, coverage_fraction, completeness_given_coverage, completeness_interval_95, by_snr[], by_distance[], false_links_per_experiment}, n_candidate_tracks, n_consistent_candidates, scenario, disclaimer}}` |
| `context` | `{reason (why the stage did not run, or null), gaia {service, status: COMPLETED/FAILED/OFFLINE_NOT_CACHED, reason, from_cache, n_stars, g_max, region, n_sources_matched}, small_bodies {service, n_queries, status_counts, matches[] {det_id, obs_id, name, separation_arcsec, tolerance_arcsec, vmag, ephemeris_error_arcsec, ephemeris_error_assumed}, queries[] {obs_id, status, reason, from_cache, n_predicted, api_version, params}}, registration_vs_gaia[] {obs_id, n_stars, mean_offset_arcsec [dRA*, dDec], se_arcsec, significance}}` |
| `astrometric_error_model` | `{status, reasons, floor_arcsec, n_sources, n_detections, dof, scale, scale_se, median_source_scale, n_outlier_sources, residual_rms_arcsec, image_offsets[] {obs_id, n, mean_arcsec, se_arcsec, significance}, image_offset_rms_arcsec}` |
| `photometric_error_model` | `{status: COMPLETED/INSUFFICIENT_INPUTS, scale, scale_se, n_series, method, assumption, reason}` |
| `validation` | `{requested, scope, runs[] (ValidationRun, see VALIDATION.md)}` |
| `playback` | below |
| `look_elsewhere` | numbers of tests and the expected false alarms at the watch threshold if calibrated |
| `unavailable_analyses[]` | `{analysis, state, reason}` (for example the known-object check, star catalogue, spectral anomalies) |
| `warnings[]`, `events[]`, `methods`, `provenance`, `provenance_calibrations[]` | traceability |
| `results_hash` | SHA-256 of the scientific core (inputs, config, measurements, statistics, categories); equal on reproduction |

### CandidateAssessment

```
candidate_id, kind (VARIABILITY|MOTION|TRANSIENT), subject_kind (SOURCE|TRACK), subject_id, category,
position {ra_deg, dec_deg, cov_arcsec2, ellipse_1sigma {a_arcsec, b_arcsec, pa_deg}},
statistic {name, method_id, significance {statistic, dof, reference, log_p, p_value, log10_p, sigma_equivalent,
           convention, conversion_method}, empirically_rescaled (motion/track: covariances x error_scale^2; variability/
           transient: flux errors x flux_error_scale; null when the scale is <= 1 or unknown), error_scale, flux_error_scale,
           leave_one_image_out, meaning, not_meaning},
quality_grade, cell_validity (Validity of the scope cell),
candidate (contract Candidate: attention_level, validity, measurements[] (tagged D values), checks[], alternatives[],
           statements[], attention_reason, cap_reason, anomaly (always U/NOT_APPLICABLE)),
evidence_for[], evidence_against[], missing_evidence[], contradictions[], assumptions[],
unusualness {sigma_equivalent, statistic, validity, is_probability_of_planet_x: false},
physical_interpretation {status: NOT_ESTABLISHED|ORDINARY_EXPLANATION_SUPPORTED (the older per-candidate field; unchanged), hypothesis, note,
                         investigation_id, interpretation_status (1.1; the subject-level scientific status, see `investigations`)},
planet_x (PlanetXAssessment: status, hypothesis, scenario (tag A), expected_parallax_arcsec, measured,
          requirements[] {req_id, description, state, detail, evidence}, competing_explanations[],
          separation_of_claims, disclaimer),
explanation[] (rule trace), rules_version,
context {known_object {status: CONFIRMED_MATCH|PROBABLE_MATCH|NO_MATCH|NOT_ASSESSABLE|EXCLUDED_BY_MOTION, name, reason,
         n_images_matched, n_images, matches[], incomplete_queries[]},
         catalogue {status: MATCH|NO_MATCH|NOT_ASSESSABLE, gaia_source_id, separation_arcsec, d2, n_within_gate, g_mag,
         pm_mas_yr, pm_error_mas_yr, parallax_mas, parallax_error_mas, reason}},
follow_up {rank (1 = first), attention_level, validity, failed_critical_checks, unresolved_critical_checks,
           rescaled_sigma_equivalent, basis (states that the rank is not a probability)}
```

`candidates[]` is sorted by `follow_up.rank`. `statistic.leave_one_image_out` (motion and tracks) gives the lowest
significance after removing any one image and the image removed.

## `heimdall.playback/1.0` (inside `investigation.json` as `playback`)

```
ordering: "MJD-AVG (UTC) ascending; obs_id breaks ties", interpolation: "none ...",
epochs[], frames[] {index, obs_id, mjd, iso_utc, epoch_index, detector, wavelength_um, bandwidth_um, wavelength_source,
                    footprint {ra[], dec[]}, included, exclusion_reasons, suitability, detections[] (det_ids),
                    measurements[] {source_id, ra, dec, flux_ujy, flux_sigma_ujy, status}},
tracks[] {track_id, points[] {det_id, obs_id, mjd, ra, dec}}
```

`Engine.playback(run, mjd_start, mjd_end)` returns the same structure for a
time window, plus `window.excluded[]` with a reason per frame.

## `heimdall.skymap/1.0`

```
run_id, data_origin, watermark ("SYNTHETIC - NOT REAL DATA" or null),
frame {system: ICRS, units: deg, epoch, projections {local: gnomonic, all_sky: Mollweide}, orientation, uncertainty},
field {ra_deg, dec_deg, radius_deg},
layers {
  coverage[] {obs_id, detector, mjd, epoch_index, wavelength_um, polygon {ra[], dec[]}, included, exclusion_reasons},
  coverage_depth {projection, axis_deg[], depth[][], in_field_points, fraction_uncovered, max_depth, median_depth_in_field},
  detections[] {det_id, obs_id, source_id, ra, dec, ellipse_1sigma, snr, flux_ujy, grade, mjd, detector, wavelength_um},
  sources[] {source_id, ra, dec, ellipse_1sigma, n_obs, n_epochs, ambiguous, max_variability_sigma, stationarity_sigma},
  candidates[] {candidate_id, kind, category, subject_id, attention_level, validity, ra, dec, ellipse_1sigma,
                sigma_equivalent, planet_x_status, investigation_id, interpretation_status, evidence_refs[], provenance_refs[]},
  associations[] {source_id, ambiguous, segments[] {det_id, from [ra, dec], to [ra, dec]}},
  tracks[] {track_id, ambiguous, points[] {det_id, ra, dec, mjd}, distance_au, meaning},
  bands[] {detector, n_observations, wavelength_range_um},
  time {epochs[], frames[] {index, obs_id, mjd, iso_utc}}},
line_semantics,
external_layers {star_catalogue {status: AVAILABLE|NOT_AVAILABLE, source, g_max, epoch, items[] {source_id, ra, dec, ref_epoch,
                 g_mag, pmra, pmdec, parallax}, reason},
                 solar_system_ephemerides {status: AVAILABLE|PARTIAL|NOT_AVAILABLE, source, meaning, queried_obs_ids,
                 failed_obs_ids, items[] {name, ra, dec, vmag, obs_id, mjd}, reason}}  (real query results only),
index {scheme, tile_id, band_height_deg}, viewport (when requested) {centre, fov_deg, level, detections {mode: items|tiles, ...}}
```

Spatial queries: `heimdall.skymap.index.SkyIndex(items).cone(ra, dec, r)` and
`.viewport(ra, dec, fov, max_items)`, which returns items, or tile counts when
there are too many. Tile ids are `L<level>:<band>:<cell>` with band height
180/2^(level+1) degrees.

## `investigations` section (schema `heimdall.investigations/1.0`, added in investigation schema 1.1; additive)

`investigation.json["investigations"] = {schema, versions, n_investigations, interpretation_counts, hypothesis_counts, investigations[]}`.
One investigation per subject (a source or a linked track) that has at least one candidate. Each contains:

| field | meaning |
|---|---|
| `investigation_id`, `subject` (kind, id, ra_deg, dec_deg, ellipse_1sigma, member_source_ids), `candidate_ids` | identity; positions are the candidates' real positions |
| `patterns` | triggering patterns (FLUX_CHANGE, SINGLE_EPOCH, POSITION_CHANGE, LINKED_MOTION) with `admissible`, `reasons`, `attention`, `validity` |
| `evidence[]` | typed items: type, domain, ref (path into the run), value, uncertainty, quality_grade, admissible, admissibility_reasons, basis (det_ids, obs_ids, fit_id, record_id, depends_on), relation, provenance, not_assessable_reason |
| `dependency_groups[]`, `evidence_relations[]` | independent groups (`merged_because`: SHARED_FIT, SAME_RECORD, DECLARED_DEPENDENCY, SHARED_DETECTIONS_SAME_DOMAIN or INDEPENDENT); unmerged relations (SHARED_DETECTION_CROSS_DOMAIN, IMAGE_CORRELATED) |
| `hypotheses[]` | hypothesis_id, family, statement, status (SUPPORTED, CONSISTENT, UNRESOLVED, CONTRADICTED, NOT_ASSESSABLE), status_rule, predictions[] (result SUPPORTS, CONTRADICTS, NEUTRAL, NOT_ASSESSABLE; evidence_ids; group_ids; reason; missing_dependency), counts of supporting/contradicting groups, competitors, falsifiers, follow_ups, max_status |
| `interpretation` | `status` (EXPLAINED, PROVISIONAL, UNRESOLVED, UNEXPLAINED_UNDER_INVESTIGATION, NOT_ASSESSABLE, INCONCLUSIVE), viable_hypotheses, unexplained_patterns, contradictions |
| `missing_evidence`, `follow_ups`, `planet_x`, `upstream`, `versions` | what could not be assessed; recommended observations (with `feasible_in_heimdall`); Planet X status only (never a probability); quality/validation/provenance references; rule and catalogue versions |

Each candidate's `physical_interpretation` gains `investigation_id` and `interpretation_status` (additive; its existing `status` and the attention level are unchanged).
Sky-map candidate records gain `investigation_id`, `interpretation_status`, `evidence_refs` and `provenance_refs` (null/empty when the run has no investigations); the sky-map schema stays `heimdall.skymap/1.0`.
`reinvestigate` writes the same structure plus `source_run`. Interpretation status is computation-independent of the run's execution status.

Additional runtime fields not listed in the tables above: `transients[]` also carry `probe_det_id`, `probe_obs_id`, `probe_flux_ujy`, `probe_sigma_ujy`,
`probe_wavelength_um`, `reference_sigma_ujy`; `detections[]` also carry `amplitude_sigma`, `solid_angle_sr`, `n_fit_pixels`, `fit_note`, `inverted`
(an inverted-image null detection); `hypotheses[]` also carry `instance_id`, `applies_to_candidates[]`, `n_supporting_groups`, `n_contradicting_groups`
and (existing alternatives only) `evidence_refs[]`.
`sources[].spectrum` keys: `points[]`, `slope`, `band_means`, `coverage[]` (per detector: MEASURED or a stated reason), `wavelength_sources[]`, `notes`.

### Which status means what (for the UI)

| Field | Domain | Meaning |
|---|---|---|
| `status.execution`, `stages[].status` | computation | did the code run (COMPLETED, PARTIAL, FAILED, NOT_APPLICABLE, INSUFFICIENT_INPUTS) |
| `status.outcome` | computation | OK, NO_OBSERVATIONS, PARTIAL, ... (never a scientific claim) |
| `candidate.attention_level` | review priority | ATTEND, WATCH, EXPLAINED, INCONCLUSIVE, NOT_ASSESSABLE (not a probability) |
| `cell_validity.state` | statistical trust of the scope | NOT_ASSESSED, UNVALIDATED, VALIDATED_FOR_CONFIG, MISCALIBRATED, NOT_ASSESSABLE |
| `interpretation.status` | scientific interpretation of a subject | EXPLAINED, PROVISIONAL, UNRESOLVED, UNEXPLAINED_UNDER_INVESTIGATION, NOT_ASSESSABLE, INCONCLUSIVE |
| `hypotheses[].status` | one hypothesis | SUPPORTED, CONSISTENT (not support), UNRESOLVED, CONTRADICTED, NOT_ASSESSABLE |
| `planet_x.status` | one scenario | CONSISTENT_REQUIRES_CONFIRMATION, INCONSISTENT, UNCONSTRAINED, NOT_ASSESSABLE, or NOT_APPLICABLE; never a probability |

Absent data is explicit: `null` with a `reason`, `status: NOT_ASSESSABLE`, or an `unavailable_analyses[]` entry; sky-map `external_layers` carry `status: NOT_AVAILABLE`.
`Engine.skymap` rebuilds the investigation metadata offline for runs saved before schema 1.1.
