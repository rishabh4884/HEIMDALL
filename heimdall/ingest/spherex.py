"""SPHEREx Level 2 spectral image (MEF) ingestion and quality control.

Structure follows QR2 and QR3 product layouts and IRSA documentation:

- ``IMAGE``: MJy/sr, float32 (big-endian on disk); zodiacal light NOT subtracted.
- ``FLAGS``: int32 bitmask; bit names defined by ``MP_<NAME>`` header keywords.
- ``VARIANCE``: (MJy/sr)^2.  ``ZODI``: MJy/sr model (not subtracted).
- ``PSF``: 101x101x121 cube, ``OVERSAMP`` = 10, zone centres ``XCTR_i/YCTR_i``
  (QR1/QR2), or ``EPSF`` binary table (pipeline version >= 7.0).
- ``WCS-WAVE``: ``WAVE-TAB`` lookup used through the alternate WCS ``W``
  (SIP must be disabled for it).
- IMAGE header: ``DETECTOR``, ``OBSID``, ``TIMESYS`` = UTC, ``MJD-BEG``,
  ``MJD-AVG``, ``MJD-END``, ``XPOSURE``, ``FINAST``, ``L2DQAFLG``, ``PSF_FWHM``,
  ``OMEGA_MEDIAN``/``OMEGA_STDDEV`` (arcsec^2), ``X_SC``/``Y_SC``/``Z_SC`` (km,
  geocentric), alternate WCS ``A`` (cutout offset in the parent image).
- PRIMARY header: ``VERSION`` (pipeline software version).

Pixel coordinates are 0-based *image* coordinates of the file's IMAGE array.
When a full frame is cropped on read, arrays hold only the window
``(x0, x1, y0, y1)``; all methods still take image coordinates. *Parent*
coordinates (image + ``parent_offset``) locate PSF zones and calibration maps.

Synthetic products written by ``heimdall.synthetic`` carry ``SYNTHETC = T`` and
an ``OBSID`` starting with ``SYNTHETIC``; they are always labelled as such.
"""

from __future__ import annotations

import copy
import functools
import logging
import math
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from astropy import units as u
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from astropy.wcs.utils import proj_plane_pixel_scales

from ..contracts import DataOrigin, Observation, Provenance, Uncertainty, ValueClass
from ..engine.config import EngineConfig
from ..engine.methods import derived_quantity, source_reported
from ..engine.psf import EpsfModel, LegacyPsfModel, PsfModel, gaussian_from_fwhm, load_epsf_table
from ..engine.skygeom import ARCSEC2_TO_SR
from .flags import FlagMasks, FlagVocabulary, build_masks


@functools.lru_cache(maxsize=16)
def _calibration_arrays(path: str, kind: str) -> tuple:
    """Read a calibration product once per process. The arrays are read-only and shared by every image."""
    with fits.open(path, memmap=False) as cal:
        if kind == "epsf":
            model = load_epsf_table(cal["EPSF"], (0.0, 0.0), "EPSF_CALIBRATION", path)
            out = (str(cal["EPSF"].header.get("DETECTOR", "")).strip(), model.arrays, model.centers, model.oversampling)
        elif kind == "spectral_wcs":
            out = (np.asarray(cal["CWAVE"].data, dtype=float), np.asarray(cal["CBAND"].data, dtype=float))
        else:
            out = (np.asarray(cal["IMAGE"].data, dtype=float),)
    for item in out:
        if isinstance(item, np.ndarray):
            item.setflags(write=False)
    return out

IMAGE_UNIT = u.Unit("MJy / sr")
VARIANCE_UNIT = u.Unit("MJy2 / sr2")
REQUIRED_HDUS = ("IMAGE", "FLAGS", "VARIANCE")
FITS_SOURCE = "SPHEREx L2 FITS header"

# Nominal detector bands in micrometres (IRSA SPHEREx Data Products documentation).
DETECTOR_BANDS_UM = {1: (0.75, 1.09), 2: (1.10, 1.62), 3: (1.63, 2.41), 4: (2.42, 3.82), 5: (3.83, 4.41), 6: (4.42, 5.00)}


@dataclass
class QcItem:
    check: str
    state: str  # PASSED | FAILED | WARNING | NOT_ASSESSABLE
    detail: str


