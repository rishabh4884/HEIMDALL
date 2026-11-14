"""Synthetic SPHEREx-like products for demonstrations and software tests.

EVERYTHING WRITTEN HERE IS SYNTHETIC AND IS NOT ASTRONOMICAL DATA.

Every file carries ``SYNTHETC = T`` and ``ORIGIN`` = "HEIMDALL SYNTHETIC
GENERATOR - NOT REAL DATA" in the primary header and an ``OBSID`` starting with
``SYNTHETIC``; ingestion and the local-fits adapter label such products
``SYNTHETIC_TEST_FIXTURE``. A ``SYNTHETIC_TRUTH.json`` file lists every
injected signal so tests can check recovery.

The layout mirrors SPHEREx Level 2 files
(IMAGE/FLAGS/VARIANCE/ZODI, ``MP_<NAME>`` flag bits as in a real QR2 header,
``WAVE-TAB`` lookup in ``WCS-WAVE``, an ``EPSF`` table like the QR3 product).
The scene is idealised: Gaussian PSF, white noise matching VARIANCE, flat
backgrounds, linear wavelength gradients. Field centre and every source are
invented; the default field centre is arbitrary.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from scipy.special import erf

from .engine.ephemeris import observer_heliocentric_au
from .engine.psf import GaussianPsfModel
from .engine.skygeom import ARCSEC2_TO_SR, angular_separation_deg, inverse_gnomonic, radec_from_vector, unit_vector

ORIGIN_TEXT = "HEIMDALL SYNTHETIC GENERATOR - NOT REAL DATA"
# Flag bit numbers as defined by a real SPHEREx QR2 FLAGS header (MP_<NAME> keywords).
FLAG_BITS = {"TRANSIENT": 0, "OVERFLOW": 1, "SUR_ERROR": 2, "PHANTOM": 4, "REFERENCE": 5, "NONFUNC": 6, "DICHROIC": 7,
             "MISSING_DATA": 9, "HOT": 10, "COLD": 11, "FULLSAMPLE": 12, "PHANMISS": 14, "NONLINEAR": 15, "PERSIST": 17,
             "OUTLIER": 19, "SOURCE": 21}
VISIT_WAVELENGTHS_UM = {1: (0.80, 0.88, 0.96, 1.04), 2: (1.15, 1.28, 1.41, 1.54)}
RESOLVING_POWER = 41.0


@dataclass(frozen=True)
class Scenario:
    seed: int = 7
    ra0: float = 30.0
    dec0: float = -10.0
    npix: int = 128
    pixel_scale_arcsec: float = 6.15
    detectors: tuple[int, ...] = (1, 2)
    epochs_mjd: tuple[float, ...] = (61000.0, 61182.6, 61365.3)
    visits_per_epoch: int = 4
    visit_spacing_days: float = 0.5
    n_stars: int = 60
    star_flux_ujy: tuple[float, float] = (1500.0, 30000.0)
    noise_sigma: float = 0.02
    background: float = 0.30
    psf_fwhm_arcsec: float = 6.0
    wavelength_gradient_um_per_px: float = 1e-4
    variable_flux_ujy: float = 8000.0
    variable_amplitude: float = 0.4
    variable_period_days: float = 97.0
    mover_distance_au: float = 1200.0
    mover_flux_ujy: float = 6000.0
    transient_flux_ujy: float = 9000.0
    transient_epoch: int = 1
    transient_visit: int = 2
    hot_pixel_fraction: float = 0.002
    cosmic_rays_per_image: int = 1
    pointing_jitter_px: float = 2.0
    epoch_rotation_deg: tuple[float, ...] = (0.0, 25.0, 50.0)
    min_source_separation_px: float = 5.0


@dataclass
class GeneratedSet:
    directory: Path
    files: list[Path]
    truth_path: Path
    truth: dict = field(default_factory=dict)


def _epsf_array(sigma_px: float, ovs: int = 5, half: int = 12) -> np.ndarray:
    """Pixel-integrated Gaussian sampled at 1/ovs pixel steps (an effective PSF), unit sum like the QR3 product."""
    k = np.arange(-half, half + 1) / ovs
    s = math.sqrt(2.0) * sigma_px
    f = 0.5 * (erf((k + 0.5) / s) - erf((k - 0.5) / s))
    arr = np.outer(f, f)
    return arr / arr.sum()


def _wave_table(npix: int, centre_um: float, gradient: float) -> fits.BinTableHDU:
    idx = np.rint(np.linspace(1, npix, 11)).astype(np.int32)
    values = np.zeros((11, 11, 2), dtype=np.float32)
    for iy, y in enumerate(idx):
        lam = centre_um + gradient * (y - (npix + 1) / 2.0)
        values[iy, :, 0] = lam
        values[iy, :, 1] = lam / RESOLVING_POWER
    cols = [fits.Column(name="X", format="11J", dim="(11)", array=idx[None, :]),
            fits.Column(name="Y", format="11J", dim="(11)", array=idx[None, :]),
            fits.Column(name="VALUES", format="242E", dim="(2,11,11)", array=values[None])]
    return fits.BinTableHDU.from_columns(cols, name="WCS-WAVE")


def _wcs_header(h: fits.Header, ra_c: float, dec_c: float, npix: int, scale_deg: float, rot_deg: float) -> None:
    c, s = math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg))
    h["RADESYS"] = "ICRS"
    h["CTYPE1"], h["CTYPE2"] = "RA---TAN", "DEC--TAN"
    h["CUNIT1"], h["CUNIT2"] = "deg", "deg"
    h["CRPIX1"] = h["CRPIX2"] = (npix + 1) / 2.0
    h["CRVAL1"], h["CRVAL2"] = ra_c, dec_c
    h["CD1_1"], h["CD1_2"] = -scale_deg * c, scale_deg * s
    h["CD2_1"], h["CD2_2"] = scale_deg * s, scale_deg * c
    # Wavelength lookup, same keyword layout as real SPHEREx L2 headers.
    for k, v in (("WCSAXESW", 2), ("WCSNAMEW", "Approximated per-pixel wavelength and bandwidth"), ("CNAME1W", "Wavelength"),
                 ("CNAME2W", "Bandwidth"), ("CRPIX1W", 1.0), ("CRPIX2W", 1.0), ("CDELT1W", 1), ("CDELT2W", 1),
                 ("CUNIT1W", "um"), ("CUNIT2W", "um"), ("CTYPE1W", "WAVE-TAB"), ("CTYPE2W", "WAVE-TAB"), ("CRVAL1W", 1),
                 ("CRVAL2W", 1), ("PV1_3W", 1), ("PV2_3W", 2), ("PS1_0W", "WCS-WAVE"), ("PS2_0W", "WCS-WAVE"),
                 ("PS1_1W", "VALUES"), ("PS2_1W", "VALUES"), ("PS1_2W", "X"), ("PS2_2W", "Y")):
        h[k] = v


def generate(directory: Path | str, scenario: Scenario | None = None) -> GeneratedSet:
    sc = scenario or Scenario()
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(sc.seed)
    scale_deg = sc.pixel_scale_arcsec / 3600.0
    sigma_px = sc.psf_fwhm_arcsec / sc.pixel_scale_arcsec / (2.0 * math.sqrt(2.0 * math.log(2.0)))
    psf = GaussianPsfModel(sigma_px, "synthetic Gaussian")
    omega_sr = (sc.pixel_scale_arcsec ** 2) * ARCSEC2_TO_SR
    half_field = 0.42 * sc.npix * scale_deg

    var_ra, var_dec = inverse_gnomonic(0.15 * half_field, -0.2 * half_field, sc.ra0, sc.dec0)
    tr_ra, tr_dec = inverse_gnomonic(-0.3 * half_field, 0.25 * half_field, sc.ra0, sc.dec0)

    visits = []
    for e, t0 in enumerate(sc.epochs_mjd):
        for v in range(sc.visits_per_epoch):
            visits.append((e, v, t0 + v * sc.visit_spacing_days))
    mjds = np.array([t for _, _, t in visits])
    observer = observer_heliocentric_au(mjds)
    u0 = unit_vector(sc.ra0 + 0.1 * half_field / math.cos(math.radians(sc.dec0)), sc.dec0 - 0.1 * half_field)
    tangential = np.cross(u0, [0.0, 0.0, 1.0])
    tangential /= np.linalg.norm(tangential)
    v_circ = 2.0 * math.pi / math.sqrt(sc.mover_distance_au)  # AU/yr, circular orbital speed
    body = sc.mover_distance_au * u0[None, :] + np.outer((mjds - mjds[0]) / 365.25, tangential * v_circ)
    mover_ra, mover_dec = radec_from_vector(body - observer)

    # Stars keep a minimum separation from each other and from every special signal (clean demonstration scene).
    taken_ra = [float(var_ra), float(tr_ra)] + [float(r) for r in mover_ra]
    taken_dec = [float(var_dec), float(tr_dec)] + [float(d) for d in mover_dec]
    min_sep = sc.min_source_separation_px * scale_deg
    stars = []
    for _ in range(200 * sc.n_stars):
        if len(stars) >= sc.n_stars:
            break
        xi, eta = rng.uniform(-half_field, half_field, 2)
        ra, dec = (float(v) for v in inverse_gnomonic(xi, eta, sc.ra0, sc.dec0))
        if np.min(angular_separation_deg(ra, dec, np.array(taken_ra), np.array(taken_dec))) < min_sep:
            continue
        lo, hi = np.log(sc.star_flux_ujy[0]), np.log(sc.star_flux_ujy[1])
        stars.append({"ra": ra, "dec": dec, "flux_ujy": float(np.exp(rng.uniform(lo, hi))),
                      "spectral_index": float(rng.uniform(-1.0, 1.0))})
        taken_ra.append(ra)
        taken_dec.append(dec)

    truth = {"synthetic": True, "warning": "SYNTHETIC SIGNALS FOR SOFTWARE TESTING - NOT ASTRONOMICAL DATA",
             "scenario": asdict(sc), "stars": stars,
             "variable": {"ra": float(var_ra), "dec": float(var_dec), "mean_flux_ujy": sc.variable_flux_ujy,
                          "amplitude": sc.variable_amplitude, "period_days": sc.variable_period_days},
             "mover": {"distance_au": sc.mover_distance_au, "flux_ujy": sc.mover_flux_ujy,
                       "orbital_speed_au_per_yr": v_circ, "positions": []},
             "transient": {"ra": float(tr_ra), "dec": float(tr_dec), "flux_ujy": sc.transient_flux_ujy,
                           "epoch": sc.transient_epoch, "visit": sc.transient_visit, "obs_ids": []},
             "cosmic_rays": []}

    files = []
    for k, (e, v, mjd) in enumerate(visits):
        rot = sc.epoch_rotation_deg[e % len(sc.epoch_rotation_deg)]
        jx, jy = rng.normal(0, sc.pointing_jitter_px, 2) * scale_deg
        ra_c, dec_c = inverse_gnomonic(jx, jy, sc.ra0, sc.dec0)
        for det in sc.detectors:
            obsid = f"SYNTHETIC_E{e}V{v}"
            obs_id = f"{obsid}D{det}"
            lam_c = VISIT_WAVELENGTHS_UM.get(det, (1.0,) * 4)[v % 4]
            header = fits.Header()
            _wcs_header(header, float(ra_c), float(dec_c), sc.npix, scale_deg, rot)
            wcs = WCS(header).celestial

            def pix(ra, dec):
                x, y = wcs.all_world2pix([[ra, dec]], 0)[0]
                return float(x), float(y)

            image = np.full((sc.npix, sc.npix), sc.background)
            flags = np.zeros((sc.npix, sc.npix), dtype=np.int32)
            sources = []
            for s in stars:
                x, y = pix(s["ra"], s["dec"])
                lam = lam_c + sc.wavelength_gradient_um_per_px * (y + 1 - (sc.npix + 1) / 2.0)
                sources.append((x, y, s["flux_ujy"] * (lam / 1.0) ** s["spectral_index"]))
            phase = 2 * math.pi * (mjd - sc.epochs_mjd[0]) / sc.variable_period_days
            sources.append((*pix(var_ra, var_dec), sc.variable_flux_ujy * (1 + sc.variable_amplitude * math.sin(phase))))
            mx, my = pix(float(mover_ra[k]), float(mover_dec[k]))
            sources.append((mx, my, sc.mover_flux_ujy))
            if det == sc.detectors[0]:
                truth["mover"]["positions"].append({"mjd": float(mjd), "ra": float(mover_ra[k]), "dec": float(mover_dec[k])})
            if e == sc.transient_epoch and v == sc.transient_visit:
                sources.append((*pix(tr_ra, tr_dec), sc.transient_flux_ujy))
                truth["transient"]["obs_ids"].append(obs_id)
            for x, y, flux in sources:
                if -5 <= x < sc.npix + 5 and -5 <= y < sc.npix + 5:
                    stamp, x_ll, y_ll = psf.stamp(x, y, 15)
                    xs0, ys0 = max(x_ll, 0), max(y_ll, 0)
                    xs1, ys1 = min(x_ll + 15, sc.npix), min(y_ll + 15, sc.npix)
                    if xs0 < xs1 and ys0 < ys1:
                        amp = flux / (omega_sr * 1e12)
                        image[ys0:ys1, xs0:xs1] += amp * stamp[ys0 - y_ll:ys1 - y_ll, xs0 - x_ll:xs1 - x_ll]
                        core = stamp[ys0 - y_ll:ys1 - y_ll, xs0 - x_ll:xs1 - x_ll] * amp > 3 * sc.noise_sigma
                        flags[ys0:ys1, xs0:xs1] |= np.where(core, 1 << FLAG_BITS["SOURCE"], 0).astype(np.int32)
            image += rng.normal(0.0, sc.noise_sigma, image.shape)
            hot = rng.random(image.shape) < sc.hot_pixel_fraction
            image[hot] += rng.uniform(1.0, 5.0, int(hot.sum()))
            flags[hot] |= 1 << FLAG_BITS["HOT"]
            for _ in range(sc.cosmic_rays_per_image):
                cx, cy = (int(c) for c in rng.integers(4, sc.npix - 4, 2))
                image[cy, cx] += 2.0
                flags[cy, cx] |= 1 << FLAG_BITS["TRANSIENT"]
                truth["cosmic_rays"].append({"obs_id": obs_id, "x": cx, "y": cy})

            primary = fits.PrimaryHDU()
            primary.header["VERSION"] = ("heimdall-synthetic-1", "SYNTHETIC generator version")
            primary.header["SYNTHETC"] = (True, "SYNTHETIC PRODUCT - NOT REAL DATA")
            primary.header["ORIGIN"] = ORIGIN_TEXT
            ih = fits.ImageHDU(image.astype(np.float32), header=header, name="IMAGE")
            for key, value in (("BUNIT", "MJy / sr"), ("DETECTOR", det), ("OBSID", obsid), ("TIMESYS", "UTC"),
                               ("MJD-BEG", mjd - 55 / 86400), ("MJD-AVG", mjd), ("MJD-END", mjd + 55 / 86400),
                               ("XPOSURE", 110.0), ("FINAST", 0), ("L2DQAFLG", "Pass"), ("PSF_FWHM", sc.psf_fwhm_arcsec),
                               ("HIERARCH OMEGA_MEDIAN", sc.pixel_scale_arcsec ** 2), ("ORIGIN", ORIGIN_TEXT)):
                ih.header[key] = value
            fh = fits.ImageHDU(flags, name="FLAGS")
            fh.header["BUNIT"] = ""
            for name, bit in FLAG_BITS.items():
                fh.header[f"HIERARCH MP_{name}"] = bit
            vh = fits.ImageHDU(np.full(image.shape, sc.noise_sigma ** 2, dtype=np.float32), name="VARIANCE")
            vh.header["BUNIT"] = "MJy2 / sr2"
            zh = fits.ImageHDU(np.full(image.shape, sc.background, dtype=np.float32), name="ZODI")
            zh.header["BUNIT"] = "MJy / sr"
            epsf = _epsf_array(sigma_px)
            centre = (sc.npix - 1) / 2.0
            eh = fits.BinTableHDU.from_columns([
                fits.Column(name="XCENTER", format="E", array=np.array([centre], dtype=np.float32)),
                fits.Column(name="YCENTER", format="E", array=np.array([centre], dtype=np.float32)),
                fits.Column(name="EPSF", format=f"{epsf.size}D", dim=f"({epsf.shape[1]},{epsf.shape[0]})", array=epsf[None])],
                name="EPSF")
            eh.header["OVSMPX"], eh.header["OVSMPY"], eh.header["DETECTOR"] = 5, 5, str(det)
            path = out / f"synthetic_{obs_id}.fits"
            fits.HDUList([primary, ih, fh, vh, zh, _wave_table(sc.npix, lam_c, sc.wavelength_gradient_um_per_px), eh]).writeto(
                path, overwrite=True)
            files.append(path)
    truth_path = out / "SYNTHETIC_TRUTH.json"
    truth_path.write_text(json.dumps(truth, indent=1), encoding="utf-8")
    return GeneratedSet(directory=out, files=files, truth_path=truth_path, truth=truth)
