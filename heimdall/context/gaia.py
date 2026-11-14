"""Gaia DR3 stars from IRSA TAP (table ``gaia_dr3_source``).

Positions are propagated from ``ref_epoch`` (Julian year, 2016.0 for DR3) to the subject's mean
epoch with the catalogue proper motion. A source matches a star when
``d^2 = delta^T (C_src + C_star + s^2 I)^-1 delta <= chi2_2^-1(gate)``. Parallax (at most about 0.8")
is added to the star's variance rather than modelled. "No match" means no star brighter than
``g_max`` within the gate. It does not mean no star exists: Gaia is incomplete near G ~ 21.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field

import numpy as np
from scipy import stats as _st

from ..engine.skygeom import angular_separation_deg, offsets_arcsec
from . import CachedService, form

TAP_SYNC = "https://irsa.ipac.caltech.edu/TAP/sync"
ESA_TAP_SYNC = "https://gea.esac.esa.int/tap-server/tap/sync"
# (label, endpoint, table). The ESA archive is the Gaia mission's own service and serves as the fallback when IRSA does not answer.
GAIA_ENDPOINTS = (("gaia_dr3_source@IRSA", TAP_SYNC, "gaia_dr3_source"), ("gaiadr3.gaia_source@ESA", ESA_TAP_SYNC, "gaiadr3.gaia_source"))
COLUMNS = ("source_id", "ra", "dec", "ra_error", "dec_error", "pmra", "pmdec", "pmra_error", "pmdec_error", "parallax",
           "parallax_error", "phot_g_mean_mag", "ref_epoch")


@dataclass
class Star:
    source_id: str
    ra: float
    dec: float
    ra_error_mas: float
    dec_error_mas: float
    pmra: float | None
    pmdec: float | None
    pmra_error: float | None
    pmdec_error: float | None
    parallax: float | None
    parallax_error: float | None
    g_mag: float | None
    ref_epoch: float

    def at_epoch(self, mjd: float) -> tuple[float, float, np.ndarray]:
        """Position (deg) at an MJD and its covariance (arcsec^2) from position, proper-motion and parallax terms."""
        dt = (2000.0 + (mjd - 51544.5) / 365.25) - self.ref_epoch
        pmra, pmdec = (self.pmra or 0.0), (self.pmdec or 0.0)
        dec = self.dec + pmdec * dt / 3.6e6
        ra = (self.ra + pmra * dt / 3.6e6 / max(math.cos(math.radians(self.dec)), 1e-6)) % 360.0
        var_ra = (self.ra_error_mas / 1e3) ** 2 + ((self.pmra_error or 0.0) * dt / 1e3) ** 2
        var_dec = (self.dec_error_mas / 1e3) ** 2 + ((self.pmdec_error or 0.0) * dt / 1e3) ** 2
        plx = ((self.parallax or 0.0) / 1e3) ** 2
        return ra, dec, np.diag([var_ra + plx, var_dec + plx])

    @property
    def pm_arcsec_yr(self) -> tuple[float, float] | None:
        if self.pmra is None or self.pmdec is None:
            return None
        return self.pmra / 1e3, self.pmdec / 1e3


@dataclass
class CatalogueQuery:
    service: str
    status: str
    reason: str
    from_cache: bool
    n_stars: int
    g_max: float
    region: list[float]
    stars: list[Star] = field(default_factory=list)


def _num(text: str) -> float | None:
    try:
        v = float(text)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_stars(text: str) -> list[Star]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or any(c not in reader.fieldnames for c in COLUMNS):
        raise ValueError(f"Gaia response lacks columns: {[c for c in COLUMNS if c not in (reader.fieldnames or [])]}")
    out = []
    for row in reader:
        ra, dec = _num(row["ra"]), _num(row["dec"])
        if ra is None or dec is None:
            continue
        out.append(Star(source_id=row["source_id"], ra=ra, dec=dec, ra_error_mas=_num(row["ra_error"]) or 0.0,
                        dec_error_mas=_num(row["dec_error"]) or 0.0, pmra=_num(row["pmra"]), pmdec=_num(row["pmdec"]),
                        pmra_error=_num(row["pmra_error"]), pmdec_error=_num(row["pmdec_error"]),
                        parallax=_num(row["parallax"]), parallax_error=_num(row["parallax_error"]),
                        g_mag=_num(row["phot_g_mean_mag"]), ref_epoch=_num(row["ref_epoch"]) or 2016.0))
    return out


class GaiaCatalogue:
    def __init__(self, service: CachedService, endpoint: tuple[str, str, str] | None = None) -> None:
        self.service = service
        self.endpoint = endpoint or GAIA_ENDPOINTS[0]

    def cone(self, ra: float, dec: float, radius_deg: float, g_max: float) -> CatalogueQuery:
        label, url, table = self.endpoint
        adql = (f"SELECT {', '.join(COLUMNS)} FROM {table} WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), "
                f"CIRCLE('ICRS', {ra:.8f}, {dec:.8f}, {radius_deg:.6f})) AND phot_g_mean_mag < {g_max:.2f}")
        body = form({"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": adql})

        def validate(raw: bytes):
            text = raw.decode("utf-8", errors="replace")
            if text.lstrip().startswith("<"):
                return "TAP returned an error document"
            try:
                parse_stars(text)
            except ValueError as exc:
                return str(exc)
            return None

        ans = self.service.fetch(url, body, validate=validate)
        if ans.status != "COMPLETED":
            return CatalogueQuery(label, ans.status, ans.reason, False, 0, g_max, [ra, dec, radius_deg])
        stars = parse_stars(ans.body.decode("utf-8", errors="replace"))
        return CatalogueQuery(label, "COMPLETED", "", ans.from_cache, len(stars), g_max,
                              [ra, dec, radius_deg], stars)


def match_position(ra: float, dec: float, cov_arcsec2, mjd: float, stars: list[Star], floor_arcsec: float,
                   gate_probability: float) -> dict:
    gate = float(_st.chi2.ppf(gate_probability, 2))
    best, n_in = None, 0
    for s in stars:
        if angular_separation_deg(ra, dec, s.ra, s.dec) * 3600.0 > 60.0:
            continue
        sra, sdec, scov = s.at_epoch(mjd)
        delta = offsets_arcsec(sra, sdec, ra, dec)
        total = np.asarray(cov_arcsec2, dtype=float) + scov + floor_arcsec ** 2 * np.eye(2)
        d2 = float(delta @ np.linalg.solve(total, delta))
        if d2 <= gate:
            n_in += 1
            if best is None or d2 < best[0]:
                best = (d2, s, float(np.hypot(*delta)))
    if best is None:
        return {"status": "NO_MATCH", "n_within_gate": 0}
    d2, s, sep = best
    return {"status": "MATCH", "n_within_gate": n_in, "gaia_source_id": s.source_id, "separation_arcsec": sep, "d2": d2,
            "g_mag": s.g_mag, "pm_mas_yr": [s.pmra, s.pmdec], "pm_error_mas_yr": [s.pmra_error, s.pmdec_error],
            "parallax_mas": s.parallax, "parallax_error_mas": s.parallax_error}
