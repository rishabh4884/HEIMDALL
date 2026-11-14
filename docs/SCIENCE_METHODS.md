# Scientific methods

Each section starts with a plain-language explanation, then gives the exact
method, its assumptions and when it cannot run. Every numeric threshold is a
**provisional engineering default** recorded in the versioned configuration
(`heimdall/engine/config.py`, hashed into every result). None is a validated
scientific threshold.

## 1. What a SPHEREx image contains

SPHEREx takes images through a *linear variable filter*: the wavelength changes
across the detector, so every pixel sees a slightly different wavelength. A
star therefore appears at a different wavelength in each image, and a source's
spectrum is built from many images. Each Level 2 file has the image (surface
brightness in MJy/sr, zodiacal light *not* removed), a variance plane, a flag
bitmask, a zodiacal-light model, a PSF model and a wavelength lookup table.
Product-specific assumptions and limitations are documented alongside the
relevant adapters and ingestion checks.

## 2. Quality control (`ingest/spherex.py`)

*Plain language:* before using an image we check that it is what it claims to
be, and decide separately whether it is good enough for detection,
photometry, astrometry, spectra and time analysis.

Checks: required extensions; matching array shapes; units (`MJy/sr`,
`(MJy/sr)^2`); fraction of finite pixels with positive variance (minimum 50%);
flag vocabulary read from the product's own `MP_<NAME>` keywords (no
vocabulary means flags cannot be interpreted, so the image is excluded);
celestial WCS; wavelength lookup; observation time (`MJD-AVG`, UTC only);
detector number; `FINAST` (fine astrometry; non-zero makes astrometry
unsuitable); `L2DQAFLG` (must be `Pass`); PSF model availability; synthetic
marker versus declared provenance. Each check is PASSED, FAILED, WARNING or
NOT_ASSESSABLE with a reason. Exclusions and per-analysis unsuitability are
recorded, never silent.