@dataclass
class QcReport:
    items: list[QcItem] = field(default_factory=list)
    excluded: bool = False
    exclusion_reasons: list[str] = field(default_factory=list)
    suitability: dict[str, bool] = field(default_factory=lambda: {
        "detection": True, "photometry": True, "astrometry": True, "spectral": True, "temporal": True})
    suitability_reasons: dict[str, list[str]] = field(default_factory=dict)

    def add(self, check: str, state: str, detail: str = "") -> None:
        self.items.append(QcItem(check=check, state=state, detail=detail))

    def exclude(self, reason: str) -> None:
        self.excluded = True
        if reason not in self.exclusion_reasons:
            self.exclusion_reasons.append(reason)
        for key in self.suitability:
            self.unsuitable(key, reason)

    def unsuitable(self, analysis: str, reason: str) -> None:
        self.suitability[analysis] = False
        self.suitability_reasons.setdefault(analysis, [])
        if reason not in self.suitability_reasons[analysis]:
            self.suitability_reasons[analysis].append(reason)


@dataclass
class ImageMeta:
    obs_id: str
    observation_id: str | None
    detector: int | None
    release: str
    pipeline_version: str | None
    mjd_avg: float | None
    mjd_beg: float | None
    mjd_end: float | None
    exposure_s: float | None
    finast: int | None
    dqa: str | None
    omega_median_arcsec2: float | None
    omega_std_arcsec2: float | None
    psf_fwhm_arcsec: float | None
    pixel_scale_arcsec: float
    spacecraft_geocentric_km: tuple[float, float, float] | None
    spacecraft_velocity_km_s: tuple[float, float, float] | None
    parent_offset: tuple[float, float]
    full_shape: tuple[int, int]
    window: tuple[int, int, int, int]
    calibration_ids: list[str]
    synthetic: bool
    psf: dict[str, object] | None
    wavelength_source: str | None
    solid_angle_source: str | None
    flag_vocabulary: dict[str, int]
    undefined_policy_flags: list[str]
    path: str


