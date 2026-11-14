"""Matched-filter detection, PSF-fit photometry and astrometry, forced photometry.

Image model (per pixel i):  d_i = B_i + A * P_i(x, y) + n_i,  n_i ~ N(0, V_i)

- B: background (``background.py``); V: the product's VARIANCE plane.
- P: PSF rendered on native pixels with unit total flux (``psf.py``).
- A: amplitude in MJy/sr. It is the surface-brightness sum over the PSF, so the
  flux density is F = A * Omega_pix (pixel solid angle, sr); 1 MJy = 1e12 uJy.

Matched filter (known PSF, independent Gaussian noise, w_i = 1/V_i, 0 if masked):
    A_hat(k) = sum w P r / sum w P^2,   sigma_A(k) = (sum w P^2)^-1/2,
    SNR(k) = A_hat / sigma_A, evaluated at every pixel k by correlation with
    the PSF centred on that pixel (r = d - B). Peaks: local maxima with
    SNR >= threshold, away from edges and masked pixels, separated by at least
    ``min_separation_px`` (stronger peak kept).

Measurement: weighted least squares of (A, x, y) by Gauss-Newton, with the
PSF re-rendered at each trial position (numerical derivatives). The parameter
covariance is the inverse Fisher matrix (J^T W J)^-1. The background-level
uncertainty adds (sigma_B * sum w P / sum w P^2)^2 to var(A). If the fit does
not converge, moves more than ``max_centroid_shift_px`` or gives A <= 0, the
amplitude is fitted at the initial position and the astrometry is marked
unusable.

Assumptions: Gaussian, independent pixel noise described by VARIANCE; the PSF
model is correct; no unresolved neighbour within the stamp. Violations are
probed by the noise-ratio diagnostics, the reduced chi-square of each fit and
the validation procedures. Photon noise of the source itself is included only
to the extent the VARIANCE plane includes it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import correlate, maximum_filter
from scipy.special import ndtr

from ..ingest.spherex import SpectralImage
from .background import Background, estimate_background
from .config import SATURATION_FLAGS, EngineConfig, DetectionParams
from .psf import PsfModel
from .skygeom import offsets_arcsec
from .stats import mad_std

DETECTION_METHOD_ID = "matched_filter_psf_fit"
DETECTION_METHOD_VERSION = "1.0.0"
MJY_TO_UJY = 1e12
# Relative standard error of a MAD-based standard deviation for Gaussian data: 1 / sqrt(2 x 0.3675).
MAD_STD_RELATIVE_SE = 1.1664


@dataclass
class ImageAnalysis:
    """Per-image working state: background-subtracted residual and weights (array coordinates)."""

    image: SpectralImage
    background: Background
    residual: np.ndarray
    weight: np.ndarray
    bad: np.ndarray


def prepare(img: SpectralImage, config: EngineConfig) -> ImageAnalysis:
    bad = img.bad_mask()
    background = estimate_background(img.image, bad, img.masks.background_excluded(img.flags), config.detection)
    residual = np.where(bad, 0.0, img.image - background.level)
    weight = np.zeros_like(img.image)
    weight[~bad] = 1.0 / img.variance[~bad]
    return ImageAnalysis(image=img, background=background, residual=residual, weight=weight, bad=bad)


def _cut(an: ImageAnalysis, x_ll: int, y_ll: int, n: int, sign: float = 1.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ox, oy = an.image.origin
    ny_a, nx_a = an.residual.shape
    r = np.zeros((n, n))
    w = np.zeros((n, n))
    flags = np.zeros((n, n), dtype=np.int64)
    ax0, ay0 = x_ll - ox, y_ll - oy
    xs0, xs1 = max(ax0, 0), min(ax0 + n, nx_a)
    ys0, ys1 = max(ay0, 0), min(ay0 + n, ny_a)
    if xs0 < xs1 and ys0 < ys1:
        st = (slice(ys0 - ay0, ys1 - ay0), slice(xs0 - ax0, xs1 - ax0))
        im = (slice(ys0, ys1), slice(xs0, xs1))
        r[st] = sign * an.residual[im]
        w[st] = an.weight[im]
        flags[st] = an.image.flags[im]
    return r, w, flags


@dataclass
class PsfFit:
    x: float
    y: float
    amp: float
    cov: np.ndarray  # (amp, x, y); position terms are zero for fixed-position fits
    chi2: float
    dof: int
    n_pix: int
    masked_fraction: float
    status: str  # CONVERGED | FIXED_POSITION | FIXED_POSITION_FALLBACK | FAILED
    iterations: int
    sum_wp: float
    sum_wp2: float
    footprint_flags: int
    note: str = ""

    @property
    def chi2_red(self) -> float:
        return self.chi2 / self.dof if self.dof > 0 else math.nan


def _render_grad(psf: PsfModel, x: float, y: float, x_ll: int, y_ll: int, n: int, h: float = 0.02):
    p = psf.render(x, y, x_ll, y_ll, n, n)
    px = (psf.render(x + h, y, x_ll, y_ll, n, n) - psf.render(x - h, y, x_ll, y_ll, n, n)) / (2 * h)
    py = (psf.render(x, y + h, x_ll, y_ll, n, n) - psf.render(x, y - h, x_ll, y_ll, n, n)) / (2 * h)
    return p, px, py


def fit_psf(an: ImageAnalysis, x_init: float, y_init: float, params: DetectionParams, *,
            fixed: bool = False, sign: float = 1.0,
            neighbours: list[tuple[float, float, float]] | None = None) -> PsfFit:
    """Fit amplitude (and position unless ``fixed``) at an image position.

    ``neighbours`` are (x, y, amplitude) models of nearby sources, subtracted
    from the stamp before fitting (deblending).
    """
    psf = an.image.psf
    n = params.psf_stamp_px
    half = n // 2
    x_ll = int(math.floor(x_init + 0.5)) - half
    y_ll = int(math.floor(y_init + 0.5)) - half
    r, w, flags = _cut(an, x_ll, y_ll, n, sign)
    for xn, yn, an_amp in neighbours or ():
        r = r - an_amp * psf.render(xn, yn, x_ll, y_ll, n, n)
    good = w > 0
    p0 = psf.render(x_init, y_init, x_ll, y_ll, n, n)
    footprint = p0 >= params.footprint_level * p0.max() if p0.max() > 0 else np.zeros_like(good)
    masked_fraction = float(1.0 - good[footprint].mean()) if footprint.any() else 1.0
    footprint_flags = int(np.bitwise_or.reduce(flags[footprint].ravel())) if footprint.any() else 0

    def linear(p):
        swp2 = float(np.sum(w * p * p))
        if not swp2 > 0:
            return None
        return float(np.sum(w * p * r) / swp2), float(np.sum(w * p)), swp2

    def fixed_fit(status: str, note: str = "") -> PsfFit:
        lin = linear(p0)
        if lin is None:
            return PsfFit(x_init, y_init, math.nan, np.full((3, 3), math.nan), math.nan, 0, 0, masked_fraction,
                          "FAILED", 0, 0.0, 0.0, footprint_flags, "no usable pixels under the PSF")
        amp, swp, swp2 = lin
        m = int(good.sum())
        cov = np.zeros((3, 3))
        cov[0, 0] = 1.0 / swp2
        chi2 = float(np.sum(w * (r - amp * p0) ** 2))
        return PsfFit(x_init, y_init, amp, cov, chi2, m - 1, m, masked_fraction, status, 0, swp, swp2, footprint_flags, note)

    if fixed:
        return fixed_fit("FIXED_POSITION")
    lin = linear(p0)
    if lin is None:
        return fixed_fit("FAILED")
    amp = lin[0]
    if not amp > 0:
        return fixed_fit("FIXED_POSITION_FALLBACK", "non-positive initial amplitude")
    x, y = x_init, y_init
    converged = False
    iterations = 0
    for iterations in range(1, params.fit_max_iter + 1):
        p, px, py = _render_grad(psf, x, y, x_ll, y_ll, n)
        jac = np.stack([p[good], amp * px[good], amp * py[good]], axis=1)
        res = (r - amp * p)[good]
        jw = jac.T * w[good]
        try:
            delta = np.linalg.solve(jw @ jac, jw @ res)
        except np.linalg.LinAlgError:
            break
        if not np.all(np.isfinite(delta)):
            break
        step = math.hypot(delta[1], delta[2])
        if step > 0.5:
            delta[1:] *= 0.5 / step
        amp += float(delta[0])
        x += float(delta[1])
        y += float(delta[2])
        if not amp > 0:
            break
        if step < 1e-3:
            converged = True
            break
    if not converged or not amp > 0 or math.hypot(x - x_init, y - y_init) > params.max_centroid_shift_px:
        why = "did not converge" if not converged else ("non-positive amplitude" if not amp > 0 else "centroid moved beyond limit")
        return fixed_fit("FIXED_POSITION_FALLBACK", why)
    p, px, py = _render_grad(psf, x, y, x_ll, y_ll, n)
    jac = np.stack([p[good], amp * px[good], amp * py[good]], axis=1)
    try:
        cov = np.linalg.inv((jac.T * w[good]) @ jac)
    except np.linalg.LinAlgError:
        return fixed_fit("FIXED_POSITION_FALLBACK", "singular Fisher matrix")
    m = int(good.sum())
    chi2 = float(np.sum(w * (r - amp * p) ** 2))
    return PsfFit(x, y, amp, cov, chi2, m - 3, m, masked_fraction, "CONVERGED", iterations,
                  float(np.sum(w * p)), float(np.sum(w * p * p)), footprint_flags)


def sky_jacobian(img: SpectralImage, x: float, y: float, ra: float, dec: float) -> np.ndarray:
    """d(xi, eta)/d(x, y) in arcsec per pixel at an image position (xi east, eta north)."""
    d = 0.5
    ras, decs = img.pixel_to_world([x + d, x - d, x, x], [y, y, y + d, y - d])
    off = offsets_arcsec(ras, decs, ra, dec)
    return np.column_stack([(off[0] - off[1]) / (2 * d), (off[2] - off[3]) / (2 * d)])


def _amp_sigma(an: ImageAnalysis, fit: PsfFit) -> float:
    var = fit.cov[0, 0]
    if fit.sum_wp2 > 0:
        var += (an.background.level_sigma * fit.sum_wp / fit.sum_wp2) ** 2
    return math.sqrt(var) if var >= 0 else math.nan


@dataclass
class DetectionRecord:
    det_id: str
    obs_id: str
    x: float
    y: float
    ra: float
    dec: float
    cov_sky_arcsec2: list[list[float]]
    sigma_major_arcsec: float
    astrometry_usable: bool
    amplitude: float
    amplitude_sigma: float
    flux_ujy: float
    flux_sigma_ujy: float
    solid_angle_sr: float
    solid_angle_source: str
    snr_peak: float
    snr_fit: float
    chi2_red: float
    n_fit_pixels: int
    masked_fraction: float
    fit_status: str
    fit_note: str
    peak_flags: list[str]
    footprint_warning_flags: list[str]
    wavelength_um: float
    bandwidth_um: float
    wavelength_source: str | None
    mjd: float | None
    detector: int | None
    quality_grade: str = "U"
    quality_reasons: list[str] = field(default_factory=list)
    accepted: bool = True
    rejection_reasons: list[str] = field(default_factory=list)
    nearest_neighbour_px: float | None = None
    inverted: bool = False
    neighbours_subtracted: int = 0

    def cov(self) -> np.ndarray:
        return np.asarray(self.cov_sky_arcsec2, dtype=float)


def _record(an: ImageAnalysis, fit: PsfFit, snr_peak: float, det_id: str, inverted: bool) -> DetectionRecord:
    img = an.image
    ra, dec = (float(v) for v in img.pixel_to_world(fit.x, fit.y))
    usable = fit.status == "CONVERGED"
    if usable:
        cov_pix = fit.cov[1:, 1:]
    else:
        # Fallback: the integer-pixel peak is uncertain by about half a pixel.
        cov_pix = np.eye(2) * 0.25
    jac = sky_jacobian(img, fit.x, fit.y, ra, dec)
    cov_sky = jac @ cov_pix @ jac.T
    cov_sky = 0.5 * (cov_sky + cov_sky.T)
    amp_sigma = _amp_sigma(an, fit)
    omega, omega_src = img.pixel_solid_angle_sr(fit.x, fit.y)
    lam, bw, wsrc = img.wavelength_at(fit.x, fit.y)
    vocab = img.vocabulary
    peak = vocab.names_in(img.pixel_flags(fit.x, fit.y))
    fp_warn = vocab.names_in(fit.footprint_flags & img.masks.warning)
    return DetectionRecord(
        det_id=det_id, obs_id=img.obs_id, x=float(fit.x), y=float(fit.y), ra=ra, dec=dec,
        cov_sky_arcsec2=cov_sky.tolist(), sigma_major_arcsec=float(math.sqrt(max(np.linalg.eigvalsh(cov_sky)[-1], 0.0))),
        astrometry_usable=usable, amplitude=float(fit.amp), amplitude_sigma=amp_sigma,
        flux_ujy=float(fit.amp * omega * MJY_TO_UJY), flux_sigma_ujy=float(amp_sigma * omega * MJY_TO_UJY),
        solid_angle_sr=float(omega), solid_angle_source=omega_src, snr_peak=float(snr_peak),
        snr_fit=float(fit.amp / amp_sigma) if amp_sigma > 0 else math.nan, chi2_red=float(fit.chi2_red),
        n_fit_pixels=fit.n_pix, masked_fraction=fit.masked_fraction, fit_status=fit.status, fit_note=fit.note,
        peak_flags=list(peak), footprint_warning_flags=list(fp_warn),
        wavelength_um=lam, bandwidth_um=bw, wavelength_source=wsrc, mjd=img.meta.mjd_avg,
        detector=img.meta.detector, inverted=inverted,
    )


def quality_grade(rec: DetectionRecord, config: EngineConfig, blend_px: float) -> tuple[str, list[str]]:
    """Provisional A-D grade of one measurement (see docs/SCIENCE_METHODS.md)."""
    q = config.quality
    if any(f in SATURATION_FLAGS for f in rec.peak_flags):
        return "D", ["SATURATION_OR_NONLINEARITY_FLAG_AT_PEAK"]
    c = [f"WARNING_FLAG_AT_PEAK:{f}" for f in rec.peak_flags if f in q.warning_flags]
    if rec.fit_status != "CONVERGED":
        c.append("CENTROID_FIT_FALLBACK")
    if math.isfinite(rec.chi2_red) and rec.chi2_red > q.grade_max_chi2_red:
        c.append("POOR_PSF_FIT")
    if not rec.snr_fit >= q.grade_min_snr:
        c.append("LOW_SNR")
    if c:
        return "C", c
    b = []
    if rec.footprint_warning_flags:
        b.append("WARNING_FLAGS_IN_FOOTPRINT")
    if rec.nearest_neighbour_px is not None and rec.nearest_neighbour_px < blend_px:
        b.append("NEIGHBOUR_WITHIN_BLEND_RADIUS")
    return ("B", b) if b else ("A", [])


def blend_radius_px(img: SpectralImage, config: EngineConfig) -> float:
    return config.quality.blend_radius_px


@dataclass
class DetectionRun:
    obs_id: str
    status: str  # COMPLETED | INSUFFICIENT_INPUTS | FAILED
    reasons: list[str]
    threshold_sigma: float
    inverted: bool
    accepted: list[DetectionRecord]
    rejected: list[DetectionRecord]
    n_peaks: int
    background: dict
    noise: dict
    nominal_false_positives: float | None
    nominal_note: str
    psf: dict | None
    method_id: str = DETECTION_METHOD_ID
    method_version: str = DETECTION_METHOD_VERSION


def _parabola_offset(sm: float, s0: float, sp: float) -> float:
    denom = sm - 2.0 * s0 + sp
    if denom >= 0:
        return 0.0
    return float(np.clip(0.5 * (sm - sp) / denom, -0.5, 0.5))


def detect(an: ImageAnalysis, config: EngineConfig, *, threshold_sigma: float | None = None,
           invert: bool = False) -> DetectionRun:
    img = an.image
    p = config.detection
    thr = p.threshold_sigma if threshold_sigma is None else float(threshold_sigma)
    base = dict(obs_id=img.obs_id, threshold_sigma=thr, inverted=invert, background=an.background.summary(),
                psf=img.psf.describe() if img.psf else None)
    if img.psf is None:
        return DetectionRun(status="INSUFFICIENT_INPUTS", reasons=["PSF_MODEL_UNAVAILABLE"], accepted=[], rejected=[],
                            n_peaks=0, noise={}, nominal_false_positives=None, nominal_note="", **base)
    sign = -1.0 if invert else 1.0
    ox, oy = img.origin
    ny_a, nx_a = an.residual.shape
    kernel, _, _ = img.psf.stamp(float(ox + nx_a // 2), float(oy + ny_a // 2), p.psf_stamp_px)
    num = correlate(sign * an.residual * an.weight, kernel, mode="constant", cval=0.0)
    den = correlate(an.weight, kernel * kernel, mode="constant", cval=0.0)
    ok = den > 0
    snr = np.zeros_like(num)
    snr[ok] = num[ok] / np.sqrt(den[ok])
    m = p.edge_margin_px
    interior = np.zeros(snr.shape, dtype=bool)
    if ny_a > 2 * m and nx_a > 2 * m:
        interior[m:ny_a - m, m:nx_a - m] = True
    peaks = (snr == maximum_filter(snr, size=3, mode="nearest")) & (snr >= thr) & interior & ~an.bad & ok
    ys, xs = np.nonzero(peaks)
    order = np.argsort(-snr[ys, xs], kind="stable")
    kept: list[int] = []
    for k in order:
        if all((xs[k] - xs[j]) ** 2 + (ys[k] - ys[j]) ** 2 >= p.min_separation_px ** 2 for j in kept):
            kept.append(int(k))

    tag = "N" if invert else "D"
    inits: list[tuple[float, float, float]] = []
    for k in kept:
        iy, ix = int(ys[k]), int(xs[k])
        dx = _parabola_offset(snr[iy, ix - 1], snr[iy, ix], snr[iy, ix + 1]) if 0 < ix < nx_a - 1 else 0.0
        dy = _parabola_offset(snr[iy - 1, ix], snr[iy, ix], snr[iy + 1, ix]) if 0 < iy < ny_a - 1 else 0.0
        inits.append((ox + ix + dx, oy + iy + dy, float(snr[iy, ix])))
    fits_ = [fit_psf(an, x, y, p, sign=sign) for x, y, _ in inits]
    n_subtracted = [0] * len(fits_)
    reach = float(p.psf_stamp_px)
    for _ in range(p.deblend_passes):
        models = [(f.x, f.y, f.amp) if f.status != "FAILED" and f.amp > 0 else None for f in fits_]
        updated = list(fits_)
        for i, (x, y, _) in enumerate(inits):
            nbrs = [m for j, m in enumerate(models) if j != i and m is not None and math.hypot(m[0] - x, m[1] - y) <= reach]
            if nbrs:
                updated[i] = fit_psf(an, x, y, p, sign=sign, neighbours=nbrs)
                n_subtracted[i] = len(nbrs)
        fits_ = updated

    records: list[DetectionRecord] = []
    for rank, ((_, _, snr_peak), fit) in enumerate(zip(inits, fits_)):
        rec = _record(an, fit, snr_peak, f"{img.obs_id}:{tag}{rank:04d}", invert)
        rec.neighbours_subtracted = n_subtracted[rank]
        if fit.status == "FAILED":
            rec.rejection_reasons.append("FIT_FAILED")
        if fit.masked_fraction > p.max_masked_fraction:
            rec.rejection_reasons.append("MASKED_FRACTION_EXCEEDS_LIMIT")
        if not fit.amp > 0:
            rec.rejection_reasons.append("NON_POSITIVE_AMPLITUDE")
        records.append(rec)

    live = [r for r in records if not r.rejection_reasons]
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            if not b.rejection_reasons and math.hypot(a.x - b.x, a.y - b.y) < p.min_separation_px:
                b.rejection_reasons.append(f"DUPLICATE_OF:{a.det_id}")
    accepted = [r for r in records if not r.rejection_reasons]
    rejected = [r for r in records if r.rejection_reasons]
    for r in rejected:
        r.accepted = False
    if accepted:
        pos = np.array([[r.x, r.y] for r in accepted])
        for i, r in enumerate(accepted):
            d = np.hypot(pos[:, 0] - r.x, pos[:, 1] - r.y)
            d[i] = np.inf
            r.nearest_neighbour_px = float(d.min()) if len(accepted) > 1 else None
    blend_px = blend_radius_px(img, config)
    for r in records:
        r.quality_grade, r.quality_reasons = quality_grade(r, config, blend_px)

    noise = noise_diagnostics(an, snr, ok & interior, accepted, config)
    n_indep = float((interior & ~an.bad).sum()) * float(np.sum(kernel * kernel))
    return DetectionRun(status="COMPLETED", reasons=[], accepted=accepted, rejected=rejected, n_peaks=len(kept),
                        noise=noise, nominal_false_positives=n_indep * float(ndtr(-thr)),
                        nominal_note="approximation: independent PSF footprints (N = pixels x sum P^2), Gaussian noise",
                        **base)


def noise_diagnostics(an: ImageAnalysis, snr: np.ndarray, valid: np.ndarray, accepted: list[DetectionRecord],
                      config: EngineConfig) -> dict:
    """Empirical / propagated noise ratios at pixel and PSF scale, away from detected sources."""
    p = config.detection
    img = an.image
    ox, oy = img.origin
    ny_a, nx_a = an.residual.shape
    near = np.zeros((ny_a, nx_a), dtype=bool)
    half = p.psf_stamp_px // 2
    for r in accepted:
        cx, cy = int(round(r.x)) - ox, int(round(r.y)) - oy
        near[max(cy - half, 0):cy + half + 1, max(cx - half, 0):cx + half + 1] = True
    source_px = img.masks.background_excluded(img.flags)
    pix = ~an.bad & ~near & ~source_px
    chi = an.residual[pix] * np.sqrt(an.weight[pix])
    step = half + 1
    grid = np.zeros_like(valid)
    grid[half::step, half::step] = True
    sample = grid & valid & ~an.bad & ~near & ~source_px
    vals = snr[sample]
    minimum = config.quality.min_noise_samples
    pixel = mad_std(chi) if chi.size >= minimum else None
    psf = mad_std(vals) if vals.size >= minimum else None
    out = {
        "pixel_ratio": pixel,
        "pixel_ratio_se": pixel * MAD_STD_RELATIVE_SE / math.sqrt(chi.size) if pixel is not None else None,
        "pixel_samples": int(chi.size),
        "psf_ratio": psf,
        "psf_ratio_se": psf * MAD_STD_RELATIVE_SE / math.sqrt(vals.size) if psf is not None else None,
        "psf_samples": int(vals.size),
        "method": "robust (MAD) std of residual/sigma per pixel and of matched-filter SNR at grid positions away from detections; "
                  "se = ratio x 1.166 / sqrt(n) (Gaussian asymptotic efficiency of the MAD, 0.37)",
    }
    if vals.size < minimum:
        out["psf_reason"] = f"fewer than {minimum} empty positions"
    return out


@dataclass
class ForcedMeasurement:
    obs_id: str
    ra: float
    dec: float
    x: float | None
    y: float | None
    status: str  # MEASURED | NOT_COVERED | UNUSABLE
    reasons: list[str]
    amplitude: float | None = None
    amplitude_sigma: float | None = None
    flux_ujy: float | None = None
    flux_sigma_ujy: float | None = None
    snr: float | None = None
    chi2_red: float | None = None
    masked_fraction: float | None = None
    peak_flags: list[str] = field(default_factory=list)
    footprint_warning_flags: list[str] = field(default_factory=list)
    wavelength_um: float | None = None
    bandwidth_um: float | None = None
    wavelength_source: str | None = None
    mjd: float | None = None
    detector: int | None = None
    solid_angle_source: str | None = None


def neighbour_models(detections: list[DetectionRecord], x: float, y: float, reach: float,
                     exclude_within: float = 1.5) -> list[tuple[float, float, float]]:
    """(x, y, amplitude) of detections near (x, y), excluding the source itself."""
    out = []
    for d in detections:
        dist = math.hypot(d.x - x, d.y - y)
        if exclude_within < dist <= reach and d.amplitude > 0:
            out.append((d.x, d.y, d.amplitude))
    return out


def forced_photometry(an: ImageAnalysis, ra: float, dec: float, config: EngineConfig,
                      detections: list[DetectionRecord] | None = None) -> ForcedMeasurement:
    """PSF-fit flux at a fixed sky position. Detected neighbours in the image are subtracted first."""
    img = an.image
    meta = img.meta
    if not img.contains(ra, dec):
        return ForcedMeasurement(obs_id=img.obs_id, ra=ra, dec=dec, x=None, y=None, status="NOT_COVERED",
                                 reasons=["POSITION_OUTSIDE_ANALYSED_PIXELS"], mjd=meta.mjd_avg, detector=meta.detector)
    x, y = (float(v) for v in img.world_to_pixel(ra, dec))
    nbrs = neighbour_models(detections or [], x, y, float(config.detection.psf_stamp_px))
    fit = fit_psf(an, x, y, config.detection, fixed=True, neighbours=nbrs)
    lam, bw, wsrc = img.wavelength_at(x, y)
    base = dict(obs_id=img.obs_id, ra=ra, dec=dec, x=x, y=y, mjd=meta.mjd_avg, detector=meta.detector,
                wavelength_um=lam, bandwidth_um=bw, wavelength_source=wsrc,
                peak_flags=list(img.vocabulary.names_in(img.pixel_flags(x, y))),
                footprint_warning_flags=list(img.vocabulary.names_in(fit.footprint_flags & img.masks.warning)),
                masked_fraction=fit.masked_fraction)
    if fit.status == "FAILED":
        return ForcedMeasurement(status="UNUSABLE", reasons=["NO_USABLE_PIXELS_UNDER_PSF"], **base)
    reasons = []
    if fit.masked_fraction > config.detection.max_masked_fraction:
        reasons.append("MASKED_FRACTION_EXCEEDS_LIMIT")
    amp_sigma = _amp_sigma(an, fit)
    omega, omega_src = img.pixel_solid_angle_sr(x, y)
    return ForcedMeasurement(status="UNUSABLE" if reasons else "MEASURED", reasons=reasons, amplitude=fit.amp,
                             amplitude_sigma=amp_sigma, flux_ujy=fit.amp * omega * MJY_TO_UJY,
                             flux_sigma_ujy=amp_sigma * omega * MJY_TO_UJY,
                             snr=fit.amp / amp_sigma if amp_sigma > 0 else None,
                             chi2_red=fit.chi2_red if fit.dof > 0 else None, solid_angle_source=omega_src, **base)


def add_point_sources(img: SpectralImage, sources: list[tuple[float, float, float]], stamp_px: int = 15) -> np.ndarray:
    """Return a copy of the image array with PSF-shaped sources (x, y, amplitude MJy/sr) added."""
    out = np.array(img.image, dtype=float, copy=True)
    ox, oy = img.origin
    ny_a, nx_a = out.shape
    for x, y, amp in sources:
        stamp, x_ll, y_ll = img.psf.stamp(x, y, stamp_px)
        ax0, ay0 = x_ll - ox, y_ll - oy
        xs0, xs1 = max(ax0, 0), min(ax0 + stamp_px, nx_a)
        ys0, ys1 = max(ay0, 0), min(ay0 + stamp_px, ny_a)
        if xs0 < xs1 and ys0 < ys1:
            out[ys0:ys1, xs0:xs1] += amp * stamp[ys0 - ay0:ys1 - ay0, xs0 - ax0:xs1 - ax0]
    return out