Pixel policy: pixels flagged `SUR_ERROR, NONFUNC, MISSING_DATA, HOT, COLD,
NONLINEAR, PERSIST, BLOOM, SNOWBALL, HALO, SATELLITE_HALO` are excluded from
fits (the list IRSA's spectrophotometry tool ignores). `TRANSIENT, OVERFLOW,
OUTLIER, DICHROIC, CROSSTALK, GHOST*, STREAK, PHANMISS, PHANTOM, REFERENCE` are
carried as warnings. `OVERFLOW` and `NONLINEAR` at a source peak mean
saturation risk. Flag names a product does not define are reported.

## 3. Background (`engine/background.py`)

*Plain language:* the sky behind sources is not zero (zodiacal light, for
example). We estimate it from pixels that contain no sources.

Sigma-clipped median of usable pixels (`SOURCE`-flagged pixels removed). For
images at least two mesh cells wide, it uses a mesh of such medians (default
32 px), filtered with a 3x3 median and interpolated bilinearly. The
uncertainty of the level, sqrt(pi/2) rms / sqrt(n), is propagated into flux
errors.

## 4. Detection and measurement (`engine/detection.py`)

*Plain language:* we slide the PSF shape across the image and ask, at every
pixel, "how strongly does the data look like a point source here, compared
with the noise?" Peaks above a threshold become detections. We then fit each
one precisely to get its brightness and position, with uncertainties.

Model: `d_i = B_i + A P_i(x, y) + n_i` with noise variance `V_i` from the
VARIANCE plane and weights `w_i = 1/V_i` (0 for masked pixels).

- **Matched filter:** `A_hat = sum(w P r)/sum(w P^2)` and
  `sigma_A = (sum w P^2)^(-1/2)`, so `SNR = A_hat/sigma_A`, computed at every
  pixel by correlation. Peaks are local maxima with SNR >= 5 (default), away
  from edges and masked pixels, at least 2 px apart.
- **PSF fit:** Gauss-Newton on (A, x, y), with the PSF re-rendered at each
  trial position. The covariance is the inverse Fisher matrix `(J^T W J)^-1`,
  with background uncertainty added to `var(A)`. Neighbours within the stamp
  are modelled and subtracted (two deblending passes). If the fit does not
  converge, moves more than 1.5 px or gives A <= 0, the amplitude is measured
  at the peak and the position is marked unusable for astrometry.
- **Flux:** `F = A x Omega_pix`, converted to microjansky. Omega comes from the
  solid-angle map if supplied, otherwise `OMEGA_MEDIAN` (the median of that
  map), otherwise the WCS pixel scale (flagged as an approximation).
- **Forced photometry:** the amplitude is fitted at a fixed sky position (used
  for light curves, spectra and non-detections).
- **Grades (provisional):** D means saturation flags at the peak. C means a
  warning flag at the peak, a centroid fallback, reduced chi-square above 3,
  or SNR below 10. B means warning flags in the footprint or a neighbour within
  3 px. A is everything else.
- **Diagnostics:** pixel-scale and PSF-scale noise ratios are the robust
  standard deviation of residual/sigma, and of the matched-filter SNR at empty
  positions. Both should be about 1 if VARIANCE describes the noise. Each
  carries a standard error of `1.166 x ratio/sqrt(n)`. A ratio counts as
  miscalibrated only when it lies more than 2 standard errors outside the
  0.8-1.25 bounds; an imprecise ratio is "not established". A nominal
  false-positive count is also reported (independent-footprint approximation;
  it underestimates peaks at low thresholds).

PSF models, in order of preference (IRSA guidance): the embedded `EPSF` table
(pipeline version 7 or later), the QR3 ePSF calibration product, the legacy
`PSF` cube (integrated onto pixels; an approximation), or a Gaussian from
`PSF_FWHM` (an approximation).

*Assumptions:* independent Gaussian pixel noise; a correct PSF; no unmodelled
neighbour. *Measured limitation:* SPHEREx's PSF is undersampled (FWHM about
0.85 px), so an integer-centred matched filter loses signal at sub-pixel
phases. With a 5-sigma threshold, injected sources reached 90% completeness
only at SNR about 7.6, both in synthetic images and in real QR2 cutouts (about
256 uJy there; `VERIFICATION_REAL_DATA.md`).

## 5. Association (`engine/association.py`)

*Plain language:* deciding which detections in different images are the same
source. Two detections match if their separation is small compared with both
position uncertainties plus image registration error.

`d^2 = delta^T (C_i + C_j + 2 s^2 I)^-1 delta <= chi2_2^-1(0.9973)` (about
11.8), with registration floor s = 0.4 arcsec (the upper end of the documented
0.1-0.4 arcsec median precision for FINAST=0). Pairs come from a k-d tree;
connected components become sources. A component with two detections from one
image is split brightest first (method 1.1.0): each detection joins the nearest
group within the gate (measured against the group's mean) that has no
detection from its image, or starts a new group. When a detection was within
the gate of a group that already had its image, the group is marked
**CONFLICT** with the alternative grouping recorded, and the new group is
**RELEASED_FROM_CONFLICT**. A member outside the gate from the mean is
**CHAINED**. Ambiguity is never resolved silently.

*Why the split:* on real 0.3-degree cutouts, PSF-fit residuals of bright stars
produced extra detections that chained stars to neighbours. The earlier rule
(keep the detection closest to the component mean) then released every
detection of some bright stars as single-image sources: one G = 13.9 star
became about 70 of them. With the split, sources with one detection fell from
772 to 287, and the Gaia stars with more than one source within 2 arcsec from
20 to 10 (at most 4 each).

## 6. Spectra (`engine/spectral.py`)

*Plain language:* a source's spectrum is its brightness at each wavelength
SPHEREx happened to sample. We list those measurements in wavelength order; we
do not invent the missing ones.

Each point keeps observation, time, detector, wavelength, bandwidth,
wavelength source (`WCS-WAVE`, about 1 nm accuracy, or `CWAVE/CBAND`), flux,
uncertainty, flags and usability. Coverage per detector band is `MEASURED`,
`COVERED_NOT_USABLE` or `NOT_IN_SELECTION`. None of these states is a measured
absence of a feature. The descriptive outputs are per-band weighted means and
a power-law slope (log F against log lambda, SNR >= 3 points).
`compare_point_sets` compares two sets (for example two epochs) only at
matched wavelengths: same detector, `|dlambda| <= 0.5 x bandwidth`. The
statistic is `chi2 = sum z^2` and a release mismatch makes the comparison
`INCOMPATIBLE`. No physical property (temperature, composition) is inferred.

## 7. Time: epochs, light curves and playback (`engine/temporal.py`)

*Plain language:* SPHEREx revisits each part of the sky in bursts a few days
long, about every six months. A light curve may only compare measurements
taken at the same wavelength.

- Epochs: observations closer than 5 days form one epoch.
- Comparable series: same detector and release, wavelengths within
  0.5 x bandwidth.
- Variability: `chi2 = sum((F_i - F_bar)^2 / (sigma_i^2 + (f_sys F_i)^2))`,
  dof n-1. It needs at least 3 points in at least 2 epochs. A leave-one-out
  repeat exposes single-point excesses. Per source, the most significant series
  is Bonferroni-corrected for the number of series tested.
- Playback: frames are the real observations in time order with footprints,
  detections and measurements. Nothing is interpolated or invented.

## 8. Positional change and parallax (`engine/motion.py`)

*Plain language:* if an object moves, its positions over time do not agree
with a single point. A nearby solar-system body also appears to wobble once a
year because the observer orbits the Sun: that wobble (parallax) gives its
distance.

- **Stationarity test (primary):** chi-square of a fixed-position model, dof
  2(n-1), using covariances with the registration floor.
- **Linear motion:** generalized least squares (GLS) of `p = p0 + mu t`. The
  improvement `Delta chi2 ~ chi2_2` is reported descriptively. A purely
  parallactic mover sampled at 0, 6 and 12 months can show a small
  `Delta chi2`, which is why stationarity is the primary statistic.
- **Parallax-motion fit:** `p_i = p0 + mu t_i + varpi f_i`, where
  `f_i = -(R_i . e, R_i . n)` and `R_i` is the observer's heliocentric position
  (ERFA ephemeris plus the spacecraft offset). Distance is
  `r = 206264.8"/varpi`, with an interval from varpi +/- 2 sigma. It is
  linearised: tests show truncation error of order 1/r (0.2% at 150 AU).
- **Empirical astrometric error model:** checks the position error model
  against the field's own sources. For sources with at least 6 astrometric
  detections, residuals about each source's mean give
  `scale = sqrt(sum chi2 / sum dof)` (1 when errors are right) with standard
  error `scale/sqrt(2 dof)`; sources above 5x the median are excluded as gross
  outliers. Per-image mean residuals estimate registration offsets. Motion and
  track candidates must pass this check (critical). If the scale is
  significantly above 1.25 and the candidate's stationarity statistic,
  recomputed with covariances times scale^2, drops below the watch threshold,
  "position errors underestimated" is a supported explanation. Real QR2 data
  gave scale 1.34 +/- 0.04, concentrated in confused sources
  (`VERIFICATION_REAL_DATA.md`).

## 9. Transients and moving objects (`engine/linking.py`)

*Plain language:* something seen in only one epoch is either a short-lived
event, an artefact, or a moving object that was elsewhere at other times. We
test the first by measuring the same spot at the same wavelength in other
images, and the last by linking such sources across epochs along physically
possible paths.

- **Transient test:** the probe is the strongest member detection. References
  are other covering images (same detector and release, wavelength within
  tolerance), with forced fluxes combined by weighted mean.
  `z = (F_probe - F_ref)/sqrt(sigma^2 + sigma_ref^2)`. The source is
  `TRANSIENT_LIKE` when z > 0 and `z^2 ~ chi2_1` gives >= 5 sigma.
- **Linking (distant-body search):** only transient-like *tracklets* are
  linked: at least 2 astrometric detections at distinct times, with no artefact
  flags. The pair separation must be at most the motion bound for r >= r_min
  (section 10) plus 3 sigma. Kept chains need a parallax-motion fit with dof
  >= 3, p >= 0.01, and parallax sigma <= parallax(r_max)/3. Chains are
  selected greedily (more epochs, then more points, then better fit). Losing
  chains are recorded, and only an equally long competitor makes a track
  ambiguous.
- **Distant-body rate bound (rules `planetx-1.3.0`).** A tracklet whose own
  apparent rate, from a weighted linear fit with the detections' covariances,
  exceeds with a 3-sigma margin the largest one-day motion of any bound body
  at r >= r_min (41.7 arcsec/day) is not eligible; it is recorded in
  `not_linked` with the measured rate. A rate that cannot be estimated never
  rejects.
- **Split tracklets (rules `planetx-1.3.0`).** A moving body is often split by
  the stationary-source association into several tracklets of one epoch. If a
  chain contains a tracklet from an epoch already covered by an accepted track
  and that tracklet is consistent with the track's fit (incremental
  chi-square p >= `link_gof_min_p`), the chain is not reported as an
  independent track; it is listed in the accepted track's
  `diagnostics.absorbed_chains` with the foreign tracklets it contained.
  Absorption checks count toward `max_link_evaluations`. The split tracklet is
  not attached to the track (its detections are not added to the fit).
  Saved-evidence check (accepted tracks only, floor-only covariance, so a
  liberal upper bound): 37 of 37 split body tracklets absorbed, 0 of 10
  foreign tracklets, 0 of 595 tracklets of a different body.