class SpectralImage:
    """An analysable spectral image: arrays (possibly cropped) plus WCS, flags, PSF and metadata."""

    def __init__(self, *, image, variance, flags, zodi, wcs, spectral_wcs, vocabulary: FlagVocabulary,
                 masks: FlagMasks, psf: PsfModel | None, meta: ImageMeta, origin: tuple[int, int] = (0, 0),
                 cwave=None, cband=None, solid_angle_map=None) -> None:
        self.image = image
        self.variance = variance
        self.flags = flags
        self.zodi = zodi
        self.wcs = wcs
        self.spectral_wcs = spectral_wcs
        self.vocabulary = vocabulary
        self.masks = masks
        self.psf = psf
        self.meta = meta
        self.origin = (int(origin[0]), int(origin[1]))
        self.cwave = cwave
        self.cband = cband
        self.solid_angle_map = solid_angle_map
        ny, nx = image.shape
        self.window = (self.origin[0], self.origin[0] + nx, self.origin[1], self.origin[1] + ny)

    @property
    def obs_id(self) -> str:
        return self.meta.obs_id

    def with_image(self, image: np.ndarray) -> SpectralImage:
        """A copy sharing everything except the pixel values (used for injection tests)."""
        if image.shape != self.image.shape:
            raise ValueError("replacement image must have the same shape")
        new = copy.copy(self)
        new.image = image
        return new

    def bad_mask(self) -> np.ndarray:
        bad = self.masks.bad_pixels(self.flags)
        with np.errstate(invalid="ignore"):
            bad |= ~np.isfinite(self.image) | ~np.isfinite(self.variance) | ~(self.variance > 0)
        return bad

    def pixel_flags(self, x: float, y: float) -> int:
        ix = int(math.floor(x + 0.5)) - self.origin[0]
        iy = int(math.floor(y + 0.5)) - self.origin[1]
        if 0 <= iy < self.flags.shape[0] and 0 <= ix < self.flags.shape[1]:
            return int(self.flags[iy, ix])
        return 0

    def pixel_to_world(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        ra, dec = self.wcs.pixel_to_world_values(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
        return np.asarray(ra) % 360.0, np.asarray(dec)

    def world_to_pixel(self, ra, dec) -> tuple[np.ndarray, np.ndarray]:
        x, y = self.wcs.world_to_pixel_values(np.asarray(ra, dtype=float), np.asarray(dec, dtype=float))
        return np.asarray(x), np.asarray(y)

    def contains(self, ra: float, dec: float, margin_px: float = 0.0) -> bool:
        x, y = self.world_to_pixel(ra, dec)
        x0, x1, y0, y1 = self.window
        return bool(np.isfinite(x) and np.isfinite(y)
                    and x0 - 0.5 + margin_px <= x <= x1 - 0.5 - margin_px
                    and y0 - 0.5 + margin_px <= y <= y1 - 0.5 - margin_px)

    def wavelength_at(self, x: float, y: float) -> tuple[float, float, str | None]:
        """Central wavelength and bandwidth (micrometres) at an image pixel, with the source used."""
        if self.cwave is not None and self.cband is not None:
            px = int(round(x + self.meta.parent_offset[0]))
            py = int(round(y + self.meta.parent_offset[1]))
            if 0 <= py < self.cwave.shape[0] and 0 <= px < self.cwave.shape[1]:
                return float(self.cwave[py, px]), float(self.cband[py, px]), "CWAVE_CBAND"
        if self.spectral_wcs is None:
            return math.nan, math.nan, None
        try:
            lam, bw = self.spectral_wcs.pixel_to_world_values(float(x), float(y))
        except Exception:  # noqa: BLE001 - a lookup failure means "not established"
            return math.nan, math.nan, None
        lam, bw = float(lam), float(bw)
        if not (math.isfinite(lam) and math.isfinite(bw)):
            return math.nan, math.nan, None
        return lam, bw, "WCS-WAVE"

    def pixel_solid_angle_sr(self, x: float, y: float) -> tuple[float, str]:
        if self.solid_angle_map is not None:
            px = int(round(x + self.meta.parent_offset[0]))
            py = int(round(y + self.meta.parent_offset[1]))
            if 0 <= py < self.solid_angle_map.shape[0] and 0 <= px < self.solid_angle_map.shape[1]:
                value = float(self.solid_angle_map[py, px])
                if value > 0:
                    return value * ARCSEC2_TO_SR, "SOLID_ANGLE_PIXEL_MAP"
        if self.meta.omega_median_arcsec2:
            return self.meta.omega_median_arcsec2 * ARCSEC2_TO_SR, "OMEGA_MEDIAN header (median of the solid angle pixel map)"
        return (self.meta.pixel_scale_arcsec ** 2) * ARCSEC2_TO_SR, "WCS pixel scale squared (approximation)"

    def footprint(self, per_edge: int = 6) -> tuple[list[float], list[float]]:
        """Sky polygon of the analysed pixel area (pixel edges), counter-clockwise in pixel space."""
        x0, x1, y0, y1 = self.window
        xa, xb, ya, yb = x0 - 0.5, x1 - 0.5, y0 - 0.5, y1 - 0.5
        ts = np.linspace(0.0, 1.0, per_edge, endpoint=False)
        xs = np.concatenate([xa + ts * (xb - xa), np.full(per_edge, xb), xb - ts * (xb - xa), np.full(per_edge, xa)])
        ys = np.concatenate([np.full(per_edge, ya), ya + ts * (yb - ya), np.full(per_edge, yb), yb - ts * (yb - ya)])
        ra, dec = self.pixel_to_world(xs, ys)
        return [float(v) for v in ra], [float(v) for v in dec]


@dataclass
class IngestResult:
    image: SpectralImage | None
    observation: Observation | None
    qc: QcReport


def _quiet_wcs(header, **kwargs) -> WCS:
    logger = logging.getLogger("astropy")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FITSFixedWarning)
            return WCS(header, **kwargs)
    finally:
        logger.setLevel(level)


def _num(header, key):
    value = header.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(header, key):
    value = header.get(key)
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def release_from_path(path: str) -> str:
    m = re.search(r"/(qr\d+|dr\d+)/", path)
    return f"spherex_{m.group(1)}" if m else "UNKNOWN"


def _units_match(header, expected: u.UnitBase) -> bool:
    raw = header.get("BUNIT")
    if not raw:
        return False
    try:
        return u.Unit(raw) == expected
    except (ValueError, TypeError):
        return False


def _legacy_psf(hdu, offset, source) -> LegacyPsfModel:
    header = hdu.header
    planes = np.asarray(hdu.data, dtype=float)
    centers = []
    for i in range(1, planes.shape[0] + 1):
        xc, yc = header.get(f"XCTR_{i}"), header.get(f"YCTR_{i}")
        if xc is None or yc is None:
            raise ValueError("legacy PSF cube lacks zone centre keywords")
        centers.append((float(xc), float(yc)))
    return LegacyPsfModel(planes, np.array(centers), int(header.get("OVERSAMP", 10)), offset, source)


def read_spherex_mef(path: Path | str, *, source_id: str, provenance: Provenance, config: EngineConfig | None = None,
                     release: str | None = None, obs_id: str | None = None,
                     crop: tuple[float, float, float] | None = None, crop_margin_px: int = 12,
                     epsf_calibration: Path | None = None, spectral_wcs_calibration: Path | None = None,
                     solid_angle_calibration: Path | None = None) -> IngestResult:
    """Parse a SPHEREx L2 spectral image (full frame or cutout) and run quality control.

    ``crop`` = (ra, dec, radius_deg) keeps only the pixels around a region,
    which bounds memory for full frames.
    """
    config = config or EngineConfig()
    qp = config.quality
    qc = QcReport()
    path = str(path)
    try:
        hdul = fits.open(path, memmap=True)
    except (OSError, ValueError) as exc:
        qc.add("fits_readable", "FAILED", f"cannot open FITS: {exc}")
        qc.exclude("PRODUCT_UNREADABLE")
        return IngestResult(image=None, observation=None, qc=qc)

    with hdul:
        try:
            names = {h.name for h in hdul}
        except (OSError, ValueError) as exc:
            qc.add("fits_readable", "FAILED", f"cannot read HDUs: {exc}")
            qc.exclude("PRODUCT_UNREADABLE")
            return IngestResult(image=None, observation=None, qc=qc)
        missing = [n for n in REQUIRED_HDUS if n not in names]
        if missing:
            qc.add("required_hdus", "FAILED", f"missing HDUs: {missing}")
            qc.exclude("REQUIRED_HDU_MISSING")
            return IngestResult(image=None, observation=None, qc=qc)
        qc.add("required_hdus", "PASSED", "IMAGE, FLAGS, VARIANCE present")

        primary = hdul[0].header
        ih = hdul["IMAGE"].header
        raw_image, raw_var, raw_flags = hdul["IMAGE"].data, hdul["VARIANCE"].data, hdul["FLAGS"].data
        if raw_image is None or raw_var is None or raw_flags is None or raw_image.ndim != 2 \
                or raw_image.shape != raw_var.shape or raw_image.shape != raw_flags.shape:
            shapes = [None if a is None else a.shape for a in (raw_image, raw_var, raw_flags)]
            qc.add("array_shapes", "FAILED", f"IMAGE/VARIANCE/FLAGS shapes {shapes}")
            qc.exclude("ARRAY_SHAPE_MISMATCH")
            return IngestResult(image=None, observation=None, qc=qc)
        full_shape = tuple(int(s) for s in raw_image.shape)
        qc.add("array_shapes", "PASSED", f"shape {full_shape}")

        if _units_match(ih, IMAGE_UNIT):
            qc.add("image_unit", "PASSED", "IMAGE BUNIT = MJy/sr")
        else:
            qc.add("image_unit", "FAILED", f"IMAGE BUNIT = {ih.get('BUNIT')!r}, expected MJy/sr")
            qc.exclude("IMAGE_UNIT_UNEXPECTED")
        if _units_match(hdul["VARIANCE"].header, VARIANCE_UNIT):
            qc.add("variance_unit", "PASSED", "VARIANCE BUNIT = (MJy/sr)^2")
        else:
            qc.add("variance_unit", "FAILED", f"VARIANCE BUNIT = {hdul['VARIANCE'].header.get('BUNIT')!r}")
            qc.exclude("VARIANCE_UNIT_UNEXPECTED")

        celestial = _quiet_wcs(ih)
        if not celestial.has_celestial or celestial.naxis != 2:
            qc.add("celestial_wcs", "FAILED", "no two-axis celestial WCS")
            qc.exclude("CELESTIAL_WCS_INVALID")
            return IngestResult(image=None, observation=None, qc=qc)
        celestial = celestial.celestial
        qc.add("celestial_wcs", "PASSED", "/".join(celestial.wcs.ctype))
        radesys = str(ih.get("RADESYS", "")).strip()
        if radesys and radesys != "ICRS":
            qc.add("reference_frame", "WARNING", f"RADESYS = {radesys}; coordinates are not ICRS")
        pixel_scale = float(np.mean(proj_plane_pixel_scales(celestial)) * 3600.0)

        ny_full, nx_full = full_shape
        x0, x1, y0, y1 = 0, nx_full, 0, ny_full
        if crop is not None:
            xc, yc = celestial.world_to_pixel_values(crop[0], crop[1])
            r_px = crop[2] * 3600.0 / pixel_scale + crop_margin_px
            if not (np.isfinite(xc) and np.isfinite(yc)):
                qc.add("region_overlap", "FAILED", "region centre does not project onto this image")
                qc.exclude("REGION_OUTSIDE_IMAGE")
                return IngestResult(image=None, observation=None, qc=qc)
            x0, x1 = max(0, int(math.floor(xc - r_px))), min(nx_full, int(math.ceil(xc + r_px)) + 1)
            y0, y1 = max(0, int(math.floor(yc - r_px))), min(ny_full, int(math.ceil(yc + r_px)) + 1)
            if x0 >= x1 or y0 >= y1:
                qc.add("region_overlap", "FAILED", "region does not overlap this image")
                qc.exclude("REGION_OUTSIDE_IMAGE")
                return IngestResult(image=None, observation=None, qc=qc)
            qc.add("region_overlap", "PASSED", f"analysis window x[{x0},{x1}) y[{y0},{y1})")
        image = np.array(raw_image[y0:y1, x0:x1], dtype=np.float64)
        variance = np.array(raw_var[y0:y1, x0:x1], dtype=np.float64)
        flags = np.array(raw_flags[y0:y1, x0:x1]).astype(np.int64)
        zodi = np.array(hdul["ZODI"].data[y0:y1, x0:x1], dtype=np.float64) if "ZODI" in names and hdul["ZODI"].data is not None else None

        with np.errstate(invalid="ignore"):
            finite = np.isfinite(image) & np.isfinite(variance) & (variance > 0)
        frac = float(finite.mean()) if finite.size else 0.0
        if frac < qp.min_finite_fraction:
            qc.add("finite_pixels", "FAILED", f"{frac:.1%} of pixels have finite values and positive variance")
            qc.exclude("TOO_FEW_VALID_PIXELS")
        else:
            qc.add("finite_pixels", "PASSED", f"{frac:.1%} valid")

        vocabulary = FlagVocabulary.from_header(hdul["FLAGS"].header)
        if not vocabulary.defined:
            qc.add("flag_vocabulary", "FAILED", "FLAGS header defines no flag bits (neither MP_<NAME> nor MSKN/MSKM); "
                                                "flag meaning cannot be established")
            qc.exclude("FLAG_SEMANTICS_NOT_ESTABLISHED")
        else:
            qc.add("flag_vocabulary", "PASSED", f"{len(vocabulary.bits)} flag bits defined in header ({vocabulary.convention})")
        if vocabulary.inconsistent:
            qc.add("flag_vocabulary_consistency", "WARNING", f"ignored inconsistent definitions: {list(vocabulary.inconsistent)[:5]}")
        masks = build_masks(vocabulary, qp.exclude_flags, qp.warning_flags, qp.background_exclude_flags)

        spectral = None
        if "WCS-WAVE" in names and ih.get("CTYPE1W"):
            try:
                spectral = _quiet_wcs(ih, fobj=hdul, key="W")
                spectral.sip = None  # documented: the wavelength WCS must not use SIP
                lam, _ = spectral.pixel_to_world_values(float((x0 + x1 - 1) / 2), float((y0 + y1 - 1) / 2))
                if not np.isfinite(lam):
                    raise ValueError("lookup returns a non-finite wavelength")
                qc.add("spectral_wcs", "PASSED", "WCS-WAVE lookup usable (visualisation fidelity, about 1 nm)")
            except Exception as exc:  # noqa: BLE001
                spectral = None
                qc.add("spectral_wcs", "FAILED", f"WCS-WAVE lookup unusable: {exc}")
        else:
            qc.add("spectral_wcs", "NOT_ASSESSABLE", "no WCS-WAVE lookup in product")

        offset = (0.0, 0.0)
        if str(ih.get("CTYPE1A", "")).strip() == "LINEAR" and ih.get("CRPIX1A") is not None:
            # Alternate WCS "A" maps cutout pixels to 0-based parent active-pixel coordinates:
            # parent = CRVAL + CDELT * (x_0based + 1 - CRPIX).
            offset = (float(ih.get("CRVAL1A", 0.0)) + 1.0 - float(ih["CRPIX1A"]),
                      float(ih.get("CRVAL2A", 0.0)) + 1.0 - float(ih["CRPIX2A"]))

        mjd_avg, mjd_beg, mjd_end = _num(ih, "MJD-AVG"), _num(ih, "MJD-BEG"), _num(ih, "MJD-END")
        if mjd_avg is None and mjd_beg is not None and mjd_end is not None:
            mjd_avg = (mjd_beg + mjd_end) / 2.0
        if mjd_avg is None:
            mjd_avg = _num(ih, "MJD-OBS")
        timesys = str(ih.get("TIMESYS", "")).strip().upper()
        if mjd_avg is None:
            qc.add("observation_time", "FAILED", "no MJD-AVG, MJD-BEG/END or MJD-OBS")
            qc.unsuitable("temporal", "OBSERVATION_TIME_MISSING")
        elif timesys != "UTC":
            qc.add("observation_time", "FAILED", f"TIMESYS = {timesys or 'missing'}; only UTC is supported")
            qc.unsuitable("temporal", "TIME_SCALE_NOT_SUPPORTED")
            mjd_avg = None
        else:
            qc.add("observation_time", "PASSED", f"MJD-AVG {mjd_avg:.6f} UTC")

        detector = _int(ih, "DETECTOR")
        if detector not in DETECTOR_BANDS_UM:
            qc.add("detector", "FAILED", f"DETECTOR = {ih.get('DETECTOR')!r}")
            qc.unsuitable("spectral", "DETECTOR_UNKNOWN")
        else:
            qc.add("detector", "PASSED", f"detector {detector}")

        finast = _int(ih, "FINAST")
        if finast == 0:
            qc.add("fine_astrometry", "PASSED", "FINAST = 0 (good fine-astrometric solution)")
        else:
            qc.add("fine_astrometry", "FAILED", f"FINAST = {finast!r}")
            if qp.require_finast_zero:
                qc.unsuitable("astrometry", "FINE_ASTROMETRY_NOT_GOOD")

        dqa = str(ih.get("L2DQAFLG", "")).strip() or None
        if dqa == "Pass":
            qc.add("l2_dqa", "PASSED", "L2DQAFLG = Pass")
        else:
            qc.add("l2_dqa", "FAILED", f"L2DQAFLG = {dqa!r}")
            if qp.require_dqa_pass:
                qc.exclude("L2_DQA_NOT_PASSED")

        synthetic = bool(primary.get("SYNTHETC", False)) or str(ih.get("OBSID", "")).startswith("SYNTHETIC")
        if synthetic and provenance.origin is not DataOrigin.SYNTHETIC_TEST_FIXTURE:
            qc.add("data_origin", "FAILED", "synthetic product presented with a non-synthetic provenance")
            qc.exclude("SYNTHETIC_PROVENANCE_MISMATCH")
        elif not synthetic and provenance.origin is DataOrigin.SYNTHETIC_TEST_FIXTURE:
            qc.add("data_origin", "WARNING", "product has no synthetic marker but provenance says synthetic")

        psf: PsfModel | None = None
        try:
            if "EPSF" in names:
                psf = load_epsf_table(hdul["EPSF"], offset, "EPSF_EMBEDDED", "EPSF table in product")
            elif epsf_calibration is not None:
                cal_det, arrays, centers, ovs = _calibration_arrays(str(epsf_calibration), "epsf")
                if detector is not None and cal_det and cal_det != str(detector):
                    raise ValueError(f"ePSF calibration is for detector {cal_det}, image is detector {detector}")
                psf = EpsfModel(arrays, centers, ovs, offset, "EPSF_CALIBRATION", f"ePSF calibration {Path(epsf_calibration).name}")
            elif "PSF" in names and hdul["PSF"].data is not None and hdul["PSF"].data.ndim == 3:
                psf = _legacy_psf(hdul["PSF"], offset, "legacy PSF cube in product (IRSA advises the ePSF instead)")
            elif _num(ih, "PSF_FWHM"):
                psf = gaussian_from_fwhm(_num(ih, "PSF_FWHM"), pixel_scale, "Gaussian with header PSF_FWHM (median)")
        except (ValueError, KeyError, OSError) as exc:
            qc.add("psf_model", "FAILED", f"PSF model unusable: {exc}")
            psf = None
        if psf is None:
            qc.add("psf_model", "FAILED", "no usable PSF model")
            for analysis in ("detection", "photometry", "astrometry"):
                qc.unsuitable(analysis, "PSF_MODEL_UNAVAILABLE")
        else:
            qc.add("psf_model", "WARNING" if psf.approximation else "PASSED", f"{psf.kind}: {psf.source}")

        cwave = cband = None
        wavelength_source = "WCS-WAVE" if spectral is not None else None
        if spectral_wcs_calibration is not None:
            try:
                cwave, cband = _calibration_arrays(str(spectral_wcs_calibration), "spectral_wcs")
                wavelength_source = "CWAVE_CBAND"
                qc.add("spectral_calibration", "PASSED", f"CWAVE/CBAND from {Path(spectral_wcs_calibration).name}")
            except (OSError, KeyError, ValueError) as exc:
                qc.add("spectral_calibration", "FAILED", f"spectral WCS calibration unusable: {exc}")
        if wavelength_source is None:
            qc.unsuitable("spectral", "WAVELENGTH_NOT_ESTABLISHED")

        solid_angle_map = None
        solid_angle_source = "OMEGA_MEDIAN header" if _num(ih, "OMEGA_MEDIAN") else "WCS pixel scale squared (approximation)"
        if solid_angle_calibration is not None:
            try:
                solid_angle_map, = _calibration_arrays(str(solid_angle_calibration), "solid_angle")
                solid_angle_source = "SOLID_ANGLE_PIXEL_MAP"
            except (OSError, KeyError, ValueError) as exc:
                qc.add("solid_angle_calibration", "FAILED", f"solid angle map unusable: {exc}")

        sc = vel = None
        if all(_num(ih, k) is not None for k in ("X_SC", "Y_SC", "Z_SC")):
            sc = (_num(ih, "X_SC"), _num(ih, "Y_SC"), _num(ih, "Z_SC"))
        if all(_num(ih, k) is not None for k in ("VX_SC", "VY_SC", "VZ_SC")):
            vel = (_num(ih, "VX_SC"), _num(ih, "VY_SC"), _num(ih, "VZ_SC"))

        calibration_ids = [str(card).split("[CALIB]", 1)[1].strip() for card in ih.get("HISTORY", []) if "[CALIB]" in str(card)]
        observation_id = str(ih.get("OBSID")).strip() if ih.get("OBSID") else None
        if obs_id is None:
            obs_id = f"{observation_id}D{detector}" if observation_id and detector else Path(path).stem

        meta = ImageMeta(
            obs_id=obs_id, observation_id=observation_id, detector=detector,
            release=release or release_from_path(path),
            pipeline_version=str(primary.get("VERSION")) if primary.get("VERSION") else None,
            mjd_avg=mjd_avg, mjd_beg=mjd_beg, mjd_end=mjd_end, exposure_s=_num(ih, "XPOSURE"), finast=finast, dqa=dqa,
            omega_median_arcsec2=_num(ih, "OMEGA_MEDIAN"), omega_std_arcsec2=_num(ih, "OMEGA_STDDEV"),
            psf_fwhm_arcsec=_num(ih, "PSF_FWHM"), pixel_scale_arcsec=pixel_scale, spacecraft_geocentric_km=sc,
            spacecraft_velocity_km_s=vel,
            parent_offset=offset, full_shape=full_shape, window=(x0, x1, y0, y1), calibration_ids=calibration_ids,
            synthetic=synthetic, psf=psf.describe() if psf else None, wavelength_source=wavelength_source,
            solid_angle_source=solid_angle_source, flag_vocabulary=dict(vocabulary.bits),
            undefined_policy_flags=list(masks.undefined), path=path,
        )
        if masks.undefined:
            qc.add("flag_policy", "WARNING", f"policy flags not defined by this product: {', '.join(masks.undefined)}")

    spectral_image = SpectralImage(image=image, variance=variance, flags=flags, zodi=zodi, wcs=celestial,
                                   spectral_wcs=spectral, vocabulary=vocabulary, masks=masks, psf=psf, meta=meta,
                                   origin=(x0, y0), cwave=cwave, cband=cband, solid_angle_map=solid_angle_map)
    if detector in DETECTOR_BANDS_UM:
        lam, _, src = spectral_image.wavelength_at((x0 + x1 - 1) / 2.0, (y0 + y1 - 1) / 2.0)
        lo, hi = DETECTOR_BANDS_UM[detector]
        if src is not None and not (lo - 0.1 <= lam <= hi + 0.1):
            qc.add("wavelength_range", "WARNING", f"wavelength {lam:.4f} um is outside the nominal D{detector} band {lo}-{hi} um")
    observation = build_observation(spectral_image, source_id, provenance)
    return IngestResult(image=None if qc.excluded else spectral_image, observation=observation, qc=qc)


def build_observation(img: SpectralImage, source_id: str, provenance: Provenance) -> Observation:
    m = img.meta
    md = {}
    if m.observation_id:
        md["observation_id"] = source_reported(m.observation_id, ValueClass.IDENTIFIER, FITS_SOURCE)
    if m.detector is not None:
        md["detector"] = source_reported(str(m.detector), ValueClass.CATEGORICAL, FITS_SOURCE)
    md["release"] = source_reported(m.release, ValueClass.CATEGORICAL, "retrieval path / collection")
    if m.pipeline_version:
        md["pipeline_version"] = source_reported(m.pipeline_version, ValueClass.CATEGORICAL, FITS_SOURCE)
    if m.mjd_avg is not None:
        md["mjd_avg_utc"] = source_reported(m.mjd_avg, ValueClass.TIME, FITS_SOURCE, unit="d (MJD, UTC)")
    if m.exposure_s is not None:
        md["exposure"] = source_reported(m.exposure_s, ValueClass.QUANTITY, FITS_SOURCE, unit="s", uncertainty=Uncertainty.unavailable())
    if m.finast is not None:
        md["finast"] = source_reported(str(m.finast), ValueClass.CATEGORICAL, FITS_SOURCE)
    if m.dqa:
        md["l2_dqa"] = source_reported(m.dqa, ValueClass.CATEGORICAL, FITS_SOURCE)
    if m.omega_median_arcsec2 is not None:
        md["pixel_solid_angle_median"] = source_reported(m.omega_median_arcsec2, ValueClass.QUANTITY, FITS_SOURCE,
                                                         unit="arcsec2", uncertainty=Uncertainty.unavailable())
    if m.psf_fwhm_arcsec is not None:
        md["psf_fwhm_median"] = source_reported(m.psf_fwhm_arcsec, ValueClass.QUANTITY, FITS_SOURCE, unit="arcsec",
                                                uncertainty=Uncertainty.unavailable())
    x0, x1, y0, y1 = img.window
    lam, bw, src = img.wavelength_at((x0 + x1 - 1) / 2.0, (y0 + y1 - 1) / 2.0)
    if src is not None and math.isfinite(lam):
        method = "cwave_lookup" if src == "CWAVE_CBAND" else "wcs_wave_lookup"
        table = "spectral_wcs_calibration" if src == "CWAVE_CBAND" else "wcs_wave_table"
        refs = {"pixel_position": f"{m.obs_id}:window_centre", table: m.obs_id}
        md["wavelength_at_centre"] = derived_quantity(lam, method, refs, unit="um", uncertainty=Uncertainty.not_established())
        md["bandwidth_at_centre"] = derived_quantity(bw, method, refs, unit="um", uncertainty=Uncertainty.not_established())
    combined = int(np.bitwise_or.reduce(img.flags.ravel())) if img.flags.size else 0
    flag_names = sorted(img.vocabulary.names_in(combined))
    return Observation(obs_id=m.obs_id, source_id=source_id, provenance=provenance, metadata=md, quality_flags=tuple(flag_names))
