"""Transient-like sources and moving-object linking.

Transient test (sources seen in only one epoch). The highest-SNR member
detection with comparable reference images is the probe. References are
images that cover the position, are not among the source's detections, and
have the same detector and release with wavelength within tolerance x
bandwidth. With F_ref the weighted mean forced flux of the references:

    z = (F_probe - F_ref) / sqrt(sigma_probe^2 + sigma_ref^2),  statistic z^2 ~ chi2(1)

``TRANSIENT_LIKE``: z > 0 and Gaussian-equivalent >= ``transient_sigma``.
``NOT_ASSESSABLE``: no comparable reference image.

Linking (distant-body search). Inputs are transient-like *tracklets*: sources
with at least ``min_tracklet_points`` astrometric detections within one epoch
and no artefact flag at any member's peak (single-image artefacts are not
linked). A pair in different epochs is feasible when its separation is within
the motion bound for r >= r_min (``planetx.max_separation_arcsec``) plus
3 sigma. Chains with one tracklet per epoch are grown in time order. A chain is
kept when the five-parameter parallax-motion fit of all member positions has
at least ``min_link_dof`` degrees of freedom, goodness-of-fit
p >= ``link_gof_min_p``, and a parallax uncertainty small enough to tell the
far end of the scenario from zero (sigma <= parallax(r_max) / 3). Chains that
are subsets of kept chains are dropped; tracklets shared by several kept
chains make them ambiguous. Evaluation stops at ``max_link_evaluations``,
a safety bound; reaching it sets ``truncated`` and a note, and the result is then incomplete.

Eligibility also requires that the tracklet's own apparent rate (weighted linear fit, 3 sigma margin) does not exceed the maximum
one-day rate of any body at r >= r_min; faster tracklets are recorded as not linked with that reason.

Split tracklets: when a candidate chain contains a tracklet from the same epoch as an accepted track and that tracklet is consistent with
the accepted track's fit (incremental chi-square p >= link_gof_min_p), the chain is not reported as a separate track; it is listed in the
accepted track's ``diagnostics.absorbed_chains``.

Effective degrees of freedom: a kept chain must also have ``effective_dof >= min_link_dof``, where each tracklet counts 2 numbers
(position) plus 2 (linear rate) only if its rate is measured to within ``link_rate_info_fraction`` x the weakest annual-parallax
rate of the scenario, minus the 5 fitted parameters. Raw dof counts noise-dominated components; chains of an unmeasured tracklet
plus an unrelated one pass the chi-square test about half the time (see docs/SCIENCE_METHODS.md).

Not linked: bodies closer than r_min (asteroids move too fast for this
search), and sources without comparable references.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .association import SourceCluster
from .config import EngineConfig
from .detection import DetectionRecord, ForcedMeasurement
from .config import ARTEFACT_FLAGS
from .motion import MotionFit, ParallaxFit, motion_test, parallax_fit
from .planetx import max_separation_arcsec, parallax_arcsec
from .skygeom import angular_separation_deg, offsets_arcsec
from .stats import Significance, chi2_significance, weighted_mean


@dataclass
class TransientTest:
    source_id: str
    status: str  # TRANSIENT_LIKE | NOT_TRANSIENT | NOT_ASSESSABLE
    reasons: list[str]
    probe_det_id: str | None = None
    probe_obs_id: str | None = None
    probe_flux_ujy: float | None = None
    probe_sigma_ujy: float | None = None
    probe_wavelength_um: float | None = None
    n_references: int = 0
    reference_flux_ujy: float | None = None
    reference_sigma_ujy: float | None = None
    z: float | None = None
    significance: Significance | None = None
    references: list[dict] = field(default_factory=list)


def transient_test(cluster: SourceCluster, members: list[DetectionRecord], forced: list[ForcedMeasurement],
                   image_release: dict[str, str], config: EngineConfig) -> TransientTest:
    tol = config.temporal.wavelength_tolerance_fraction
    detected = set(cluster.obs_ids)
    probes = sorted(members, key=lambda d: -d.snr_fit if math.isfinite(d.snr_fit) else 0.0)
    for probe in probes:
        if probe.wavelength_um is None or not math.isfinite(probe.wavelength_um) or not probe.bandwidth_um:
            continue
        refs = [f for f in forced if f.status == "MEASURED" and f.obs_id not in detected
                and f.detector == probe.detector and image_release.get(f.obs_id) == image_release.get(probe.obs_id)
                and f.wavelength_um is not None and math.isfinite(f.wavelength_um) and f.bandwidth_um
                and abs(f.wavelength_um - probe.wavelength_um) <= tol * min(f.bandwidth_um, probe.bandwidth_um)]
        if not refs:
            continue
        wm = weighted_mean(np.array([f.flux_ujy for f in refs]), np.array([f.flux_sigma_ujy for f in refs]))
        z = (probe.flux_ujy - wm.mean) / math.hypot(probe.flux_sigma_ujy, wm.sigma)
        sig = chi2_significance(z * z, 1)
        transient = z > 0 and sig.sigma_equivalent >= config.significance.transient_sigma
        return TransientTest(
            source_id=cluster.source_id, status="TRANSIENT_LIKE" if transient else "NOT_TRANSIENT", reasons=[],
            probe_det_id=probe.det_id, probe_obs_id=probe.obs_id, probe_flux_ujy=probe.flux_ujy,
            probe_sigma_ujy=probe.flux_sigma_ujy, probe_wavelength_um=probe.wavelength_um, n_references=len(refs),
            reference_flux_ujy=wm.mean, reference_sigma_ujy=wm.sigma, z=z, significance=sig,
            references=[{"obs_id": f.obs_id, "mjd": f.mjd, "wavelength_um": f.wavelength_um, "flux_ujy": f.flux_ujy,
                         "flux_sigma_ujy": f.flux_sigma_ujy} for f in refs])
    return TransientTest(source_id=cluster.source_id, status="NOT_ASSESSABLE", reasons=["NO_COMPARABLE_REFERENCE_IMAGE"])


@dataclass
class Track:
    track_id: str
    source_ids: list[str]
    det_ids: list[str]
    obs_ids: list[str]
    epoch_indices: list[int]
    n_points: int
    motion: MotionFit
    parallax: ParallaxFit
    ambiguous: bool = False
    shares_sources_with: list[str] = field(default_factory=list)
    link_rule: str = ""
    diagnostics: dict = field(default_factory=dict)  # effective-dof evidence for this accepted chain


@dataclass
class LinkingResult:
    tracks: list[Track]
    n_inputs: int
    n_feasible_pairs: int
    n_evaluations: int
    truncated: bool
    r_min_au: float
    notes: list[str]
    n_rejected_low_effective_dof: int = 0  # chains that passed every other criterion but had too few testable constraints
    rejected_examples: list[dict] = field(default_factory=list)  # first few such chains with their evidence (bounded)
    rate_scale_arcsec_day: float | None = None
    n_absorbed_chains: int = 0  # chains not reported as separate tracks because a tracklet belongs to an accepted track


def _positions(dets: list[DetectionRecord], floor2: float):
    usable = [d for d in dets if d.astrometry_usable and d.mjd is not None]
    ra = [d.ra for d in usable]
    dec = [d.dec for d in usable]
    cov = [d.cov() + floor2 * np.eye(2) for d in usable]
    return usable, ra, dec, cov


def tracklet_eligible(members: list[DetectionRecord], config: EngineConfig) -> tuple[bool, str]:
    usable = [d for d in members if d.astrometry_usable and d.mjd is not None]
    if len(usable) < config.planetx.min_tracklet_points:
        return False, f"fewer than {config.planetx.min_tracklet_points} astrometric detections in its epoch"
    if len({round(d.mjd, 5) for d in usable}) < 2:
        return False, "detections at a single time: no within-epoch motion can be measured"
    flagged = sorted({f for d in members for f in d.peak_flags if f in ARTEFACT_FLAGS})
    if flagged:
        return False, f"artefact flags at peak: {flagged}"
    too_fast, why = tracklet_rate_exceeds_bound(members, config)
    if too_fast:
        return False, why
    return True, ""


MAX_REJECTION_EXAMPLES = 25
DAYS_PER_YEAR = 365.25


def rate_scale_arcsec_per_day(config: EngineConfig) -> float:
    """Weakest annual-parallax apparent rate of the scenario: parallax(r_max) x 2 pi per year, scaled by link_rate_info_fraction."""
    p = config.planetx
    return parallax_arcsec(p.r_max_au) * 2.0 * math.pi / DAYS_PER_YEAR * p.link_rate_info_fraction


def _rate_fit(dets: list[DetectionRecord], floor2: float):
    """Weighted linear fit x(t) = x0 + v t of a tracklet's usable detections (sky offsets in arcsec, t in days).

    Uses the covariances the parallax fit itself uses (``cov() + floor^2 I``). Returns ``(v, cov_v, "")`` or ``(None, None, reason)``;
    unavailable or non-positive-definite covariances are reported, never replaced by assumed values.
    """
    usable = [d for d in dets if d.astrometry_usable and d.mjd is not None]
    if len(usable) < 2:
        return None, None, "FEWER_THAN_2_ASTROMETRIC_DETECTIONS"
    times = np.array([d.mjd for d in usable], dtype=float)
    if not np.ptp(times) > 1e-9:
        return None, None, "SINGLE_TIME"
    tau = times - float(times.mean())
    ref = usable[0]
    info = np.zeros((4, 4))
    rhs = np.zeros(4)
    eye = np.eye(2)
    for d, ti in zip(usable, tau):
        try:
            c = np.asarray(d.cov(), dtype=float) + floor2 * eye
            if c.shape != (2, 2) or not np.all(np.isfinite(c)) or np.min(np.linalg.eigvalsh((c + c.T) / 2.0)) <= 0.0:
                return None, None, "COVARIANCE_UNAVAILABLE_OR_NOT_POSITIVE_DEFINITE"
            w = np.linalg.inv(c)
        except (TypeError, ValueError, np.linalg.LinAlgError):
            return None, None, "COVARIANCE_UNAVAILABLE_OR_NOT_POSITIVE_DEFINITE"
        x = np.concatenate([eye, ti * eye], axis=1)  # 2 x 4
        y = np.asarray(offsets_arcsec(d.ra, d.dec, ref.ra, ref.dec), dtype=float)
        info += x.T @ w @ x
        rhs += x.T @ w @ y
    try:
        cov = np.linalg.inv(info)
    except np.linalg.LinAlgError:
        return None, None, "RATE_NOT_ESTIMABLE"
    return (cov @ rhs)[2:], cov[2:, 2:], ""


def tracklet_rate_precision(dets: list[DetectionRecord], floor2: float) -> tuple[float | None, str]:
    """1-sigma uncertainty (arcsec/day, worst principal axis) of a tracklet's apparent rate, or (None, reason)."""
    v, cov, why = _rate_fit(dets, floor2)
    if v is None:
        return None, why
    return float(math.sqrt(max(float(np.max(np.linalg.eigvalsh(cov))), 0.0))), ""