- **Effective degrees of freedom (rules `planetx-1.2.0`, config 1.2.0).** The
  parallax-motion fit is a linear least-squares model with 5 parameters, so a
  chain of N detections has `2N - 5` degrees of freedom and its chi-square is
  exact under the null. That count includes components that carry no
  information about whether two tracklets belong together: a tracklet seen
  for minutes has a rate uncertainty of hundreds of arcsec/day, far above
  any parallax rate (at most 41.7 arcsec/day for r >= 100 AU), so the fit
  can absorb almost any partner. Simulation on the actual fit: a short body
  tracklet plus an unrelated slow tracklet passes `p >= 0.01` about 45% of
  the time, against 1% when the body tracklet's rate is measured.
  A chain is therefore also required to have
  `effective_dof = sum over tracklets of (2 + 2 if its rate is measured) - 5 >= min_link_dof`.
  A rate is measured when its 1-sigma, from a weighted linear fit with the
  detections' own covariances plus the registration floor, is at most
  `link_rate_info_fraction` x parallax(r_max) x 2 pi / yr (about 1.8
  arcsec/day). A tracklet never counts more than position + rate (4 numbers):
  extra points only test the noise model, and adding points to the partner
  does not make a wrong chain easier to reject (simulation: still accepted
  about 50%). If the covariance is missing or invalid the rate is reported as
  unavailable and the tracklet counts as unmeasured. `p >= 0.01`, `min_link_dof
  = 3` and the parallax-uncertainty limit are unchanged. Each accepted track
  carries `diagnostics`; the result carries the number of chains rejected only
  by this rule and the first examples. Limitation: in-tracklet curvature
  (informative for long tracklets of nearby bodies) is not counted, so the
  rule is conservative there.
