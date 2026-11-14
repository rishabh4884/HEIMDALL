"""Reference astrometry for a target and its epoch-dependent expected position.

Sources: Gaia DR3 (``gaia_dr3_source`` through IRSA TAP) and CatWISE2020 (``catwise_2020``). The expected
position at an observation time combines the catalogue position (with its reference epoch), proper motion
(full space-motion propagation with astropy when a parallax exists) and annual parallax computed from the
Earth's barycentric position. SPHEREx orbits close to the Earth, so the Earth's heliocentric motion is the
relevant parallax baseline; the spacecraft's offset from the geocentre (about 0.0001 AU) is far below the
precision of any measurement here and is ignored (stated limitation).

Uncertainty: catalogue position and proper-motion errors propagate linearly. Radial velocity is unknown
and set to zero (its effect on position over a year is below a milliarcsecond even for Barnard's Star).
No value is invented: if a proper motion is missing and a documented fallback exists, the result says so
in ``pm_source``; otherwise the target is unresolved for motion purposes.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import asdict, dataclass, field

from ..context import CachedService, form
from ..engine.skygeom import angular_separation_deg
from .registry import TargetSpec

TAP_SYNC = "https://irsa.ipac.caltech.edu/TAP/sync"
# CatWISE2020 positions ra_pm/dec_pm refer to this epoch (Marocco et al. 2021); checked below against the
# NASA reference position of WISE 0855 where available.
CATWISE_REFERENCE_MJD = 57170.0
W2_AB_OFFSET = 3.339  # Vega -> AB for WISE W2 (Jarrett et al. 2011)
W1_AB_OFFSET = 2.699
MJD_J2000 = 51544.5


def mjd_to_jyear(mjd: float) -> float:
    return 2000.0 + (mjd - MJD_J2000) / 365.25


def jyear_to_mjd(jy: float) -> float:
    return MJD_J2000 + (jy - 2000.0) * 365.25


@dataclass
class ExpectedPosition:
    ra_deg: float
    dec_deg: float
    cov_arcsec2: list[list[float]]  # (ra*cos(dec), dec) frame
    mjd: float
    epoch_jyear: float
    components: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReferenceAstrometry:
    target_id: str
    catalogue: str
    status: str  # RESOLVED | UNRESOLVED | AMBIGUOUS | SERVICE_FAILED
    reason: str = ""
    source_id: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    epoch_jyear: float | None = None
    epoch_mjd: float | None = None
    pmra_mas_yr: float | None = None  # includes cos(dec)
    pmdec_mas_yr: float | None = None
    pmra_err_mas_yr: float = 0.0
    pmdec_err_mas_yr: float = 0.0
    ra_err_mas: float = 0.0
    dec_err_mas: float = 0.0
    parallax_mas: float | None = None
    parallax_err_mas: float | None = None
    magnitudes: dict = field(default_factory=dict)
    pm_source: str = ""
    selection_rule: dict = field(default_factory=dict)
    alternatives: list[dict] = field(default_factory=list)
    service: dict = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.status in ("RESOLVED", "AMBIGUOUS", "COORDINATE_ONLY") and self.ra_deg is not None

    @property
    def has_proper_motion(self) -> bool:
        return self.pmra_mas_yr is not None and self.pmdec_mas_yr is not None

    def to_dict(self) -> dict:
        return asdict(self)

    def at_mjd(self, mjd: float) -> ExpectedPosition:
        """Propagate to an observation time. Raises ValueError if the target is not resolved."""
        if not self.resolved:
            raise ValueError("reference astrometry is not resolved")
        from astropy import units as u
        from astropy.coordinates import SkyCoord, get_body_barycentric
        from astropy.time import Time

        t_ref = Time(self.epoch_jyear, format="jyear")
        t_obs = Time(mjd, format="mjd")
        dt = mjd_to_jyear(mjd) - self.epoch_jyear
        pmra, pmdec = (self.pmra_mas_yr or 0.0), (self.pmdec_mas_yr or 0.0)
        plx_ok = bool(self.parallax_mas and self.parallax_mas >= 1.0)
        if plx_ok:
            c0 = SkyCoord(ra=self.ra_deg * u.deg, dec=self.dec_deg * u.deg, pm_ra_cosdec=pmra * u.mas / u.yr, pm_dec=pmdec * u.mas / u.yr,
                          distance=(1000.0 / self.parallax_mas) * u.pc, radial_velocity=0.0 * u.km / u.s, obstime=t_ref)
            c1 = c0.apply_space_motion(new_obstime=t_obs)
            ra, dec = float(c1.ra.deg), float(c1.dec.deg)
        else:
            # Without a usable parallax the perspective term is undefined; first-order tangent-plane motion is exact to well below a mas here.
            dec = self.dec_deg + pmdec * dt / 3.6e6
            ra = self.ra_deg + pmra * dt / 3.6e6 / max(math.cos(math.radians(self.dec_deg)), 1e-9)
        # Annual parallax (not part of apply_space_motion): offsets in (ra*cos dec, dec), arcsec.
        plx_ra = plx_dec = 0.0
        if plx_ok:
            e = get_body_barycentric("earth", t_obs, ephemeris="builtin")
            x, y, z = (float(e.x.to_value(u.au)), float(e.y.to_value(u.au)), float(e.z.to_value(u.au)))
            a, d = math.radians(ra), math.radians(dec)
            plx_ra = self.parallax_mas / 1e3 * (x * math.sin(a) - y * math.cos(a))
            plx_dec = self.parallax_mas / 1e3 * (x * math.cos(a) * math.sin(d) + y * math.sin(a) * math.sin(d) - z * math.cos(d))
            ra += plx_ra / 3600.0 / max(math.cos(math.radians(dec)), 1e-9)
            dec += plx_dec / 3600.0
        var_ra = (self.ra_err_mas / 1e3) ** 2 + (self.pmra_err_mas_yr * dt / 1e3) ** 2
        var_dec = (self.dec_err_mas / 1e3) ** 2 + (self.pmdec_err_mas_yr * dt / 1e3) ** 2
        if self.parallax_err_mas:
            var_ra += (self.parallax_err_mas / 1e3) ** 2 * (plx_ra / max(self.parallax_mas, 1e-9) * 1e3) ** 2 / 1e6
            var_dec += (self.parallax_err_mas / 1e3) ** 2 * (plx_dec / max(self.parallax_mas, 1e-9) * 1e3) ** 2 / 1e6
        return ExpectedPosition(
            ra_deg=ra % 360.0, dec_deg=dec, cov_arcsec2=[[var_ra, 0.0], [0.0, var_dec]], mjd=mjd, epoch_jyear=mjd_to_jyear(mjd),
            components={"reference_epoch_jyear": self.epoch_jyear, "dt_years": dt,
                        "proper_motion_shift_arcsec": [pmra * dt / 1e3, pmdec * dt / 1e3],
                        "parallax_shift_arcsec": [plx_ra, plx_dec], "radial_velocity": "assumed 0 (not available)"})


def _f(x: str | None) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def gaia_cone_with_fallback(service: CachedService, ra: float, dec: float, radius_deg: float, g_max: float):
    """Gaia DR3 cone: IRSA first, then the ESA Gaia archive. The attempts are recorded on the returned query."""
    from ..context.gaia import GAIA_ENDPOINTS, GaiaCatalogue
    attempts = []
    q = None
    for ep in GAIA_ENDPOINTS:
        q = GaiaCatalogue(service, ep).cone(ra, dec, radius_deg, g_max)
        attempts.append({"service": q.service, "status": q.status, "reason": q.reason})
        if q.status == "COMPLETED":
            break
    q.attempts = attempts  # type: ignore[attr-defined]
    return q


def _gaia_query(spec: TargetSpec, service: CachedService):
    """Cone query with the selection rule pushed to the server, so large fields stay cheap. IRSA first, ESA as fallback."""
    from ..context.gaia import COLUMNS, GAIA_ENDPOINTS, CatalogueQuery, parse_stars
    where = [f"1 = CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {spec.seed_ra_deg:.8f}, {spec.seed_dec_deg:.8f}, {spec.search_radius_deg:.6f}))",
             f"phot_g_mean_mag < {spec.max_g_mag:.2f}"]
    if spec.min_g_mag is not None:
        where.append(f"phot_g_mean_mag >= {spec.min_g_mag:.2f}")
    if spec.min_parallax_mas is not None:
        where.append(f"parallax >= {spec.min_parallax_mas:.3f}")
    if spec.max_parallax_mas is not None:
        where.append(f"parallax <= {spec.max_parallax_mas:.3f}")

    def validate(raw: bytes):
        text = raw.decode("utf-8", errors="replace")
        if text.lstrip().startswith("<"):
            return "TAP returned an error document"
        try:
            parse_stars(text)
        except ValueError as exc:
            return str(exc)
        return None

    region = [spec.seed_ra_deg, spec.seed_dec_deg, spec.search_radius_deg]
    attempts, q = [], None
    for label, url, table in GAIA_ENDPOINTS:
        body = form({"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv",
                     "QUERY": f"SELECT {', '.join(COLUMNS)} FROM {table} WHERE " + " AND ".join(where)})
        ans = service.fetch(url, body, validate=validate)
        attempts.append({"service": label, "status": ans.status, "reason": ans.reason})
        if ans.status == "COMPLETED":
            stars = parse_stars(ans.body.decode("utf-8", errors="replace"))
            q = CatalogueQuery(label, "COMPLETED", "", ans.from_cache, len(stars), spec.max_g_mag, region, stars)
            break
        q = CatalogueQuery(label, ans.status, ans.reason, False, 0, spec.max_g_mag, region)
    q.attempts = attempts  # type: ignore[attr-defined]
    return q


def _gaia_resolve(spec: TargetSpec, service: CachedService) -> ReferenceAstrometry:
    q = _gaia_query(spec, service)
    base = ReferenceAstrometry(target_id=spec.target_id, catalogue="Gaia DR3", status="SERVICE_FAILED",
                               service={"name": q.service, "status": q.status, "from_cache": q.from_cache, "n_stars": q.n_stars,
                                        "attempts": getattr(q, "attempts", [])},
                               selection_rule={"seed": [spec.seed_ra_deg, spec.seed_dec_deg], "radius_deg": spec.search_radius_deg,
                                               "min_parallax_mas": spec.min_parallax_mas, "max_parallax_mas": spec.max_parallax_mas,
                                               "min_g_mag": spec.min_g_mag, "max_g_mag": spec.max_g_mag, "prefer": spec.prefer})
    if q.status != "COMPLETED":
        base.reason = q.reason
        return base
    keep = []
    for s in q.stars:
        if spec.min_parallax_mas is not None and not (s.parallax is not None and s.parallax >= spec.min_parallax_mas):
            continue
        if spec.max_parallax_mas is not None and not (s.parallax is not None and s.parallax <= spec.max_parallax_mas):
            continue
        if spec.min_g_mag is not None and not (s.g_mag is not None and s.g_mag >= spec.min_g_mag):
            continue
        keep.append(s)
    if not keep:
        base.status, base.reason = "UNRESOLVED", "no Gaia DR3 source satisfies the selection rule"
        return base

    def seed_distance(s) -> float:
        # Propagate the Gaia position back to the seed epoch before comparing with the seed.
        dt = spec.seed_epoch_jyear - s.ref_epoch
        dec = s.dec + (s.pmdec or 0.0) * dt / 3.6e6
        ra = s.ra + (s.pmra or 0.0) * dt / 3.6e6 / max(math.cos(math.radians(s.dec)), 1e-6)
        return float(angular_separation_deg(ra, dec, spec.seed_ra_deg, spec.seed_dec_deg))

    if spec.prefer == "nearest_seed":
        keep.sort(key=seed_distance)
    else:
        keep.sort(key=lambda s: (s.g_mag if s.g_mag is not None else 99.0))
    s = keep[0]
    alt = [{"source_id": a.source_id, "g_mag": a.g_mag, "parallax_mas": a.parallax,
            "separation_arcsec": float(angular_separation_deg(a.ra, a.dec, s.ra, s.dec)) * 3600.0} for a in keep[1:4]]
    base.status = "AMBIGUOUS" if alt and spec.prefer == "nearest_seed" and len(keep) > 1 else "RESOLVED"
    base.reason = "more than one source satisfies the rule; the first by the stated preference was taken" if base.status == "AMBIGUOUS" else ""
    base.source_id, base.ra_deg, base.dec_deg = f"Gaia DR3 {s.source_id}", s.ra, s.dec
    base.epoch_jyear, base.epoch_mjd = s.ref_epoch, jyear_to_mjd(s.ref_epoch)
    base.pmra_mas_yr, base.pmdec_mas_yr = s.pmra, s.pmdec
    base.pmra_err_mas_yr, base.pmdec_err_mas_yr = s.pmra_error or 0.0, s.pmdec_error or 0.0
    base.ra_err_mas, base.dec_err_mas = s.ra_error_mas, s.dec_error_mas
    base.parallax_mas, base.parallax_err_mas = s.parallax, s.parallax_error
    base.magnitudes = {"G": s.g_mag}
    base.pm_source = "Gaia DR3 catalogue" if s.pmra is not None else "unavailable"
    base.alternatives = alt
    return base


CATWISE_COLUMNS = ("source_name", "ra_pm", "dec_pm", "sigra_pm", "sigdec_pm", "pmra", "pmdec", "sigpmra", "sigpmdec",
                   "w1mpro_pm", "w1sigmpro_pm", "w2mpro_pm", "w2sigmpro_pm", "pmcode", "par_pm", "par_pmsig")


def parse_catwise(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or any(c not in reader.fieldnames for c in CATWISE_COLUMNS):
        raise ValueError("CatWISE response lacks expected columns")
    return list(reader)


def _catwise_resolve(spec: TargetSpec, service: CachedService) -> ReferenceAstrometry:
    pm_floor = (spec.min_pm_mas_yr or 0.0) / 1e3
    adql = (f"SELECT {', '.join(CATWISE_COLUMNS)} FROM catwise_2020 WHERE 1 = CONTAINS(POINT('ICRS', ra_pm, dec_pm), "
            f"CIRCLE('ICRS', {spec.seed_ra_deg:.8f}, {spec.seed_dec_deg:.8f}, {spec.search_radius_deg:.6f})) "
            f"AND (pmra*pmra + pmdec*pmdec) > {pm_floor ** 2:.6f}")
    body = form({"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": adql})

    def validate(raw: bytes):
        text = raw.decode("utf-8", errors="replace")
        if text.lstrip().startswith("<"):
            return "TAP returned an error document"
        try:
            parse_catwise(text)
        except ValueError as exc:
            return str(exc)
        return None

    ans = service.fetch(TAP_SYNC, body, validate=validate)
    base = ReferenceAstrometry(target_id=spec.target_id, catalogue="CatWISE2020", status="SERVICE_FAILED",
                               service={"name": "catwise_2020@IRSA", "status": ans.status, "from_cache": ans.from_cache},
                               selection_rule={"seed": [spec.seed_ra_deg, spec.seed_dec_deg], "radius_deg": spec.search_radius_deg,
                                               "min_pm_mas_yr": spec.min_pm_mas_yr, "max_w2_mag": spec.max_w2_mag})
    if ans.status != "COMPLETED":
        base.reason = ans.reason
        return base
    rows = parse_catwise(ans.body.decode("utf-8", errors="replace"))
    cands = []
    for r in rows:
        w2 = _f(r["w2mpro_pm"])
        if spec.max_w2_mag is not None and not (w2 is not None and w2 <= spec.max_w2_mag):
            continue
        if _f(r["ra_pm"]) is None or _f(r["pmra"]) is None:
            continue
        cands.append(r)
    if not cands:
        base.status, base.reason = "UNRESOLVED", "no CatWISE2020 source satisfies the selection rule"
        return base
    # CatWISE can list one fast mover as several entries. Take the entry with the smallest proper-motion error and
    # report the rest as alternatives (their agreement is a consistency check, their existence is a caveat).
    cands.sort(key=lambda r: math.hypot(_f(r["sigpmra"]) or 9.0, _f(r["sigpmdec"]) or 9.0))
    r = cands[0]
    base.status = "AMBIGUOUS" if len(cands) > 1 else "RESOLVED"
    base.reason = (f"{len(cands)} CatWISE2020 entries satisfy the rule (a fast mover is often split); the one with the smallest "
                   "proper-motion error was taken; the others are listed") if len(cands) > 1 else ""
    base.source_id = f"CatWISE2020 {r['source_name']}"
    base.ra_deg, base.dec_deg = _f(r["ra_pm"]), _f(r["dec_pm"])
    base.epoch_mjd, base.epoch_jyear = CATWISE_REFERENCE_MJD, mjd_to_jyear(CATWISE_REFERENCE_MJD)
    base.pmra_mas_yr, base.pmdec_mas_yr = _f(r["pmra"]) * 1e3, _f(r["pmdec"]) * 1e3  # arcsec/yr -> mas/yr
    base.pmra_err_mas_yr, base.pmdec_err_mas_yr = (_f(r["sigpmra"]) or 0.0) * 1e3, (_f(r["sigpmdec"]) or 0.0) * 1e3
    base.ra_err_mas, base.dec_err_mas = (_f(r["sigra_pm"]) or 0.0) * 1e3, (_f(r["sigdec_pm"]) or 0.0) * 1e3
    par = _f(r["par_pm"])
    if par is not None and par > 0:
        base.parallax_mas, base.parallax_err_mas = par * 1e3 if par < 5 else par, (_f(r["par_pmsig"]) or None)
    base.magnitudes = {"W1_vega": _f(r["w1mpro_pm"]), "W1_err": _f(r["w1sigmpro_pm"]), "W2_vega": _f(r["w2mpro_pm"]),
                       "W2_err": _f(r["w2sigmpro_pm"])}
    base.pm_source = "CatWISE2020 catalogue (arcsec/yr converted to mas/yr)"
    base.alternatives = [{"source_id": c["source_name"], "ra": _f(c["ra_pm"]), "dec": _f(c["dec_pm"]), "pmra_arcsec_yr": _f(c["pmra"]),
                          "pmdec_arcsec_yr": _f(c["pmdec"]), "W2_vega": _f(c["w2mpro_pm"])} for c in cands[1:6]]
    return base


def resolve(spec: TargetSpec, service: CachedService, *, gaia_service: CachedService | None = None) -> ReferenceAstrometry:
    """Resolve a target with its catalogue rule. Never falls back silently to the seed position."""
    if spec.target_id.startswith("COORD-"):
        out = _gaia_resolve(spec, gaia_service or service)
        if out.status == "UNRESOLVED":  # a bare coordinate need not be a catalogued star
            out.status = "UNRESOLVED"
            out.reason = "no Gaia DR3 source within the search radius; the investigation proceeds from the coordinate alone"
        return out
    ref = _catwise_resolve(spec, service) if spec.catalogue == "catwise2020" else _gaia_resolve(spec, gaia_service or service)
    if ref.resolved and not ref.has_proper_motion and spec.fallback_pm_mas_yr is not None:
        ref.pmra_mas_yr, ref.pmdec_mas_yr = spec.fallback_pm_mas_yr
        ref.pm_source = spec.fallback_pm_source
    return ref


def seed_position_epoch_check(ref: ReferenceAstrometry, ra_deg: float, dec_deg: float) -> dict:
    """What epoch does a stated reference position correspond to, given the catalogue proper motion?

    Solves the linear model ra(t) = ra_ref + pmra (t - t_ref) / cos(dec) for t using the larger proper-motion axis.
    Reported as a consistency check on an externally supplied coordinate (for example NASA's WISE 0855 position);
    it is an inference, never a stated fact about that coordinate.
    """
    if not ref.resolved or not ref.has_proper_motion:
        return {"status": "NOT_ASSESSABLE", "reason": "no resolved proper-motion reference"}
    cd = max(math.cos(math.radians(ref.dec_deg)), 1e-9)
    d_ra = (ra_deg - ref.ra_deg) * 3.6e6 * cd
    d_dec = (dec_deg - ref.dec_deg) * 3.6e6
    use_ra = abs(ref.pmra_mas_yr) >= abs(ref.pmdec_mas_yr)
    dt = d_ra / ref.pmra_mas_yr if use_ra else d_dec / ref.pmdec_mas_yr
    other = (d_dec - ref.pmdec_mas_yr * dt) if use_ra else (d_ra - ref.pmra_mas_yr * dt)
    return {"status": "COMPUTED", "implied_epoch_jyear": ref.epoch_jyear + dt, "residual_other_axis_arcsec": other / 1e3,
            "note": "inferred from the catalogue proper motion; the supplied coordinate's epoch is not stated by its source",
            "distance_from_reference_arcsec": float(angular_separation_deg(ra_deg, dec_deg, ref.ra_deg, ref.dec_deg)) * 3600.0,
            "consistent_with_linear_motion": bool(abs(other) / 1e3 < 1.0)}