def tracklet_rate_exceeds_bound(dets: list[DetectionRecord], config: EngineConfig) -> tuple[bool, str]:
    """True when the tracklet's apparent rate is, with 3 sigma margin, above what any bound body at r >= r_min can show.

    The bound is the largest rate over one day (observer reflex at Earth's maximum speed plus the escape speed at r_min). A rate that
    cannot be estimated never rejects.
    """
    v, cov, _ = _rate_fit(dets, config.association.registration_floor_arcsec ** 2)
    if v is None:
        return False, ""
    rate = float(np.hypot(*v))
    sigma = float(math.sqrt(max(float(np.max(np.linalg.eigvalsh(cov))), 0.0)))
    bound = max_separation_arcsec(1.0, config.planetx.r_min_au)
    if rate - 3.0 * sigma > bound:
        return True, (f"apparent rate {rate:.1f} +/- {sigma:.1f} arcsec/day exceeds the maximum {bound:.1f} arcsec/day of any body at "
                      f"r >= {config.planetx.r_min_au:g} AU (not a distant-body tracklet)")
    return False, ""


def effective_dof(informative: list[bool]) -> int:
    """Testable constraints of a chain: 2 numbers (position) per tracklet plus 2 (linear rate) if the rate is measured, minus 5 parameters.

    Further points of a tracklet only test the noise model or in-tracklet curvature; they do not link epochs, so a tracklet never
    counts more than position + rate (4 numbers).
    """
    return sum(2 + (2 if inf else 0) for inf in informative) - 5