- *Not linked:* bodies closer than r_min (asteroids move too fast for this
  search) and sources with no comparable reference image.
- **OUTLIER is not an artefact flag.** The SPHEREx pipeline sets OUTLIER
  ("Pixel flagged by Detect Outliers") where a pixel departs from the static
  sky, which is exactly what a moving or changing source does. On real QR2 data
  both OUTLIER-flagged "transients" of the first run were asteroid 368 Haidea.
  Treating OUTLIER as an artefact would have rejected every real mover, so it is
  recorded as evidence of change and never excludes a point from tracklets or
  lowers a quality grade. Artefact flags are TRANSIENT (a jump within one
  exposure's ramp), PERSIST, CROSSTALK, GHOST*, STREAK.

## 10. The Planet X hypothesis (`engine/planetx.py`)

*Plain language:* a distant planet would look like a faint source that moves
very slowly, mostly through the annual parallax wobble. Its size gives its
distance. We can test whether a measured mover fits a chosen distance range.
We cannot say that Planet X exists.

Scenario: r in [r_min, r_max] = [100, 2000] AU by default. This is an
assumption (tagged `A`) and is user-configurable.

Physics: `varpi = 206264.8"/r`, escape speed `v_esc = 2 pi sqrt(2/r)` AU/yr,
and observer displacement at most `min(6.39 dt, 2.04)` AU. The largest
apparent displacement is
`[min(6.39 dt, 2.04) + v_esc(r_min) dt]/(r_min - 1)` rad.

Requirements, each SATISFIED, NOT_SATISFIED or NOT_ASSESSABLE:
1. multi-epoch coverage;
2. positional change;
3. parallax at 3 sigma or more;
4. distance interval overlapping the scenario;
5. a bound orbit (transverse speed below escape speed);
6. parallax-motion fit quality;
7. not a star (parallax above 1" at 3 sigma; NOT_SATISFIED when the source
   coincides with a Gaia DR3 star);
8. not a known solar-system object (JPL SB Ident, section 13; NOT_SATISFIED on
   a match, NOT_ASSESSABLE when a query did not complete);
9. plausible brightness (*no validated 0.75-5 um model*);
10. validated significance;
11. independent confirmation (*always NOT_SATISFIED inside HEIMDALL*);
12. measurement reliable: every applicable critical measurement check passed
    (quality, association, blending, error models, independence). It is added
    by the reasoning; when not met, a would-be consistent candidate becomes
    `NOT_ASSESSABLE`.

Fit quality (requirement 6) needs at least 3 degrees of freedom, the same
minimum as linking: a good fit with 1 or 2 degrees of freedom tests almost
nothing (rules `planetx-1.2.0-provisional`).

Status: `NOT_APPLICABLE`, `NOT_ASSESSABLE`, `INCONSISTENT`, `UNCONSTRAINED`
or `CONSISTENT_REQUIRES_CONFIRMATION` (the best possible). A confirmed known
body or a Gaia star makes a candidate `INCONSISTENT`.

Region level: `NOT_ASSESSABLE` without a moving-object injection measurement
(a lack of candidates is not evidence of absence). With one it is
`COMPLETENESS_LIMITED`: a statement of how many injected bodies in the scenario
range were recovered (with a 95% Wilson interval, by flux), what fraction stayed
in the analysed area for two epochs, and how many real candidates are
consistent. It describes the sensitivity of this field, these epochs and this
configuration only. **No probability of Planet X is ever computed.**

## 11. Reasoning and categories (`engine/reasoning.py`)

*Plain language:* a large statistic only says the data disagree with a simple
model. Before paying attention we check the data quality, look for ordinary
explanations (artefacts, blends, calibration, saturation, noise) and record
what could not be checked.

Each candidate records:
- checks (critical or not, with applicability rules);
- alternatives, each `SUPPORTED`, `RULED_OUT`, `PLAUSIBLE_UNTESTED` or
  `NOT_ASSESSABLE`. A `SUPPORTED` or `RULED_OUT` alternative must cite its
  evidence;
- a hypothesis statement, plus a conclusion when warranted. A language guard
  rejects claim words such as "discovered", "confirmed" or "proof";
- evidence for and against, missing evidence, contradictions and assumptions;
- three separate fields: unusualness (the statistic), physical
  interpretation, and Planet X relevance.

Attention rules, in order: `EXPLAINED` (an ordinary explanation is supported)
-> `INCONCLUSIVE` (a critical check failed or was unresolved) -> `ATTEND`
(Z >= 5 and `VALIDATED_FOR_CONFIG`) -> `WATCH` (Z >= 5 but unvalidated, with
`cap_reason`; or 3 <= Z < 5) -> `INCONCLUSIVE`. Ambiguity, blending or a
release mismatch make an ordinary explanation *possible*. They fail critical
checks but do not count as *supported* explanations. Sources seen in one epoch
are assessed only by the transient test.

Error-model checks: `astrometric_error_model` (MOTION, TRACK) and
`photometric_error_model` (VARIABILITY, TRANSIENT; rules 1.3.0) are critical.
The photometric scale is `sqrt(median(chi2 / (dof x m_dof)))` over the field's
constant-flux tests, where `m_k = (1 - 2/(9k))^3` is the null median of
`chi2_k / k`, with a seeded bootstrap standard error. It measures how much more
the field's sources scatter than their stated flux errors allow (confusion,
correlated noise, PSF mismatch). The check fails when the scale exceeds 1.25 by
more than 2 standard errors. The statistic is then recomputed with errors
multiplied by the scale (`empirically_rescaled`). If it falls below the watch
level, `photometric_errors_underestimated` is supported (`EXPLAINED`);
otherwise the candidate stays `INCONCLUSIVE`. Without enough comparable series,
the check is unresolved.

