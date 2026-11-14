"""Shared helpers for engine tests. Everything generated here is SYNTHETIC (software tests only)."""

from __future__ import annotations

import atexit
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from heimdall import __version__
from heimdall.contracts import DataOrigin, Provenance
from heimdall.engine.detection import DetectionRecord
from heimdall.synthetic import GeneratedSet, Scenario, generate

_CACHE: dict[str, GeneratedSet] = {}


def small_scenario(**overrides) -> Scenario:
    base = dict(npix=96, n_stars=30, visits_per_epoch=3, seed=11)
    base.update(overrides)
    return Scenario(**base)


def generated(name: str = "small", **overrides) -> GeneratedSet:
    """Generate (once per process) a small synthetic product set in a temporary directory."""
    if name not in _CACHE:
        root = Path(tempfile.mkdtemp(prefix=f"heimdall-test-{name}-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        _CACHE[name] = generate(root / "products", small_scenario(**overrides))
    return _CACHE[name]


def synthetic_provenance() -> Provenance:
    return Provenance(origin=DataOrigin.SYNTHETIC_TEST_FIXTURE, retrieved_at=datetime.now(UTC), software_version=__version__)


def make_det(det_id: str, obs_id: str, ra: float, dec: float, *, sigma_arcsec: float = 0.2, snr: float = 50.0,
             mjd: float = 61000.0, grade: str = "A", flags=(), neighbour_px=None, flux: float = 1000.0) -> DetectionRecord:
    cov = (np.eye(2) * sigma_arcsec ** 2).tolist()
    return DetectionRecord(
        det_id=det_id, obs_id=obs_id, x=0.0, y=0.0, ra=ra, dec=dec, cov_sky_arcsec2=cov, sigma_major_arcsec=sigma_arcsec,
        astrometry_usable=True, amplitude=1.0, amplitude_sigma=1.0 / snr, flux_ujy=flux, flux_sigma_ujy=flux / snr,
        solid_angle_sr=8.9e-10, solid_angle_source="test", snr_peak=snr, snr_fit=snr, chi2_red=1.0, n_fit_pixels=121,
        masked_fraction=0.0, fit_status="CONVERGED", fit_note="", peak_flags=list(flags), footprint_warning_flags=[],
        wavelength_um=1.2, bandwidth_um=0.03, wavelength_source="WCS-WAVE", mjd=mjd, detector=2, quality_grade=grade,
        nearest_neighbour_px=neighbour_px)
