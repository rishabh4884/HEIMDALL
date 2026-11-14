"""Per-image measurements of a target and of Gaia stationary control stars.

For every usable SPHEREx image this module reports, with uncertainties and validity states:

- the expected position of the target (catalogue propagation, see ``astrometry``);
- a fixed-position PSF-fit flux at that position (forced photometry; no detection is assumed);
- a free-position PSF fit (centroid) and its sky covariance;
- a translation registration shift against isolated Gaia control stars measured in the same image,
  estimated with sigma-clipping and applied to the target centroid (leave-one-out for the controls);
- contamination context (neighbouring Gaia stars inside the PSF stamp) and quality flags.

Assumptions stated, not hidden: the registration model is a pure translation (the cutouts are a few
arcminutes across); control stars are assumed stationary after catalogue propagation; the target centroid
is not deblended from neighbours (a neighbour inside the PSF stamp lowers the centroid's validity instead).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from ..context.gaia import Star
from ..engine.config import EngineConfig
from ..engine.detection import (ForcedMeasurement, ImageAnalysis, _record, fit_psf, forced_photometry, prepare)
from ..engine.skygeom import angular_separation_deg, offsets_arcsec
from ..engine.stats import mad_std
from ..ingest.spherex import SpectralImage
from .astrometry import ReferenceAstrometry

CONTROL_G_RANGE = (11.0, 16.5)
CONTROL_ISOLATION_ARCSEC = 30.0
CONTROL_MIN_SNR = 15.0
MIN_CONTROLS_FOR_REGISTRATION = 3
SATURATION_FLAGS = ("NONLINEAR", "OVERFLOW", "BLOOM", "SNOWBALL", "HALO", "SATELLITE_HALO", "PERSIST", "SUR_ERROR")


def reference_from_star(s: Star, target_id: str = "CONTROL") -> ReferenceAstrometry:
    return ReferenceAstrometry(
        target_id=target_id, catalogue="Gaia DR3", status="RESOLVED", source_id=f"Gaia DR3 {s.source_id}", ra_deg=s.ra, dec_deg=s.dec,
        epoch_jyear=s.ref_epoch, pmra_mas_yr=s.pmra, pmdec_mas_yr=s.pmdec, pmra_err_mas_yr=s.pmra_error or 0.0,
        pmdec_err_mas_yr=s.pmdec_error or 0.0, ra_err_mas=s.ra_error_mas, dec_err_mas=s.dec_error_mas, parallax_mas=s.parallax,
        parallax_err_mas=s.parallax_error, magnitudes={"G": s.g_mag})


def select_controls(stars: list[Star], target: ReferenceAstrometry, max_controls: int = 12) -> list[Star]:
    """Isolated, moderately bright Gaia stars: bright enough for a precise centroid, faint enough to be unsaturated."""
    lo, hi = CONTROL_G_RANGE
    out = []
    for s in stars:
        if s.g_mag is None or not (lo <= s.g_mag <= hi) or s.pmra is None or s.pmdec is None:
            continue
        if target.resolved and float(angular_separation_deg(s.ra, s.dec, target.ra_deg, target.dec_deg)) * 3600.0 < 20.0:
            continue
        iso = True
        for o in stars:
            if o is s or o.g_mag is None or o.g_mag > s.g_mag + 3.0:
                continue
            if float(angular_separation_deg(s.ra, s.dec, o.ra, o.dec)) * 3600.0 < CONTROL_ISOLATION_ARCSEC:
                iso = False
                break
        if iso:
            out.append(s)
    out.sort(key=lambda s: s.g_mag)
    return out[:max_controls]


@dataclass
class CentroidMeasurement:
    ra_deg: float
    dec_deg: float
    cov_arcsec2: list[list[float]]
    snr: float
    chi2_red: float
    fit_status: str
    masked_fraction: float
    flags: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ControlOffset:
    gaia_source_id: str
    g_mag: float
    d_east_arcsec: float
    d_north_arcsec: float
    cov_arcsec2: list[list[float]]
    snr: float


def _measure_centroid(an: ImageAnalysis, ra: float, dec: float, cfg: EngineConfig) -> CentroidMeasurement | None:
    img = an.image
    if not img.contains(ra, dec, margin_px=cfg.detection.edge_margin_px):
        return None
    x, y = (float(v) for v in img.world_to_pixel(ra, dec))
    fit = fit_psf(an, x, y, cfg.detection, fixed=False)
    if fit.status == "FAILED":
        return None
    rec = _record(an, fit, 0.0, "TARGET", False)
    flags = sorted(set(rec.peak_flags) | set(rec.footprint_warning_flags))
    return CentroidMeasurement(ra_deg=rec.ra, dec_deg=rec.dec, cov_arcsec2=rec.cov_sky_arcsec2, snr=rec.snr_fit, chi2_red=rec.chi2_red,
                               fit_status=fit.status, masked_fraction=fit.masked_fraction, flags=flags)


def _clipped_mean(d: np.ndarray, cov: np.ndarray, floor: float, clip: float = 3.0, iters: int = 5):
    """Weighted translation estimate with iterative clipping on the robust scatter. Returns (shift, shift_cov, kept_mask, scale)."""
    n = len(d)
    keep = np.ones(n, dtype=bool)
    shift, scov, scale = np.zeros(2), np.eye(2) * floor ** 2, 1.0
    for _ in range(iters):
        if keep.sum() < 1:
            break
        w = np.array([np.linalg.inv(cov[i] + np.eye(2) * 1e-6) for i in range(n)])
        W = w[keep].sum(axis=0)
        shift = np.linalg.solve(W, sum(w[i] @ d[i] for i in np.flatnonzero(keep)))
        r = d - shift
        sig = np.array([mad_std(r[:, 0][keep]), mad_std(r[:, 1][keep])]) if keep.sum() >= 3 else np.array([floor, floor])
        tol = clip * np.maximum(sig, floor)
        new = np.all(np.abs(r) <= tol, axis=1)
        if new.sum() < 1 or (new == keep).all():
            break
        keep = new
    if keep.sum() >= 1:
        W = np.array([np.linalg.inv(cov[i] + np.eye(2) * 1e-6) for i in np.flatnonzero(keep)]).sum(axis=0)
        chi2 = float(sum((d[i] - shift) @ np.linalg.inv(cov[i] + np.eye(2) * 1e-6) @ (d[i] - shift) for i in np.flatnonzero(keep)))
        dof = 2 * int(keep.sum()) - 2
        scale = max(1.0, math.sqrt(chi2 / dof)) if dof > 0 else 1.0
        scov = np.linalg.inv(W) * scale ** 2 + np.eye(2) * floor ** 2 / max(int(keep.sum()), 1)
    return shift, scov, keep, scale


def registration_shift(controls: list[ControlOffset], floor_arcsec: float, exclude: int | None = None) -> dict:
    use = [c for i, c in enumerate(controls) if i != exclude]
    if len(use) < MIN_CONTROLS_FOR_REGISTRATION:
        return {"status": "UNCONSTRAINED", "n_controls": len(use), "shift_arcsec": [0.0, 0.0],
                "cov_arcsec2": (np.eye(2) * floor_arcsec ** 2).tolist(),
                "reason": f"fewer than {MIN_CONTROLS_FOR_REGISTRATION} usable control stars; no correction applied, floor uncertainty used"}
    d = np.array([[c.d_east_arcsec, c.d_north_arcsec] for c in use])
    cov = np.array([np.asarray(c.cov_arcsec2) for c in use])
    shift, scov, keep, scale = _clipped_mean(d, cov, floor_arcsec)
    return {"status": "ESTIMATED", "n_controls": len(use), "n_used": int(keep.sum()), "shift_arcsec": shift.tolist(),
            "cov_arcsec2": scov.tolist(), "error_scale": float(scale),
            "model": "translation only; sigma-clipped weighted mean of control-star offsets (measured - Gaia expected)"}


@dataclass
class ImageMeasurement:
    obs_id: str
    status: str  # MEASURED | NOT_COVERED | EXCLUDED | UNUSABLE
    reasons: list[str]
    mjd: float | None = None
    detector: int | None = None
    release: str | None = None
    pipeline_version: str | None = None
    product_checksum: str | None = None
    access_reference: str | None = None
    retrieval_origin: str | None = None
    expected: dict | None = None
    forced: dict | None = None
    centroid: dict | None = None
    registration: dict | None = None
    corrected_offset_arcsec: list[float] | None = None  # measured (registered) minus expected: (east, north)
    corrected_offset_cov_arcsec2: list[list[float]] | None = None
    controls: list[dict] = field(default_factory=list)
    control_fluxes: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    saturation_risk: bool = False
    contamination: list[dict] = field(default_factory=list)
    wavelength_um: float | None = None
    bandwidth_um: float | None = None
    pixel_scale_arcsec: float | None = None
    psf_fwhm_arcsec: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def contamination(stars: list[Star], target: ReferenceAstrometry, mjd: float, stamp_arcsec: float, g_target: float | None) -> list[dict]:
    """Gaia stars inside the PSF stamp. Flux ratios use G only as a rough optical proxy and are labelled as such."""
    ex = target.at_mjd(mjd)
    out = []
    for s in stars:
        if s.source_id in str(target.source_id):
            continue
        ra, dec, _ = s.at_epoch(mjd)
        sep = float(angular_separation_deg(ra, dec, ex.ra_deg, ex.dec_deg)) * 3600.0
        if sep <= stamp_arcsec and s.g_mag is not None:
            item = {"gaia_source_id": s.source_id, "separation_arcsec": sep, "g_mag": s.g_mag}
            if g_target is not None:
                item["optical_flux_ratio_to_target"] = float(10 ** (-0.4 * (s.g_mag - g_target)))
                item["note"] = "G-band ratio is an optical proxy; the SPHEREx ratio depends on colour"
            out.append(item)
    return sorted(out, key=lambda d: d["separation_arcsec"])


def measure_image(img: SpectralImage, target: ReferenceAstrometry, controls: list[Star], stars: list[Star], cfg: EngineConfig, *,
                  product: dict | None = None) -> ImageMeasurement:
    meta = img.meta
    prod = product or {}
    base = dict(obs_id=img.obs_id, mjd=meta.mjd_avg, detector=meta.detector, release=meta.release, pipeline_version=meta.pipeline_version,
                product_checksum=prod.get("checksum"), access_reference=prod.get("access_reference"), retrieval_origin=prod.get("origin"),
                pixel_scale_arcsec=meta.pixel_scale_arcsec, psf_fwhm_arcsec=meta.psf_fwhm_arcsec)
    if meta.mjd_avg is None:
        return ImageMeasurement(status="UNUSABLE", reasons=["NO_OBSERVATION_TIME"], **base)
    ex = target.at_mjd(meta.mjd_avg)
    expected = {"ra_deg": ex.ra_deg, "dec_deg": ex.dec_deg, "cov_arcsec2": ex.cov_arcsec2, "components": ex.components}
    if not img.contains(ex.ra_deg, ex.dec_deg, margin_px=cfg.detection.edge_margin_px):
        return ImageMeasurement(status="NOT_COVERED", reasons=["EXPECTED_POSITION_OUTSIDE_ANALYSED_PIXELS"], expected=expected, **base)
    an = prepare(img, cfg)
    forced: ForcedMeasurement = forced_photometry(an, ex.ra_deg, ex.dec_deg, cfg)
    cen = _measure_centroid(an, ex.ra_deg, ex.dec_deg, cfg)
    x, y = (float(v) for v in img.world_to_pixel(ex.ra_deg, ex.dec_deg))
    reasons: list[str] = []
    flags = sorted(set(forced.peak_flags) | set(forced.footprint_warning_flags) | (set(cen.flags) if cen else set()))
    sat = any(f in SATURATION_FLAGS for f in flags)
    if sat:
        reasons.append("SATURATION_OR_NONLINEARITY_FLAGS_IN_PSF_FOOTPRINT")
    stamp_arcsec = cfg.detection.psf_stamp_px * meta.pixel_scale_arcsec / 2.0
    cont = contamination(stars, target, meta.mjd_avg, stamp_arcsec, (target.magnitudes or {}).get("G"))
    # Control stars in this image.
    offsets: list[ControlOffset] = []
    control_fluxes: list[dict] = []
    for s in controls:
        cref = reference_from_star(s)
        ce = cref.at_mjd(meta.mjd_avg)
        cf = forced_photometry(an, ce.ra_deg, ce.dec_deg, cfg)
        if cf.status == "MEASURED" and cf.snr is not None and cf.snr >= 3.0 and not any(f in SATURATION_FLAGS for f in cf.peak_flags + cf.footprint_warning_flags):
            control_fluxes.append({"gaia_source_id": s.source_id, "g_mag": s.g_mag, "flux_ujy": cf.flux_ujy, "flux_sigma_ujy": cf.flux_sigma_ujy,
                                   "snr": cf.snr, "wavelength_um": cf.wavelength_um, "bandwidth_um": cf.bandwidth_um, "chi2_red": cf.chi2_red})
        cm = _measure_centroid(an, ce.ra_deg, ce.dec_deg, cfg)
        if cm is None or cm.fit_status != "CONVERGED" or cm.snr < CONTROL_MIN_SNR or cm.masked_fraction > cfg.detection.max_masked_fraction:
            continue
        if any(f in SATURATION_FLAGS for f in cm.flags):
            continue
        d = offsets_arcsec(cm.ra_deg, cm.dec_deg, ce.ra_deg, ce.dec_deg)
        cov = np.asarray(cm.cov_arcsec2) + np.asarray(ce.cov_arcsec2)
        offsets.append(ControlOffset(s.source_id, float(s.g_mag or math.nan), float(d[0]), float(d[1]), cov.tolist(), cm.snr))
    floor = cfg.association.registration_floor_arcsec
    reg = registration_shift(offsets, floor)
    out = ImageMeasurement(status="MEASURED", reasons=reasons, expected=expected, forced=asdict(forced), registration=reg,
                           controls=[asdict(o) for o in offsets], control_fluxes=control_fluxes, flags=flags, saturation_risk=sat, contamination=cont,
                           wavelength_um=forced.wavelength_um, bandwidth_um=forced.bandwidth_um, **base)
    if cen is not None:
        out.centroid = cen.to_dict()
        d = offsets_arcsec(cen.ra_deg, cen.dec_deg, ex.ra_deg, ex.dec_deg)
        shift = np.asarray(reg["shift_arcsec"])
        cov = np.asarray(cen.cov_arcsec2) + np.asarray(reg["cov_arcsec2"]) + np.asarray(ex.cov_arcsec2)
        out.corrected_offset_arcsec = (np.asarray(d) - shift).tolist()
        out.corrected_offset_cov_arcsec2 = cov.tolist()
        if cen.fit_status != "CONVERGED":
            out.reasons.append("CENTROID_FIT_NOT_CONVERGED")
    else:
        out.reasons.append("CENTROID_UNAVAILABLE")
    if forced.status != "MEASURED":
        out.reasons.extend(forced.reasons or ["FORCED_PHOTOMETRY_UNUSABLE"])
    _ = (x, y)
    return out