Context checks (rules `reasoning-1.2.0-provisional` and later):
- `known_object_check` passes on `NO_MATCH` or `EXCLUDED_BY_MOTION`, fails on a
  match (the `known_solar_system_object` alternative is then `SUPPORTED` for a
  confirmed match, `PLAUSIBLE_UNTESTED` for a one-image match) and is
  unresolved when a query did not complete.
- A MOTION source matched to a single Gaia DR3 star: if the star's catalogued
  proper motion is below 10 mas/yr, the apparent motion is
  `apparent_motion_contradicted_by_catalogue` (`SUPPORTED`, so `EXPLAINED`).
  Otherwise HEIMDALL's linear motion is compared with Gaia's (chi-square, 2 dof):
  agreement supports `high_proper_motion_star`; disagreement supports the
  contradiction.
- `known_object` states: a body that matches only some of a subject's detection images gives `PROBABLE_MATCH` with `partial: true` (never
  `CONFIRMED_MATCH`), because the other detections are not explained and the subject is a mixed association; partial matches are not
  upgraded across subjects.
- A TRANSIENT at a Gaia star's position is a `catalogued_star` (a known star;
  the flux change is a variability or measurement question).

Independence (`not_single_image_driven`, critical for MOTION): the stationarity
test is repeated with each image left out. If the lowest significance drops
below the watch level, one image drives the result and `single_image_systematic`
is supported. Several detections in one image are never counted as independent
epochs, and a QR2/QR3 duplicate listing of one exposure is counted once
(newest release kept, the other recorded as `superseded_listings`).

