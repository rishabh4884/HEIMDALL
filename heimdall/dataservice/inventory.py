"""Persistent inventory of discovered SPHEREx observations, answering region queries without a live archive request.

A region question is answered from the inventory only when a completed *sync* (a successful archive discovery) covers it: same scope,
a cone that contains the requested cone, detectors/time window that include the requested ones. Without such a sync the answer is
``NOT_INDEXED`` -- never "no observations". Members of a covering sync are narrowed by footprint polygon when the archive supplied one.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from ..data_access.irsa_spherex import _release_rank
from .db import ServiceDB
from .schemas import CURRENT_COLLECTIONS, SCOPE_CURRENT, SCOPE_SYNTHETIC, RegionSpec


def now_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(UTC)).isoformat(timespec="seconds")


def sep_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r2 - r1)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def parse_polygon(text: str | None) -> list[tuple[float, float]]:
    """'POLYGON ICRS ra dec ra dec ...' -> vertices; [] if absent or unparseable (then membership is decided by the sync only)."""
    if not text:
        return []
    parts = text.replace(",", " ").split()
    try:
        nums = [float(p) for p in parts[2:]] if parts and parts[0].upper() == "POLYGON" and parts[1].isalpha() else [float(p) for p in parts[1:]]
    except ValueError:
        return []
    return list(zip(nums[0::2], nums[1::2])) if len(nums) >= 6 and len(nums) % 2 == 0 else []


def footprint_intersects_cone(poly: list[tuple[float, float]], ra: float, dec: float, radius: float) -> bool:
    """Planar (tangent-plane) test: the cone centre lies inside the polygon, or a vertex/edge comes within the radius."""
    if not poly:
        return True
    cosd = math.cos(math.radians(dec))

    def xy(r, d):
        dr = (r - ra + 180.0) % 360.0 - 180.0
        return dr * cosd, d - dec

    pts = [xy(r, d) for r, d in poly]
    inside = False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        if (y1 > 0) != (y2 > 0) and x1 + (0 - y1) * (x2 - x1) / (y2 - y1) > 0:
            inside = not inside
    if inside:
        return True
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        dx, dy = x2 - x1, y2 - y1
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / (dx * dx + dy * dy)))
        if math.hypot(x1 + t * dx, y1 + t * dy) <= radius:
            return True
    return False


@dataclass
class ObsRecord:
    obs_id: str
    release: str
    collection: str
    detector: int
    access_reference: str
    discovered_at: str
    t_min: float | None = None
    t_max: float | None = None
    wl_min_um: float | None = None
    wl_max_um: float | None = None
    footprint: str | None = None
    s_ra: float | None = None
    s_dec: float | None = None
    route: str | None = None
    archive_checksum: str | None = None
    product_version: str | None = None
    source_version: str | None = None
    origin: str = "LIVE"
    legacy: bool = False
    synthetic: bool = False
    superseded_by: str | None = None
    last_seen_at: str | None = None


def _row(r) -> ObsRecord:
    d = dict(r)
    d.pop("extra", None)
    d["legacy"], d["synthetic"] = bool(d["legacy"]), bool(d["synthetic"])
    return ObsRecord(**d)


class Inventory:
    def __init__(self, db: ServiceDB) -> None:
        self.db = db

    # -- writes -------------------------------------------------------------------------------
    def register(self, records: list[ObsRecord]) -> None:
        """Insert/refresh observations and resolve supersession: for one obs_id the newest release wins, older ones are marked."""
        with self.db.transaction() as db:
            for r in records:
                db.conn.execute(
                    "INSERT INTO observations(obs_id,release,collection,detector,footprint,s_ra,s_dec,t_min,t_max,wl_min_um,wl_max_um,route,access_reference,"
                    "discovered_at,last_seen_at,archive_checksum,product_version,source_version,origin,legacy,synthetic) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(obs_id,release) DO UPDATE SET last_seen_at=excluded.last_seen_at, "
                    "footprint=COALESCE(excluded.footprint,footprint), route=excluded.route, access_reference=excluded.access_reference, "
                    "archive_checksum=COALESCE(excluded.archive_checksum,archive_checksum), product_version=excluded.product_version, "
                    "source_version=excluded.source_version",
                    (r.obs_id, r.release, r.collection, r.detector, r.footprint, r.s_ra, r.s_dec, r.t_min, r.t_max, r.wl_min_um, r.wl_max_um, r.route,
                     r.access_reference, r.discovered_at, r.last_seen_at or r.discovered_at, r.archive_checksum, r.product_version, r.source_version,
                     r.origin, int(r.legacy), int(r.synthetic)))
            for oid in {r.obs_id for r in records}:
                rows = db.conn.execute("SELECT release FROM observations WHERE obs_id=?", (oid,)).fetchall()
                if len(rows) > 1:
                    best = max((x["release"] for x in rows), key=_release_rank)
                    db.conn.execute("UPDATE observations SET superseded_by=CASE WHEN release=? THEN NULL ELSE ? END WHERE obs_id=?", (best, best, oid))

    def record_sync(self, region: RegionSpec, scope: str, members: list[tuple[str, str]], *, completed_at: str, route: str | None, from_cache: bool,
                    truncated: bool = False) -> int:
        newest = None
        for oid, rel in members:
            r = self.db.one("SELECT t_max FROM observations WHERE obs_id=? AND release=?", (oid, rel))
            if r and r["t_max"] is not None:
                newest = max(newest or -math.inf, r["t_max"])
        cur = self.db.execute(
            "INSERT INTO syncs(ra,dec,radius,detectors,mjd_start,mjd_end,collections,scope,completed_at,route,from_cache,n_obs,newest_t_max,members,truncated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (region.ra_deg, region.dec_deg, region.radius_deg, json.dumps(sorted(region.detectors)) if region.detectors else None, region.mjd_start,
             region.mjd_end, json.dumps(sorted(region.collections or CURRENT_COLLECTIONS)), scope, completed_at, route, int(from_cache), len(members),
             newest, json.dumps(sorted(members)), int(bool(truncated))))
        return int(cur.lastrowid)

    # -- reads (never touch the network) ---------------------------------------------------------
    def covering_sync(self, region: RegionSpec, scope: str = SCOPE_CURRENT):
        """The most recent completed sync whose cone, filters and collections include ``region``; None if the region is not indexed."""
        want_cols = set(region.collections or CURRENT_COLLECTIONS)
        # a truncated answer never certifies coverage; the declination prefilter keeps this cheap with many partition syncs
        for s in self.db.query("SELECT * FROM syncs WHERE scope=? AND truncated=0 AND dec - radius <= ? AND dec + radius >= ? "
                               "ORDER BY completed_at DESC, sync_id DESC", (scope, region.dec_deg + 1e-9, region.dec_deg - 1e-9)):
            if sep_deg(region.ra_deg, region.dec_deg, s["ra"], s["dec"]) + region.radius_deg > s["radius"] + 1e-9:
                continue
            sd = json.loads(s["detectors"]) if s["detectors"] else None
            if sd is not None and (region.detectors is None or not set(region.detectors) <= set(sd)):
                continue
            if s["mjd_start"] is not None and (region.mjd_start is None or region.mjd_start < s["mjd_start"]):
                continue
            if s["mjd_end"] is not None and (region.mjd_end is None or region.mjd_end > s["mjd_end"]):
                continue
            if not want_cols <= set(json.loads(s["collections"])):
                continue
            return s
        return None

    def members(self, region: RegionSpec, sync) -> list[ObsRecord]:
        """Non-superseded observations of the covering sync that intersect the requested cone and filters."""
        out = []
        for oid, rel in json.loads(sync["members"]):
            r = self.db.one("SELECT * FROM observations WHERE obs_id=? AND release=?", (oid, rel))
            if r is None or r["superseded_by"]:
                continue
            o = _row(r)
            if region.detectors and o.detector not in region.detectors:
                continue
            if region.mjd_start is not None and o.t_max is not None and o.t_max < region.mjd_start:
                continue
            if region.mjd_end is not None and o.t_min is not None and o.t_min > region.mjd_end:
                continue
            if not footprint_intersects_cone(parse_polygon(o.footprint), region.ra_deg, region.dec_deg, region.radius_deg):
                continue
            out.append(o)
        return sorted(out, key=lambda o: (o.t_min if o.t_min is not None else math.inf, o.obs_id))

    def get(self, obs_id: str, release: str) -> ObsRecord | None:
        r = self.db.one("SELECT * FROM observations WHERE obs_id=? AND release=?", (obs_id, release))
        return _row(r) if r else None

    def counts(self, scope: str = SCOPE_CURRENT) -> dict:
        syn = 1 if scope == SCOPE_SYNTHETIC else 0
        rows = self.db.query("SELECT release, COUNT(*) n, SUM(superseded_by IS NOT NULL) sup, MAX(t_max) newest FROM observations WHERE synthetic=? GROUP BY release", (syn,))
        return {r["release"]: {"n": r["n"], "superseded": r["sup"] or 0, "newest_t_max": r["newest"]} for r in rows}
