# Real-data verification: public SPHEREx QR2 data from IRSA (2026-10-29)

## IRSA access stabilisation and NASA-target runs (2026-11-15, UTC)

This section supersedes the earlier "access recheck" note. It does **not** alter the historical QR2 validation below. Everything here is
operational or provisional: nothing is an independently validated astrophysical result, and nothing concerns Planet X.

### Routes and what the documentation supports

- IRSA's SPHEREx archive documentation (`caltech-ipac.github.io/spherex-archive-documentation`, "spherex data access") names the SIA2
  collections `spherex_dr1`, `spherex_dr1_deep`, `spherex_dr1_cube`, `spherex_qr3`, `spherex_qr3_deep`, says QR2 is "not searchable via SIA2"
  (still directly downloadable until February 2027), says SIA2 availability lags new data by about a day, and that new data appear first in
  TAP. HEIMDALL therefore searches `spherex_dr1`, `spherex_dr1_deep`, `spherex_qr3`, `spherex_qr3_deep` (the `_cube` product is not a Level 2 image).
- The generic SIA2 parameters (`COLLECTION`, `POS`, `RESPONSEFORMAT`, `MAXREC`) are documented at `irsa.ipac.caltech.edu/ibe/sia.html`
  (that page's own SPHEREx example still shows the older `spherex_qr2` name; the collection names above come from the SPHEREx documentation).
- **Live response format (validated 2026-11-15 05:02Z, `spherex_dr1`, CSV):** 51 columns including `obs_id` (`2025W33_2C_0321_1`),
  `access_url` (`https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/<week>/<l2b-version>/<detector>/level2_<obsid>D<n>_spx_<version>.fits`),
  `t_min`/`t_max` (MJD), `em_min`/`em_max` (metres), `obs_collection`, `dataproduct_subtype` (`science`), `calib_level` (2), `energy_bandpassname`
  (`SPHEREx-D<n>`), `access_estsize`, `s_region`. SIA2 carries **no checksum**; HEIMDALL records the SHA-256 of what it retrieves. A trimmed
  real sample is `tests/fixtures/sia2_dr1_live_sample.csv`.
- **Route policy (`heimdall/data_access/irsa_spherex.py`):** SIA2 first for current DR1/QR3; TAP is a bounded fallback (restricted to the same
  collections so the two routes mean the same thing). A request that names a legacy collection (e.g. `spherex_qr2`) goes to TAP only and is
  labelled legacy in the provenance; QR2 is never mixed in silently. Failover happens on timeout, HTTP 500/502/503 and malformed answers, not
  on 401/403/bad input. Each route has its own timeout (TAP min(configured, 45) s, SIA2 max(configured, 60) s), exponential backoff with up to
  25 % jitter, and a circuit breaker (3 consecutive failed discoveries open a route for 120 s; open routes are skipped and reported
  `SKIPPED_CIRCUIT_OPEN`). If no route answers, `discover` raises; it never returns `NO_OBSERVATIONS` for a failed search. Successful SIA2 and
  TAP answers are cached, so `HEIMDALL_OFFLINE=1` replays the same discovery (verified live below).
- Retrieval by `obs_id` alone still looks the file up through TAP; the pipeline always passes the `access_reference` obtained at discovery, so
  SIA2-discovered observations do not need TAP.

### Endpoint behaviour observed (all times UTC; `python3 -m heimdall irsa-status` unless noted)

| Time | TAP | SIA2 | Direct product (cutout) | ePSF calibration |
|---|---|---|---|---|
| 04:59 (curl) | 502 | 502 | not probed | 200 |
| 05:00-05:02 (curl, `MAXREC=3`) | 502 | 200 on 4 of 6 requests, 502 on the rest | - | - |
| 05:06 | 502 | 502 | OK (3.95 MB FITS, 20 s) | 404 (probe bug: unformatted `{det}` in the URL; fixed) |
| 05:12 | 502 | OK | - | - |
| 05:16 | timeout (46 s) | OK | OK | OK |
| 05:39 | 502 | 502 | 503 | OK |

SIA2 and TAP failed independently and intermittently; SIA2 was usable in most of the window in which TAP was not. IRSA remained unstable
throughout, which is why discovery is retried within a bounded budget rather than assumed.

### Real image retrieved and measured through the final adapter

`python3 scripts/irsa_live_smoke.py --attempts 8` (record: `.heimdall/verification/2026-11-15-irsa/live_smoke.json`):
attempt 1 failed on both routes (SIA2 502, TAP 502; reported as a service failure, not "no observations"); attempt 2 succeeded **through SIA2**.

- Image `2026W12_1A_0311_3D2`, release `spherex_dr1`, pipeline version 7.0.5, detector 2, MJD 61116.6838;
  `ibe/data/spherex/dr1/level2/2026W12_1A/l2b-v27-2026-235/2/level2_2026W12_1A_0311_3D2_spx_l2b-v27-2026-235.fits`; 0.05-degree cutout
  retrieved 2026-11-15T05:15:05Z (origin `LIVE`), 3 965 760 bytes, SHA-256 `8ddfd7120db45a9ed92676ed6d88a79ed43219e6c7811b37e4b9fa0caf7bb0d9`;
  ePSF `epsf_D2_spx_cal-epsf-v1-2026-191` SHA-256 `7b94af0250aa7e56a9e78af34ca2c1373985926c0a0f3263218d1b0ddc94a926`.
- HEIMDALL ingestion passed (no QC exclusions). One **forced PSF measurement at the unassociated field position** (RA 269.4485, Dec 4.7394):
  129.0 +/- 36.5 uJy, S/N 3.5, reduced chi-square 48.4. **This is not a detection and not a measurement of any source**: the position is not
  tied to a catalogue object, and the large reduced chi-square says the single-PSF model does not describe the stamp. It proves that access,
  ingestion and measurement work on a real image, nothing more.
- Offline replay: `HEIMDALL_OFFLINE=1 python3 -m heimdall coverage --source irsa-spherex --ra 269.4485 --dec 4.7394 --radius 0.02 --detectors 2`
  returned the same 63 detector-2 observations (4 epochs) from the cached SIA2 answer.

### NASA targets (saved JSON; `docs/NASA_TARGET_MATRIX.md` is generated from it)

The five-target runner (`.heimdall/verification/2026-11-15-targets/`, TAP route) produced the saved results for NASA-01..04. Two targeted
reruns were made because the access route changed materially: **NASA-05** (previously `BLOCKED`: discovery HTTP 502) now completed through
SIA2 (189 observations found, 12 selected, 10 measured, all products `LIVE`, SIA2 route recorded in `discovery.route`), and **NASA-01** was
rerun to exercise the fixed blind pipeline but IRSA returned 502 on both routes at 05:39Z, so that attempt is `BLOCKED` and is kept as
`.heimdall/state/targets/NASA-01/latest_attempt.json`; `latest.json` still holds the earlier completed result (`tinv:NASA-01:88a6ee56`).
A blocked attempt no longer replaces a completed result as `latest`.

| Target | Status | Observations found / measured | What it can and cannot say |
|---|---|---|---|
| NASA-01 Barnard's Star | COMPLETED / LIMITED_BY_DATA | 120 / 9 | all measured images have saturation flags; no valid motion test. Blind pipeline had failed in that run (since fixed; not rerun on this target). |
| NASA-02 Epsilon Indi B | COMPLETED / PASSED | 177 / 16 | motion consistent with catalogue (fitted 4.533 vs 4.613 arcsec/yr, 15 of 16 points, 521 d); variability with caveats. Provisional; registration artefact and blended companion not excluded. |
| NASA-03 WISE 0855 | PARTIAL / LIMITED_BY_DATA | 93 / 9 | too faint per image for centroids; no motion or variability test. |
| NASA-04 RW Aur | COMPLETED / LIMITED_BY_DATA | 111 / 12 | no comparable series with enough points; saturation flags. |
| NASA-05 R Dra | COMPLETED / LIMITED_BY_DATA | 189 / 10 | all 10 measured images saturation-flagged; no valid variability test; blind pipeline PARTIAL, 0 of 10 blind detections near the expected position (not a non-detection of the star, which is saturated). |

### Limits and reproduction

- No target result is an independently validated astrophysical claim; "LIMITED_BY_DATA" is a statement about the data, never a non-detection.
- Not done: live confirmation of the TAP fallback direction after SIA2 failure (it is covered by deterministic tests `tests/test_irsa_routes.py`,
  and in the live runs SIA2 or TAP-down/SIA2-up occurred, not SIA2-down/TAP-up); QR2 data (direct download only) are not searched.
- Reproduce: `python3 -m heimdall irsa-status`; `python3 scripts/irsa_live_smoke.py`; `scripts/run_nasa_targets.sh [NASA-0x ...]` (bounded health
  check, finite budget, exit 1 if no discovery route answers); `python3 -m heimdall target matrix --write docs/NASA_TARGET_MATRIX.md`;
  tests: `python3 -W error -m unittest tests.test_irsa_routes tests.test_adapters`.

---

## Historical QR2 validation (2026-10-29)

**What this is:** an end-to-end exercise of the HEIMDALL engine on genuine,
public SPHEREx Level 2 images. **What it is not:** scientific validation.
No significance was validated, no candidate reached `ATTEND` or `WATCH`, and
nothing here is a scientific claim about any source or about Planet X.

- **Part 1** (sections 1-8): 16 small (59x59 px) QR2 detector-2 cutouts, the
  first real-data run. Kept as recorded.
- **Part 2** (sections P1-P9): all 75 QR2 detector-2 images of the same field
  as 0.3-degree cutouts, with full calibration, Gaia DR3 and JPL context, all
  validation procedures and an offline rerun, plus checks on MWIR and QR3
  products.

## 1. Commands executed

Environment for every run: `HEIMDALL_STATE_DIR` and `HEIMDALL_CACHE_DIR` in the
session temporary directory and `HEIMDALL_REQUEST_TIMEOUT_S=180`. No credential
was used (the archive is public).

| Step | Command | Outcome |
|---|---|---|
| Access | `python3 -m heimdall access irsa-spherex` | reachable; `spherex.plane` visible |
| Coverage | `python3 -m heimdall coverage --source irsa-spherex --ra 150.7817877 --dec 3.3502204 --radius 0.05 --detectors 2` | `OK`: 75 detector-2 observations in 5 epochs |
| Run 1 | `python3 -m heimdall investigate --source irsa-spherex --ra 150.7817877 --dec 3.3502204 --radius 0.05 --detectors 2 --max-observations 16 --validate` | exit 0 after 1808 s (retrieval 1532 s, ePSF download 257 s, all computation about 10 s); run `R20261029T081953-8e3d2344` |
| Fixes | four engine defects exposed by run 1 (section 6) | full test suite re-run |
| Run 2 | same command, products from the verified cache | 13.5 s, 16/16 from cache; run `R20261029T085631-d5d295b1` |
| Run 3 | same command after the injection fix | 12.2 s, 16/16 from cache; run `R20261029T085842-d335b205` |
| Run 4 (final) | same command after bumping the rules versions to `reasoning-1.1.0-provisional` and `validation-1.1.0` (labels only) | 12.0 s, 16/16 from cache; run `R20261029T090247-3de23aa1`; results hash, validation outcomes and metrics identical to run 3 |

The field (RA 150.7817877, Dec 3.3502204) is the neutral position used in
IRSA's SPHEREx Source Discovery Tool tutorial. It is not one of the
Known-Objects checkpoints. The selection is 16 of the 75 images, evenly spaced
in time (deterministic rule recorded in the run).

Evidence kept locally (git-ignored, not committed):
`.heimdall/verification/2026-10-29-irsa-qr2/<run_id>/` (`investigation.json`,
`events.jsonl`, `skymap.json`, `skymap.svg`) and `run1_console.log`. The 16
cutouts (about 5.1 MB each, 81.6 MB in total) and the ePSF product (7.9 MB) remain in the
temporary cache and can be re-downloaded with the same command.

## 2. The products are genuine and correctly identified

All 16 products come from collection `spherex_qr2`, under
`ibe/data/spherex/qr2/level2/<week>/l2b-v20-...` or `l2b-v24-...`. Each was
cross-checked programmatically against an independent re-query of IRSA TAP,
and **every check passed for 16/16**:

- the header `OBSID` + `DETECTOR` equal TAP's `observationid` and band (`SPHEREx-D2`);
- `MJD-AVG` lies within TAP's time bounds;
- the header `VERSION` matches TAP's `provenance_version`;
- the product's own wavelength lookup puts the window centre inside TAP's band
  (1.103-1.650 um);
- the SHA-256 of each cached file equals the checksum recorded in the run;
- no synthetic marker is present, and each file has the 7 expected extensions
  ending with `WCS-WAVE`.

The QR3 ePSF calibration
(`qr3/epsf/cal-epsf-v1-2026-191/2/epsf_D2_spx_cal-epsf-v1-2026-191.fits`) also
matched its recorded SHA-256. IRSA publishes MD5 checksums only for full
products, so cutout bytes could not be checked against an archive checksum.
Integrity rests on verified TLS, FITS structure and these metadata
cross-checks.

| obs_id | MJD-AVG (UTC) | Pipeline | lambda at centre (um) | Analysed pixels | Detections | Pixel noise ratio | PSF-scale noise ratio (n) |
|---|---|---|---|---|---|---|---|
| 2025W17_4B_0167_1D2 | 60790.8453 | 6.4+psffix1 | 1.3750 | 44x59 | 14 | 1.048 | 0.92 (26) |
| 2025W19_2B_0021_1D2 | 60803.6193 | 6.4+psffix1 | 1.3750 | 42x59 | 15 | 1.091 | 0.77 (21) |
| 2025W21_1B_0182_2D2 | 60814.8975 | 6.4+psffix1 | 1.4132 | 59x59 | 21 | 1.035 | 0.84 (38) |
| 2025W47_1A_0657_1D2 | 60999.0792 | 6.4+psffix1 | 1.1238 | 59x59 | 20 | 1.098 | 1.28 (38) |
| 2025W47_1A_0669_2D2 | 60999.1482 | 6.4+psffix1 | 1.5013 | 59x59 | 23 | 1.077 | 0.97 (38) |
| 2025W47_2A_0327_1D2 | 61000.9811 | 6.4+psffix1 | 1.3414 | 59x59 | 20 | 1.100 | 1.05 (35) |
| 2025W47_2A_0357_2D2 | 61001.1173 | 6.4+psffix1 | 1.2639 | 59x59 | 20 | 1.056 | 0.98 (39) |
| 2025W47_2A_0507_3D2 | 61001.7985 | 6.4+psffix1 | 1.1574 | 59x59 | 22 | 1.054 | 1.12 (38) |
| 2025W47_2A_0534_3D2 | 61001.9330 | 6.4+psffix1 | 1.5456 | 59x59 | 23 | 1.129 | 1.47 (34) |
| 2025W47_2A_0613_4D2 | 61002.2719 | 6.4+psffix1 | 1.3010 | 59x59 | 24 | 1.062 | 1.02 (35) |
| 2026W19_2A_0757_1D2 | 61169.4638 | 6.5.6 | 1.4818 | 59x59 | 22 | 1.114 | 0.84 (38) |
| 2026W19_2A_1126_2D2 | 61170.4105 | 6.5.6 | 1.1366 | 59x59 | 19 | 1.055 | 1.12 (39) |
| 2026W20_1A_0133_1D2 | 61171.3576 | 6.5.6 | 1.2483 | 59x59 | 23 | 1.027 | 1.25 (35) |
| 2026W20_1A_0600_4D2 | 61172.6545 | 6.5.6 | 1.1062 | 59x58 | 19 | 1.105 | 0.95 (37) |
| 2026W20_1A_0960_2D2 | 61173.7388 | 6.5.6 | 1.1581 | 56x59 | 21 | 1.105 | 1.05 (30) |
| 2026W20_1A_1240_2D2 | 61174.4819 | 6.5.6 | 1.3025 | 46x59 | 13 | 1.070 | 1.24 (31) |

Some cutouts are narrower than 59 px because the field lies at a detector edge
(`parent_offset` x = 0). Epochs: MJD 60790.85, 60803.62 and 60814.90 (one
image each), 60999.08-61002.27 (7 images) and 61169.46-61174.48 (6 images).

## 3. Methods executed on the real data

| Stage | What ran |
|---|---|
| Ingestion and QC | 16/16 included. Every image passed units, flag vocabulary (16 bits), TAN-SIP WCS, WAVE-TAB lookup, `MJD-AVG` UTC, detector, `FINAST = 0`, `L2DQAFLG = Pass` and PSF checks. The only non-pass was the `flag_policy` WARNING: 9 policy flag names (for example BLOOM and GHOST) are not defined in QR2 headers |
| PSF | QR3 ePSF calibration (441 zones), placed with the cutouts' parent offsets |
| Detection | matched filter, PSF fit, two deblending passes: 319 accepted, 5 rejected |
| Association | 48 sources, 13 ambiguous (chained or conflicting) |
| Photometry and spectra | 734 forced measurements. Spectra assembled for every source; for example one source has 16 usable D2 points over 1.100-1.553 um (WCS-WAVE wavelengths). Detectors 1 and 3-6 are `NOT_IN_SELECTION` |
| Time | 5 epochs. Comparable series built, but **no variability test was possible**: no source had at least 3 measurements at matched wavelength (within 0.5 x bandwidth) across at least 2 epochs among these 16 images. This is not evidence of constancy |
| Astrometry | stationarity test (23 sources), linear-motion and parallax fits, and the empirical astrometric error model (section 4) |
| Transients and linking | 25 single-epoch sources tested; linking found no eligible tracklet, so 0 tracks |
| Validation | 8 procedures (section 5) |
| Reasoning and Planet X | 9 candidates assessed; region status `NOT_ASSESSABLE` |
| Outputs | investigation JSON, playback (16 frames), sky map JSON and SVG, events |

## 4. Measurements and findings (final run, `R20261029T090247-3de23aa1`)

- **Detection quality.** Grades: A 118, B 5, C 196. C reasons: poor PSF fit
  (reduced chi-square above 3) 122, SNR below 10 73, centroid fallback 23,
  OUTLIER flag at peak 6. Median reduced chi-square by SNR: 1.40 (5-10), 3.83
  (10-30), 1.49 (30-100), 9.14 (above 100). The ePSF does not describe
  sources to within the noise at moderate and high SNR. The cause (PSF model,
  pixel phase or blending) is not established.
- **Noise model.** Pixel-scale noise ratio 1.03-1.13 (median 1.073; each from
  about 2,000-3,000 pixels, standard error about 0.025). The VARIANCE plane
  therefore *underestimates* the pixel scatter away from detected sources by
  about 7%, which is within the provisional 0.8-1.25 tolerance. PSF-scale
  ratios rest on only 21-39 empty positions per cutout (standard error about
  0.2-0.3). Thirteen images are within bounds and three are undetermined, so
  the check is `NOT_ASSESSABLE`.
- **Astrometric error model.**
  - Error scale 1.341 +/- 0.044, from 18 sources, 254 detections and 472
    degrees of freedom. The check fails significantly.
  - Residual RMS 0.56" (RA) and 0.69" (Dec), against a median centroid sigma
    of 0.24" plus the 0.4" floor.
  - Per-image registration offsets: RMS 0.21"/0.23". One is significant:
    `2026W20_1A_0960_2D2`, +0.54" in RA (5.7 sigma).
  - The excess is concentrated in six sources: chi2/dof 3.2-7.2, median SNR
    15 (range 7-62), all chained-ambiguous and grade C, several with a
    detected neighbour within 3 px. The other 13 sources with at least 6
    detections have chi2/dof 0.18-1.29 (median 0.28), so for them the 0.4"
    floor is conservative.
  - The global scale therefore mixes two populations. It is a first-order
    correction, not a source-specific error model.
- **Motion candidates (6).**
  - The stationarity test gave Z 5.5-11.0, falling to 2.7-6.9 when errors are
    rescaled by the measured scale.
  - All parallaxes are consistent with zero (-0.22" to +0.25", sigma
    0.11-0.18"), and the linear-motion fits are poor (p < 1e-8).
  - Planet X status: **INCONSISTENT** for all six.
  - Attention: 4 `INCONCLUSIVE`, with failed critical checks for data-quality
    grade, association ambiguity, blending and the astrometric error model,
    and the noise check unresolved. 2 `EXPLAINED` as "position errors
    underestimated" (rescaled Z 2.7 and 2.8, below 3).
  - None shows evidence of genuine motion.
- **Transients.**
  - Of 25 tested: 3 `TRANSIENT_LIKE`, 18 `NOT_TRANSIENT`, 4 `NOT_ASSESSABLE`
    (no comparable reference image).
  - Two are `EXPLAINED` as image artefacts (OUTLIER flag at the peak).
  - One is `INCONCLUSIVE`: `S00025`, grade C, 1443 +/- 57 uJy against
    546 +/- 44 uJy in its only comparable reference image.
- **Look-elsewhere.** 23 motion and 25 transient tests. If calibrated, about
  0.13 false alarms would be expected at the watch threshold.

## 5. Validation runs on these images (scope `REAL | spherex_qr2 | config sha256:66e3a2be...`)

| Procedure | Outcome | Numbers |
|---|---|---|
| independent numerical checks | PASSED | 8/8 |
| injection-recovery | UNVALIDATED (insufficient sample) | 66 injections. Recovered by SNR: 2 0/10, 3 1/10, 4 1/10, 5 3/9, 6 5/9, 8 9/9, 10 8/9. 90% completeness at SNR about 7.6 (about 256 uJy; median flux sigma 33.9 uJy). Flux pull mean 0.52, std 1.33 (n = 25, SNR >= 5). Median-based position ratio 0.95 |
| inverted-image false detections | UNVALIDATED (insufficient sample) | 5 sigma: 0 in 16 images (95% upper 0.23 per image > 0.1). 4 sigma: 2 (approximation predicted 0.63) |
| constant-source variability null | UNVALIDATED (insufficient sample) | 5 trials (only 5 empty positions fit in these cutouts); KS p 0.97 |
| stationary-source motion null | UNVALIDATED (insufficient sample) | 9 controls; KS p 0.0002 (non-uniform even with 9 trials), 0 false positives at watch: errors conservative for these controls |
| noise-model calibration | NOT_ASSESSABLE | see section 4 |
| robustness | PASSED | detection recovery 1.00 (mesh x0.5 and x2), 0.99 (no deblending) |
| reproducibility | PASSED | 6/6 images bit-identical |

Every scientific cell for real data in this configuration remains
`UNVALIDATED`. The 59x59 cutouts are too small for the minimum trial counts.

## 6. Engine defects this run exposed, and their fixes

1. **The noise check ignored estimator precision** (run 1). Ratios within
   about 1.5 standard errors of the bounds produced `FAILED`, which made
   "noise underestimated" a SUPPORTED explanation and labelled all six motion
   candidates `EXPLAINED`. That attribution was unsupported. Fix: noise ratios
   now carry standard errors (1.166/sqrt(n) for a MAD-based sigma); failure
   needs a significant deviation; imprecise estimates are `NOT_ASSESSABLE`.
2. **No empirical check of the astrometric error model.** Added
   `motion.astrometric_error_model` (error scale, per-image offsets), a
   critical `astrometric_error_model` check for motion and track candidates,
   the alternative "position errors underestimated", and the rescaled
   statistic.
3. **A stage count was mislabelled.** `n_transient_like` counted linkable
   tracklets (0) instead of `TRANSIENT_LIKE` sources (3). It is now reported
   as both `n_transient_like` and `n_link_inputs`.
4. **The injection SNR grid restarted for every image** (runs 1-2), so the
   8- and 10-sigma bins were never injected into small cutouts. The grid now
   uses a counter shared across images (run 3).

Measurements did not change. Detections, rejections, source positions and
motion fits are bit-identical across all four runs, and runs 2, 3 and 4 have
the same `results_hash` (`sha256:3af2c9e7...`). Only interpretation and validation
metrics changed. The full suite was re-run after the fixes: 223 tests, 219
passed and 4 opt-in live tests skipped, also with `-W error`.

## 7. What this establishes, and what it does not

**Established on real data:**
- live access, discovery, retrieval, verified caching and recovery;
- parsing and QC of real QR2 Level 2 cutouts;
- use of the QR3 ePSF;
- every pipeline stage running with recorded statuses, exclusions and
  reasons;
- deterministic reruns;
- measured noise and astrometric behaviour of the engine on these images, and
  an empirical completeness curve for injected PSF-model sources.

**Not established:**
- any validated significance (every real-data cell is UNVALIDATED);
- any variability test (no comparable series);
- any moving-object linkage, or completeness for moving objects;
- photometric calibration accuracy;
- behaviour on detectors D1 and D3-D6 (in particular MWIR ePSF orientation),
  QR3 science images, full frames or other fields;
- any physical interpretation of any source.

Planet X is not supported or refuted by this run. The region status stays
`NOT_ASSESSABLE`, and a lack of candidates is not evidence of absence.

## 8. Needed for real-data validation (as written after Part 1)

Larger samples, a better astrometric error model, the CWAVE/CBAND and
solid-angle products, the MWIR ePSF orientation and moving-object injection.
Part 2 addresses each of these; see P9 for what remains.

# Part 2: 0.3-degree cutouts, external context, QR3 and MWIR

## P1. Data obtained and commands

All products are public (IRSA, no credentials). A development helper fetched
them through the engine's own adapter (`IrsaSpherexSource.retrieve`), so the
cache keys are the ones the pipeline uses:

- all 75 QR2 detector-2 images of the field (RA 150.7817877, Dec 3.3502204) as
  0.3-degree cutouts (up to 176 px; clipped near detector edges): 75/75 retrieved,
  0 failures, 14 s to 3 min each. Pipeline versions: `6.4+psffix1` (48) and `6.5.6` (27);
- 8 QR2 detector-5 (MWIR) cutouts of 0.1 degree, evenly spaced in time;
- the QR3 ePSF for D2 and D5; the QR2 spectral WCS (33.3 MB) and solid-angle map
  (16.7 MB) for D2;
- one QR3 D2 science product (`2026W36_1A_0001_1D2`, pipeline 7.0.5) as a
  0.1-degree cutout at RA 56.170, Dec 69.623, because QR3 did not yet cover the
  verification field.

Final command (first online, then repeated offline):

```bash
HEIMDALL_STATE_DIR=<state> HEIMDALL_CACHE_DIR=<cache> \
python3 -m heimdall investigate --source irsa-spherex --ra 150.7817877 --dec 3.3502204 --radius 0.15 \
    --detectors 2 --calibration-products full --validate --label "..."
HEIMDALL_OFFLINE=1 <the same command>      # every input from the cache
```

| Run | What |
|---|---|
| `R20261029T095851-96d3381e`, `R20261029T100408-be3e4133` | context on the 16 Part-1 cutouts, before and after the JPL tolerance fix |
| `R20261029T104206-305e8221` | preliminary: 45 early images (exposed the photometric and association defects) |
| `R20261029T105918-09c79536` | all 75 images online, before the Planet X and association fixes |
| **`R20261029T112935-4636a636`** | **final**: all 75 images, offline, all fixes; 656 s, peak memory 1.04 GB; config `sha256:7988a557...`, results `sha256:362634ed...` |

The offline final run used no network: discovery, 75 cutouts, 3 calibration
products, the Gaia answer and 43 JPL answers all came from the cache
(`manifest.json`: 75 inputs `CACHED`).

## P2. Product checks (details in `SPHEREX_FIDELITY.md`)

- **QR3 flags.** QR3 headers define flag planes with `MSKNnnnn/MSKMnnnn/MSKDnnnn`,
  not `MP_<NAME>`. The engine correctly refused the product
  (`FLAG_SEMANTICS_NOT_ESTABLISHED`) until both conventions were parsed. The 14
  names shared with QR2 have identical bits; QR3 adds 8 artefact planes.
- **QR3 embedded EPSF** (first real use): ingested without exclusion;
  103 detections in a crowded 59x59 cutout; median reduced chi-square 5.8 for
  38 converged fits at SNR >= 20.
- **Calibration products:** CWAVE/CBAND and the solid-angle map have the
  expected extensions and units; every final-run point used `CWAVE_CBAND`
  wavelengths and `SOLID_ANGLE_PIXEL_MAP` solid angles.
- **MWIR ePSF orientation (D5):** median reduced chi-square 1.16 as delivered
  (14 stars), 1.25 mirrored in X, 1.17 with mirrored zone centres, 1.22 with
  both. Used as delivered. Weak evidence (one detector, 8 images, 14 stars).

## P3. External context on real data

- **Gaia DR3:** 324 stars with G < 21 in a 0.17-degree cone; 229 of 682
  sources matched.
- **Registration against Gaia:** for all 75 images the mean offset of matched
  stars from their propagated Gaia positions has median 0.155 arcsec and maximum
  0.429 arcsec. It is significant (>= 3 sigma) in 46 images: per-image
  registration errors are real and correlated across sources, though mostly
  below the 0.4 arcsec floor. They are measured, not yet corrected.
- **JPL SB Ident:** 43 images needed a query (transients and single-epoch
  movers), and all 43 completed. Two known asteroids were found at their
  predicted positions: **368 Haidea** and **44524 (1998 YZ3)**. They explain
  9 transient and 4 motion candidates of the final run.
- **368 Haidea in the Part-1 data:** the two OUTLIER-flagged "transients" that
  Part 1 had explained as image artefacts lie 0.29 and 0.48 arcsec from JPL's
  predicted positions (tolerance 3.74 arcsec), in images 0.47 days apart
  (predicted V = 16.4; measured 1931 uJy, about AB 15.7 at 1.37 um). OUTLIER
  marks change on the sky, so it is no longer treated as an artefact flag.
- **Service behaviour:** JPL two-pass queries without a magnitude limit took
  2.5-6 min and returned about 7000 screening rows. With `mag-required=true`
  and `vmag-lim=21` they took about 24 s and gave the same integrated result for
  the tested image (56081 (1999 AU8) found in both).

## P4. Measurements (final run)

- 75/75 images usable; 5 epochs (MJD 60790.8: 5 images, 60803.6: 4,
  60814.9: 2, 60998.3: 37, 61169.1: 27).
- 12,770 detections; 682 sources (287 seen in one image only).
- Astrometric error scale 1.077 +/- 0.005 (280 sources). Photometric error
  scale 1.191 +/- 0.011 (5550 comparable series): the field's fluxes scatter
  19% more than their stated errors. This is below the provisional 1.25 limit,
  so the check passes, but every flux-based significance is also reported
  rescaled by 1.191.
- Median per-pixel noise ratio 1.072.

## P5. Candidates (final run)

292 candidates: 185 `INCONCLUSIVE`, 107 `EXPLAINED`, none `WATCH` or `ATTEND`.

- Explained: variability by a single-measurement outlier (44) or an artefact
  (4); motion by a single image (38), by a Gaia star whose catalogued motion
  contradicts it (18), by a high proper-motion star whose Gaia motion matches it
  (11), by known asteroids (4) or artefacts (4); transients by known asteroids
  (9) or artefacts (2).
- Inconclusive, most often because of failed critical checks:
  pipeline-version consistency (176; the field mixes pipeline 6.4 and 6.5.6
  images, and no cross-version calibration is applied), quality grade (176),
  blending (69), ambiguous association (48), fit quality (43) and robustness to
  systematics (29).
- Follow-up ranks 1-3 are variability candidates S00303 (Z = 10.3; 8.5 after
  flux-error rescaling), S00263 (7.0; 5.5) and S00571 (6.5; 5.3). They are
  `INCONCLUSIVE` because their measurements span both pipeline versions and the
  noise model is unresolved.
- Planet X: 90 `INCONSISTENT`, 14 `NOT_ASSESSABLE`, 1 `UNCONSTRAINED`, none
  consistent. Region status `COMPLETENESS_LIMITED` (P6).
- The online run before the fixes had reported one "consistent" candidate
  (S00043). It was a defect: a parallax fit to 3 positions (1 degree of
  freedom) of a blended, chained source that failed 5 critical checks (P7).

## P6. Validation on these images (scope `REAL | spherex_qr2 | sha256:7988a557...`)

| Procedure | Sample | Outcome | Measured | Limits |
|---|---|---|---|---|
| independent numerical checks | 8 | PASSED | | software mathematics only |
| injection-recovery (detection) | 1093 sources, SNR 2-10 | **MISCALIBRATED** | 90% completeness at SNR 7.7 (256 uJy); flux pull mean +0.30, std 1.31; position ratio 1.19 (54% inside the 68% region) | injected sources sit at empty positions on real sky; their PSF equals the model |
| inverted-image null | 75 images | **MISCALIBRATED** | 29 false detections at 5 sigma: 0.39 per image (95%: 0.26-0.56), about 130 times the Gaussian expectation | negative structure (for example near bright stars) is not Gaussian noise |
| constant-source variability null | 40 trials | UNVALIDATED | | fewer than 100 trials |
| stationary-source motion null | 55 Gaia stars with proper motion < 10 mas/yr | UNVALIDATED | | fewer than 100 trials |
| noise-model calibration | 75 images | NOT_ASSESSABLE | median pixel ratio 1.072 | |
| moving-object injection | 32 bodies, all in the field for >= 2 epochs | UNVALIDATED | recovered 7/32 (95%: 0.11-0.39); 589 uJy 2/11, 1105 uJy 2/11, 2210 uJy 3/10; 100-271 AU 1/5, 271-737 AU 2/11, 737-2000 AU 4/16; 0 false links per experiment; median distance error 8.6% | 4 x 8 bodies; constant flux; this field and cadence only |
| PSF-mismatch sensitivity | 75 sources, PSF broadened by 0.3 px | INFORMATIONAL | flux ratio 0.999, pull std 1.01, chi-square 1.26, position ratio 1.10 | one form of PSF error |
| robustness of detection settings | 2907 detections | PASSED | | first 6 images |
| reproducibility of detection | 6 images | PASSED | bit-identical | |

What these show:
- The engine's flux and position uncertainties are too small for faint
  sources on real sky: by about 30% (flux) and 20% (position) for injected
  sources, and 19% (flux) and 8% (position) for the field's own repeated
  measurements. A 0.3 px PSF error does not explain this, so the likely cause
  is background structure and confusion that the per-pixel variance omits.
- Single-image false detections at 5 sigma are far more frequent than Gaussian
  noise predicts. Transient significances therefore cannot be validated here,
  and none is promoted.
- The current linking finds few injected distant bodies on this cadence
  (7/32), so the region statement is weak and says so. It is a conditional
  sensitivity statement, not evidence of absence.

## P7. Defects found in Part 2 and fixed

| Defect | Found by | Fix |
|---|---|---|
| QR3 products excluded (flag convention unknown) | QR3 product | both conventions parsed; inconsistent planes reported |
| OUTLIER treated as an artefact, rejecting real movers | JPL match of 368 Haidea | OUTLIER is a change flag |
| JPL tolerance used the coarse first-pass error (one body "matched" 46 detections) | 16-cutout context run | second-pass positions use the stated assumed error |
| first-pass screening rows treated as predictions | cached JPL response | only second-pass bodies count |
| JPL queries took minutes | live timing | magnitude-limited queries (about 24 s) |
| calibration arrays read per image (about 100 MB each) | memory review | read once, shared read-only |
| CLI crash on a completeness-limited region | synthetic demo | fixed, with a test |
| moving-object false links counted the field's own tracks | synthetic demo | only links created by the injection count |
| region statement hid the distance dependence | synthetic demo | completeness by distance included |
| photometric errors underestimated without any check | preliminary run (pull std 1.38) | photometric error model, critical check, rescaled statistic |
| a 1-dof parallax fit on an unreliable source was "consistent" with Planet X | online final run | `fit_quality` needs >= 3 dof; new `measurement_reliable` requirement |
| bright stars fragmented into single-image sources | Gaia cross-check | brightest-first split of conflicting components |

## P8. What Part 2 establishes, and what it does not

**Established on real data:**
- the complete pipeline, including Gaia and JPL context, full calibration and
  every validation procedure, on 75 genuine images, and an offline rerun with
  no network access;
- QR3 ingestion with embedded EPSF tables;
- identification of real known asteroids at their predicted positions;
- measured error-model deficiencies and single-image false-detection rates on
  real data, with the engine refusing to promote any candidate as a result.

**Not established:**
- any validated real-data significance (no real cell is `VALIDATED_FOR_CONFIG`);
- the physical nature of any source; the follow-up candidates are unexplained,
  not anomalous;
- usable moving-object completeness: 7/32 is too low and too small a sample;
- behaviour on D1, D3, D4 and D6, QR3 fields at scale, or other fields.

Planet X is neither supported nor refuted. No region-level absence is claimed.

## P9. Still needed

- **Linking for faster movers:** velocity-based tracklets inside each epoch,
  then re-measure completeness with at least 100 covered bodies
  (`validation.moving_object_experiments`).
- **Error models at the source:** a background-structure (confusion) term in
  flux and position uncertainties, calibrated by injection and the field's
  repeated measurements, and Gaia-based per-image registration corrections
  applied before the motion stages.
- **Cross-version calibration:** relative photometry between pipeline 6.4 and
  6.5.6 images, or analysis restricted to one version.
- **Larger samples for the nulls:** full frames or more cutouts, so that the
  variability and motion nulls reach 100 trials.
- **Artefact model:** a model of negative and positive structure near bright
  stars, measured by the inverted-image null.