Follow-up rank: candidates are ordered lexicographically by attention level
(ATTEND, WATCH, INCONCLUSIVE, NOT_ASSESSABLE, EXPLAINED), validated before
unvalidated, fewer failed then fewer unresolved critical checks, and higher
error-rescaled significance. Each candidate's `follow_up` records the deciding
values. It orders follow-up effort; it is not a probability.

## 12. Significance conventions (`engine/stats.py`)

p-values are upper-tail probabilities computed in log space (series or Lentz
continued fraction), so values below 1e-300 do not underflow. The two-sided
Gaussian equivalent is `Z = Q^-1(p/2)` (with one degree of freedom,
`Z = sqrt(chi2)`). A computed Z is not a validated significance; validity
comes from the validation registry (`docs/VALIDATION.md`).

## 13. External context: Gaia DR3 and known small bodies (`context/`)

*Plain language:* before calling something unusual, ask whether it is a star
that is already catalogued, or an asteroid whose position on that night is
already known.

Query states are never conflated: `COMPLETED` (the service answered; "no match"
is then a real result), `FAILED` (network, HTTP or malformed response; nothing
is concluded), `OFFLINE_NOT_CACHED`, `NOT_APPLICABLE` (synthetic data, which no
real catalogue describes) and `NOT_REQUESTED`. Every answer is cached, so an
offline rerun (`HEIMDALL_OFFLINE=1`) sees the same answer.

- **Gaia DR3** (IRSA TAP, `gaia_dr3_source`, G < 21): one cone per field. Star
  positions are propagated from `ref_epoch` to the source's mean epoch with the
  catalogued proper motion. A source matches when
  `d^2 = delta^T (C_src + C_star + s^2 I)^-1 delta <= chi2_2^-1(0.9973)`, where
  `C_star` includes position, proper-motion and parallax variance and `s` is the
  registration floor. No match does not mean "not a star": Gaia is incomplete
  near G = 21 and misses very red sources.
- **Registration against Gaia:** for uniquely matched stars measured at
  SNR >= 10, the mean offset (measured minus Gaia) per image, with its standard
  error and significance, is an external check of each image's astrometric
  solution (`context.registration_vs_gaia`).