def link_moving(inputs: list[tuple[SourceCluster, list[DetectionRecord], int]], observer_au: dict[str, np.ndarray],
                config: EngineConfig) -> LinkingResult:
    """Link transient-like tracklets from different epochs into candidate tracks."""
    p = config.planetx
    floor2 = config.association.registration_floor_arcsec ** 2
    max_sigma_plx = parallax_arcsec(p.r_max_au) / 3.0
    items = sorted(inputs, key=lambda t: (t[2], t[0].source_id))
    n = len(items)
    mean_mjd = [float(np.mean([d.mjd for d in dets if d.mjd is not None])) for _, dets, _ in items]
    sigma = [math.sqrt(max(np.linalg.eigvalsh(np.asarray(c.cov_arcsec2))[-1], 0.0) + floor2) for c, _, _ in items]
    rate_scale = rate_scale_arcsec_per_day(config)
    rate_info = [tracklet_rate_precision(dets, floor2) for _, dets, _ in items]
    informative = [sg is not None and sg <= rate_scale for sg, _ in rate_info]
    succ: dict[int, list[int]] = {i: [] for i in range(n)}
    n_pairs = 0
    for i in range(n):
        for j in range(n):
            if items[j][2] <= items[i][2]:
                continue
            sep = float(angular_separation_deg(items[i][0].ra, items[i][0].dec, items[j][0].ra, items[j][0].dec)) * 3600.0
            allowed = max_separation_arcsec(mean_mjd[j] - mean_mjd[i], p.r_min_au) + 3.0 * math.hypot(sigma[i], sigma[j])
            if sep <= allowed:
                succ[i].append(j)
                n_pairs += 1

    evaluations = 0
    truncated = False
    kept: dict[tuple[int, ...], tuple[MotionFit, ParallaxFit]] = {}
    n_rejected_eff = 0
    rejected_examples: list[dict] = []

    def chain_diagnostics(chain, plx):
        return {"effective_dof": effective_dof([informative[k] for k in chain]), "raw_dof": plx.dof, "rate_scale_arcsec_day": rate_scale,
                "rule": "effective_dof >= min_link_dof; a tracklet's rate is informative if its sigma <= rate_scale",
                "tracklets": [{"source_id": items[k][0].source_id, "epoch": items[k][2], "n_points": len([d for d in items[k][1] if d.astrometry_usable]),
                               "rate_sigma_arcsec_day": rate_info[k][0], "informative": informative[k], "unavailable_reason": rate_info[k][1] or None}
                              for k in chain]}

    def evaluate(chain: tuple[int, ...]):
        dets = [d for k in chain for d in items[k][1]]
        usable, ra, dec, cov = _positions(dets, floor2)
        obs = np.array([observer_au[d.obs_id] for d in usable]) if usable else np.zeros((0, 3))
        plx = parallax_fit(ra, dec, cov, [d.mjd for d in usable], obs)
        return plx, usable

    def grow(chain: tuple[int, ...]):
        nonlocal evaluations, truncated, n_rejected_eff
        for j in succ[chain[-1]]:
            if evaluations >= p.max_link_evaluations:
                truncated = True
                return
            new = chain + (j,)
            evaluations += 1
            plx, usable = evaluate(new)
            if plx.status == "COMPLETED" and plx.gof is not None and plx.gof.p_value < p.link_gof_min_p:
                continue
            raw_ok = (plx.status == "COMPLETED" and plx.dof is not None and plx.dof >= p.min_link_dof
                      and plx.gof is not None and plx.parallax_sigma_arcsec <= max_sigma_plx)
            constrained = raw_ok and effective_dof([informative[k] for k in new]) >= p.min_link_dof
            if raw_ok and not constrained:
                n_rejected_eff += 1
                if len(rejected_examples) < MAX_REJECTION_EXAMPLES:
                    rejected_examples.append({**chain_diagnostics(new, plx), "n_points": plx.n_points, "p_value": plx.gof.p_value,
                                              "parallax_sigma_arcsec": plx.parallax_sigma_arcsec})
            if constrained:
                mt = motion_test([d.ra for d in usable], [d.dec for d in usable],
                                 [d.cov() + floor2 * np.eye(2) for d in usable], [d.mjd for d in usable])
                kept[new] = (mt, plx)
            grow(new)

    for i in range(n):
        grow((i,))

    # Greedy selection: more epochs, then more points, then better fit. A chain that shares a tracklet with an
    # accepted track is recorded as a competing link; only an equally long competitor makes the track ambiguous.
    def rank(c):
        plx = kept[c][1]
        return (-len(c), -plx.n_points, -(plx.gof.p_value if plx.gof else 0.0), c)

    accepted: list[tuple[int, ...]] = []
    competitors: dict[tuple[int, ...], list[tuple[int, ...]]] = {}
    absorbed: dict[tuple[int, ...], list[dict]] = {}
    belongs_cache: dict[tuple[tuple[int, ...], int], bool] = {}
    n_absorbed = 0

    def belongs(a: tuple[int, ...], j: int) -> bool:
        """Tracklet j (same epoch as a tracklet of accepted chain a) is consistent with a's fit: incremental chi-square p >= link_gof_min_p."""
        nonlocal evaluations, truncated
        if (a, j) not in belongs_cache:
            if evaluations >= p.max_link_evaluations:
                truncated = True  # ownership not examined: the chain stays a separate (unabsorbed) track
                return False
            evaluations += 1
            comb, _ = evaluate(a + (j,))
            acc = kept[a][1]
            ok = False
            if comb.status == "COMPLETED" and comb.chi2 is not None and acc.chi2 is not None and comb.dof is not None and acc.dof is not None \
                    and comb.dof > acc.dof:
                inc = chi2_significance(max(comb.chi2 - acc.chi2, 0.0), comb.dof - acc.dof)
                ok = inc.p_value >= p.link_gof_min_p
            belongs_cache[(a, j)] = ok
        return belongs_cache[(a, j)]

    for c in sorted(kept, key=rank):
        owner, reason, via = next(((a, "shares_tracklet", None) for a in accepted if set(a) & set(c)), (None, None, None))
        if owner is None:
            # A split tracklet: another tracklet of the same epoch as an accepted track's, consistent with that track's fit. It is the
            # same object seen in a second piece, so a chain through it must not be reported as an independent track.
            for a in accepted:
                a_epochs = {items[i][2] for i in a}
                via = next((j for j in c if items[j][2] in a_epochs and belongs(a, j)), None)
                if via is not None:
                    owner, reason = a, "tracklet_consistent_with_accepted_track"
                    break
        if owner is None:
            accepted.append(c)
            competitors[c] = []
            absorbed[c] = []
        elif reason == "shares_tracklet":
            competitors[owner].append(c)
        else:
            n_absorbed += 1
            absorbed[owner].append({"source_ids": [items[i][0].source_id for i in c], "epochs": [items[i][2] for i in c],
                                    "consistent_tracklet": items[via][0].source_id, "reason": reason})
    tracks = []
    for k, c in enumerate(sorted(accepted)):
        mt, plx = kept[c]
        dets = [d for i in c for d in items[i][1]]
        rivals = competitors[c]
        tracks.append(Track(
            track_id=f"T{k:04d}", source_ids=[items[i][0].source_id for i in c], det_ids=[d.det_id for d in dets],
            obs_ids=sorted({d.obs_id for d in dets}), epoch_indices=[items[i][2] for i in c],
            n_points=len([d for d in dets if d.astrometry_usable]), motion=mt, parallax=plx,
            ambiguous=any(len(r) >= len(c) for r in rivals), diagnostics={**chain_diagnostics(c, plx), "absorbed_chains": absorbed[c][:MAX_REJECTION_EXAMPLES],
                                                                                           "n_absorbed_chains": len(absorbed[c])},
            shares_sources_with=[",".join(items[i][0].source_id for i in r) for r in rivals],
            link_rule=(f"motion bound for r >= {p.r_min_au:g} AU; parallax-motion fit p >= {p.link_gof_min_p:g}, "
                       f"dof >= {p.min_link_dof}, parallax sigma <= {max_sigma_plx:.1f} arcsec; greedy by epochs, "
                       f"points, fit")))
    notes = []
    if truncated:
        notes.append(f"linking stopped after {p.max_link_evaluations} evaluations; some chains were not evaluated")
    return LinkingResult(tracks=tracks, n_inputs=n, n_feasible_pairs=n_pairs, n_evaluations=evaluations,
                         truncated=truncated, r_min_au=p.r_min_au, notes=notes, n_rejected_low_effective_dof=n_rejected_eff,
                         rejected_examples=rejected_examples, rate_scale_arcsec_day=rate_scale, n_absorbed_chains=n_absorbed)