- **Gaia stationary controls:** stars with catalogued proper motion below
  10 mas/yr are known not to move at SPHEREx precision. When available, they
  replace the "assumed stationary" controls of the motion null test.
- **JPL SB Identification** (`sb_ident.api`, version 1.1): one query per image
  that contains a transient, a track or a single-epoch mover, with the
  spacecraft's geocentric state vector from the header (`xobs`), the
  mid-exposure time (JD, UT), the image centre and half-width,
  `two-pass=true`, `mag-required=true` and `vmag-lim=21`. The single-image
  5-sigma depth is about AB 18.4 at 1.4 um and asteroids are at most about
  1.5 mag brighter in the near-infrared than in V, so fainter bodies cannot be
  detected. Bodies without magnitude parameters (mostly comets) are therefore
  not checked. Only second-pass (numerically integrated) bodies count; the
  first-pass rows are a coarse screening list. A detection matches when its
  separation is at most `sqrt(chi2_2^-1(0.9973))` times the combined sigma of
  the centroid, the registration floor and an assumed 1.0" ephemeris error (the
  API gives no error for second-pass positions; the assumption is recorded).
- **Subject states:** `CONFIRMED_MATCH` (the same body in at least two images,
  including as separate single-image sources, because it moves between images),
  `PROBABLE_MATCH` (one image), `NO_MATCH` (every relevant query completed with
  nothing within tolerance), `NOT_ASSESSABLE` (a relevant query did not
  complete) and `EXCLUDED_BY_MOTION` (a stationary source detected at one
  position over at least the epoch gap: every known small body would have moved
  by far more, so no query is needed).


## Investigation and hypothesis evaluation (`engine/evidence.py`, `hypotheses.py`, `investigation.py`; investigation-1.0.0-provisional)

Runs after reasoning, reads only the serialized results (so a saved run can be re-investigated offline), runs no detection, and uses no LLM.

- **Evidence** is extracted from existing measurements (variability, transient test, astrometric fit, proper motion, parallax, tracks, tracklet-rate exclusion, spectral slope and comparisons, Gaia and JPL records, error models). Checks and validity are not evidence: the candidate gate (attention in ATTEND/WATCH and a cell that is not MISCALIBRATED) only decides whether a measured pattern is *admissible* for astrophysical hypotheses.
- **Dependency groups.** Items are one group if they share a fit, an external record, a declared dependency, or detections within the same measurement domain (flux with flux, position with position). Flux and position items sharing a detection stay separate groups (relation recorded); image-level error models are never merged (IMAGE_CORRELATED relation). Support is counted in groups, not items.
- **Hypotheses** (catalogue `hypotheses-1.0.0-provisional`): known small body, high-proper-motion star, stellar variability, extragalactic variability, astrophysical transient, unknown inner Solar System body, thermal dust/red spectrum, Planet X scenario, plus the existing instrumental and catalogue alternatives re-expressed (not recomputed). Each prediction returns SUPPORTS / CONTRADICTS / NEUTRAL / NOT_ASSESSABLE.
- **Status rules.** A decisive contradiction gives CONTRADICTED. A closed gate gives NOT_ASSESSABLE for astrophysical hypotheses. A required prediction that cannot be assessed gives NOT_ASSESSABLE (never a contradiction). Otherwise CONSISTENT, which means *not contradicted, not supported*. SUPPORTED is possible only for identification-type hypotheses (known body with a CONFIRMED_MATCH; Gaia proper-motion match) and the instrumental alternatives already assessed; astrophysical hypotheses are capped at CONSISTENT. No match in a catalogue never supports an unknown or extragalactic origin; a parallax distance contradicts the inner-body hypothesis only when its whole interval lies beyond r_min.
- **Not done:** no temperature or composition from colour or band labels (no photospheric reference exists, so the thermal hypothesis is NOT_ASSESSABLE); no probability or score; within-epoch light curves, morphology, variability classification and periods do not exist.
- **Planet X** is one scenario wrapped from the existing requirement evaluation (status CONSISTENT_REQUIRES_CONFIRMATION at most). Unmet confirmation or validation requirements are NEUTRAL, not contradictions. An unexplained pattern is never assumed to be Planet X.
