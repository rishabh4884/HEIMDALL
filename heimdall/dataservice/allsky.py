"""Partitioned, resumable all-sky metadata inventory with explicit completeness accounting.

The sky (or a declared sub-domain) is tiled into declination bands split into near-equal-area RA cells; the polar bands are single caps
centred on the pole. Each tile is searched with one cone that circumscribes it (RA wrap and poles are handled by the cone geometry), as a
durable INDEX job of the data service, so retries, backoff, circuit breakers, deadlines and recovery are the same as everywhere else.

Partition states:

* ``UNSEARCHED`` -- planned, no attempt yet;  ``QUEUED`` -- an INDEX job owns it;
* ``SEARCHED_HAS_OBS`` / ``SEARCHED_EMPTY`` -- a successful, *untruncated* answer (``SEARCHED_EMPTY`` only from a successful search);
* ``SEARCHED_UNVERIFIED`` -- answered, but truncation could not be checked (the TAP fallback route sends no MAXREC); not certified;
* ``SUBDIVIDED`` -- the answer reached MAXREC (truncated): replaced by four children; never counted as searched itself;
* ``FAILED`` -- the job ended without an answer (deadline, invalid data, software error); retried by later steps;
* ``NEEDS_REFRESH`` -- searched longer ago than the refresh age.

Accounting keeps metadata coverage (area searched), downloaded-product coverage (partitions holding verified stored products) and
scientifically used coverage (partitions with products pinned by a frozen analysis manifest) separate. ``all_sky_complete`` is true only
when every leaf partition of a whole-sky domain is certified searched; nothing else sets it.
"""

from __future__ import annotations

import json
import math

from .inventory import now_iso
from .schemas import JobState, RegionSpec

WHOLE_SKY = (0.0, 360.0, -90.0, 90.0)
CERTIFIED = ("SEARCHED_HAS_OBS", "SEARCHED_EMPTY")


def _sep(ra1, dec1, ra2, dec2) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    h = math.sin((d2 - d1) / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin((r2 - r1) / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h))))


def tile_cone(ra_min, ra_max, dec_min, dec_max) -> tuple[float, float, float]:
    """Centre and radius of a cone containing the whole tile (plus a small margin)."""
    if dec_max >= 90.0 and ra_max - ra_min >= 360.0:
        return 0.0, 90.0, 90.0 - dec_min
    if dec_min <= -90.0 and ra_max - ra_min >= 360.0:
        return 0.0, -90.0, dec_max + 90.0
    ra_c = (ra_min + ra_max) / 2.0 % 360.0
    dec_c = (dec_min + dec_max) / 2.0
    pts = [(ra, dec) for ra in (ra_min, (ra_min + ra_max) / 2.0, ra_max) for dec in (dec_min, dec_max)]
    pts += [(ra_min, dec_c), (ra_max, dec_c)]
    return ra_c, dec_c, max(_sep(ra_c, dec_c, r % 360.0, d) for r, d in pts) * 1.001 + 1e-6


def tile_area(ra_min, ra_max, dec_min, dec_max) -> float:
    return math.degrees(math.radians(ra_max - ra_min)) * math.degrees(math.sin(math.radians(dec_max)) - math.sin(math.radians(dec_min)))


def plan_tiles(tile_deg: float, domain=WHOLE_SKY) -> list[dict]:
    ra0, ra1, d0, d1 = domain
    out = []
    nb = max(1, math.ceil((d1 - d0) / tile_deg))
    for b in range(nb):
        lo, hi = d0 + b * (d1 - d0) / nb, d0 + (b + 1) * (d1 - d0) / nb
        if (hi >= 90.0 or lo <= -90.0) and ra1 - ra0 >= 360.0:
            cells = 1  # polar cap
        else:
            width = (ra1 - ra0) * max(math.cos(math.radians(max(abs(lo), abs(hi)))), math.cos(math.radians(min(abs(lo), abs(hi))))) \
                if not (lo < 0 < hi) else (ra1 - ra0)
            cells = max(1, math.ceil(width / tile_deg))
        for c in range(cells):
            a, z = ra0 + c * (ra1 - ra0) / cells, ra0 + (c + 1) * (ra1 - ra0) / cells
            out.append({"ra_min": a, "ra_max": z, "dec_min": lo, "dec_max": hi})
    return out


class AllSkyIndex:
    def __init__(self, service) -> None:
        self.svc = service
        self.db = service.db

    @staticmethod
    def domain_id(tile_deg: float, domain=WHOLE_SKY) -> str:
        return f"T{tile_deg:g}:{domain[0]:g},{domain[1]:g},{domain[2]:g},{domain[3]:g}"

    # -- planning ----------------------------------------------------------------------------------------------
    def plan(self, tile_deg: float = 2.0, domain=WHOLE_SKY) -> dict:
        """Idempotent: creates the level-0 partitions of a domain once; re-planning keeps existing states."""
        if not (0.25 <= tile_deg <= 5.0):
            raise ValueError("tile_deg must be within [0.25, 5] degrees (one cone per tile; cones are limited to 5 degrees)")
        ra0, ra1, d0, d1 = domain
        if not (0 <= ra0 < ra1 <= 360 and -90 <= d0 < d1 <= 90):
            raise ValueError("domain must be ra 0..360, dec -90..90 with min < max")
        dom = self.domain_id(tile_deg, domain)
        now = now_iso(self.svc.clock())
        tiles = plan_tiles(tile_deg, domain)
        with self.db.transaction() as db:
            for i, t in enumerate(tiles):
                cra, cdec, crad = tile_cone(t["ra_min"], t["ra_max"], t["dec_min"], t["dec_max"])
                if crad > 5.0 + 1e-9:
                    raise ValueError("tile too large for one cone; use a smaller tile_deg")
                db.conn.execute("INSERT OR IGNORE INTO allsky_partitions(partition_id,domain,level,parent,ra_min,ra_max,dec_min,dec_max,cone_ra,cone_dec,"
                                "cone_radius,area_deg2,state,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (f"{dom}/0:{i}", dom, 0, None, t["ra_min"], t["ra_max"], t["dec_min"], t["dec_max"], cra, cdec, crad,
                                 tile_area(t["ra_min"], t["ra_max"], t["dec_min"], t["dec_max"]), "UNSEARCHED", now))
        return {"domain": dom, "partitions": len(tiles), "area_deg2": sum(tile_area(**t) for t in tiles)}

    # -- execution ---------------------------------------------------------------------------------------------
    def _region(self, p) -> RegionSpec:
        return RegionSpec(round(p["cone_ra"] % 360.0, 8), round(p["cone_dec"], 8), round(p["cone_radius"], 8))

    def reconcile(self, dom: str) -> dict:
        """Fold the outcome of every partition's INDEX job into the partition state (resumable at any time)."""
        changed: dict[str, int] = {}
        now = now_iso(self.svc.clock())
        for p in self.db.query("SELECT * FROM allsky_partitions WHERE domain=? AND state='QUEUED'", (dom,)):
            job = self.svc.jobs.get(p["job_id"]) if p["job_id"] else None
            if job is None:
                new, extra = "UNSEARCHED", {}
            elif job["state"] is JobState.COMPLETE:
                r = job["result"] or {}
                if r.get("truncated") is True:
                    self._subdivide(p, now)
                    new, extra = "SUBDIVIDED", {"n_obs": r.get("n_observations"), "truncated": 1}
                else:
                    new = "SEARCHED_HAS_OBS" if r.get("truncated") is False else "SEARCHED_UNVERIFIED"
                    extra = {"n_obs": r.get("n_observations"), "sync_id": r.get("sync_id"), "truncated": 0 if r.get("truncated") is False else None,
                             "searched_at": job["updated_at"]}
            elif job["state"] is JobState.NO_COVERAGE:
                r = job["result"] or {}
                new = "SEARCHED_EMPTY" if r.get("truncated") is not True else "SEARCHED_UNVERIFIED"
                extra = {"n_obs": 0, "searched_at": job["updated_at"], "truncated": 0}
            elif job["state"] in (JobState.UNAVAILABLE_AFTER_DEADLINE, JobState.INVALID_SOURCE_DATA, JobState.FAILED, JobState.CANCELLED):
                new, extra = "FAILED", {"last_error": json.dumps(job["last_error"] or {"reason": job["reason"]})}
            else:
                continue  # still running or waiting for a retry
            sets = ", ".join(f"{k}=?" for k in ["state", "updated_at", *extra])
            self.db.execute(f"UPDATE allsky_partitions SET {sets} WHERE partition_id=?", (new, now, *extra.values(), p["partition_id"]))
            changed[new] = changed.get(new, 0) + 1
        return changed

    def _subdivide(self, p, now: str) -> None:
        if p["level"] >= 6:
            return  # bounded recursion; a still-truncated level-6 tile stays visible as SUBDIVIDED with no children (recorded limit)
        ra_mid, dec_mid = (p["ra_min"] + p["ra_max"]) / 2.0, (p["dec_min"] + p["dec_max"]) / 2.0
        with self.db.transaction() as db:
            for k, (a, z, lo, hi) in enumerate([(p["ra_min"], ra_mid, p["dec_min"], dec_mid), (ra_mid, p["ra_max"], p["dec_min"], dec_mid),
                                                (p["ra_min"], ra_mid, dec_mid, p["dec_max"]), (ra_mid, p["ra_max"], dec_mid, p["dec_max"])]):
                cra, cdec, crad = tile_cone(a, z, lo, hi)
                db.conn.execute("INSERT OR IGNORE INTO allsky_partitions(partition_id,domain,level,parent,ra_min,ra_max,dec_min,dec_max,cone_ra,cone_dec,"
                                "cone_radius,area_deg2,state,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (f"{p['partition_id']}.{k}", p["domain"], p["level"] + 1, p["partition_id"], a, z, lo, hi, cra, cdec, crad,
                                 tile_area(a, z, lo, hi), "UNSEARCHED", now))

    def step(self, dom: str, *, max_new: int = 4, max_in_flight: int = 8, refresh_age_s: float | None = None) -> dict:
        """Reconcile finished work, mark stale partitions for refresh, then hand at most ``max_new`` partitions to durable INDEX jobs while
        keeping at most ``max_in_flight`` outstanding (bounded queue; the worker does the network work)."""
        changed = self.reconcile(dom)
        if refresh_age_s:
            from datetime import timedelta
            cutoff = now_iso(self.svc.clock() - timedelta(seconds=refresh_age_s))
            self.db.execute("UPDATE allsky_partitions SET state='NEEDS_REFRESH' WHERE domain=? AND state IN ('SEARCHED_HAS_OBS','SEARCHED_EMPTY') "
                            "AND searched_at < ?", (dom, cutoff))
        in_flight = self.db.one("SELECT COUNT(*) FROM allsky_partitions WHERE domain=? AND state='QUEUED'", (dom,))[0]
        budget = max(0, min(max_new, max_in_flight - in_flight))
        todo = self.db.query("SELECT * FROM allsky_partitions WHERE domain=? AND state IN ('UNSEARCHED','FAILED','NEEDS_REFRESH') "
                             "ORDER BY CASE state WHEN 'UNSEARCHED' THEN 0 WHEN 'NEEDS_REFRESH' THEN 1 ELSE 2 END, level, partition_id LIMIT ?", (dom, budget))
        submitted = []
        for p in todo:
            region = self._region(p)
            st = self.svc.ensure_index(region)  # durable, idempotent INDEX job (or immediate answer if already indexed)
            job = st.get("job")
            now = now_iso(self.svc.clock())
            if job is None:  # already indexed by an existing untruncated sync
                sync = self.svc.inventory.covering_sync(region, self.svc.cfg.scope)
                self.db.execute("UPDATE allsky_partitions SET state=?, sync_id=?, n_obs=?, truncated=0, searched_at=?, updated_at=? WHERE partition_id=?",
                                ("SEARCHED_HAS_OBS" if sync and sync["n_obs"] else "SEARCHED_EMPTY", sync["sync_id"] if sync else None,
                                 sync["n_obs"] if sync else 0, sync["completed_at"] if sync else now, now, p["partition_id"]))
            else:
                self.db.execute("UPDATE allsky_partitions SET state='QUEUED', job_id=?, attempts=attempts+1, updated_at=? WHERE partition_id=?",
                                (job["job_id"], now, p["partition_id"]))
                submitted.append(p["partition_id"])
        return {"domain": dom, "reconciled": changed, "submitted": submitted, "in_flight": in_flight + len(submitted)}

    # -- accounting --------------------------------------------------------------------------------------------
    def accounting(self, dom: str, *, offset: int = 0, limit: int = 200) -> dict:
        rows = self.db.query("SELECT * FROM allsky_partitions WHERE domain=? ORDER BY level, partition_id", (dom,))
        if not rows:
            return {"domain": dom, "planned": False, "all_sky_complete": False}
        leaves = [r for r in rows if r["state"] != "SUBDIVIDED"]
        by_state: dict[str, int] = {}
        for r in rows:
            by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        total = sum(r["area_deg2"] for r in rows if r["level"] == 0)
        cert = sum(r["area_deg2"] for r in leaves if r["state"] in CERTIFIED)
        # downloaded-product and scientifically-used coverage, by stored observation centres falling in leaf tiles
        stored = self.db.query("SELECT DISTINCT o.s_ra, o.s_dec, k.sha256 FROM product_keys k JOIN observations o ON o.obs_id=k.obs_id AND o.release=k.release "
                               "WHERE o.s_ra IS NOT NULL")
        pinned = {r[0] for r in self.db.query("SELECT DISTINCT sha256 FROM pins")}

        def leaf_of(ra, dec):
            for r in leaves:
                if r["dec_min"] <= dec <= r["dec_max"] and (r["ra_min"] <= ra < r["ra_max"] or (r["ra_max"] >= 360.0 and ra >= r["ra_min"])):
                    return r["partition_id"]
            return None
        with_products = {leaf_of(r["s_ra"], r["s_dec"]) for r in stored} - {None}
        used = {leaf_of(r["s_ra"], r["s_dec"]) for r in stored if r["sha256"] in pinned} - {None}
        whole = dom.endswith(":0,360,-90,90")
        complete = whole and bool(leaves) and all(r["state"] in CERTIFIED for r in leaves)
        page = [{k: r[k] for k in ("partition_id", "level", "parent", "ra_min", "ra_max", "dec_min", "dec_max", "cone_ra", "cone_dec", "cone_radius",
                                   "area_deg2", "state", "job_id", "n_obs", "truncated", "attempts", "searched_at")} | {"last_error": json.loads(r["last_error"]) if r["last_error"] else None}
                for r in rows[offset:offset + max(1, min(limit, 2000))]]
        return {"domain": dom, "planned": True, "whole_sky_domain": whole, "partitions": len(rows), "leaf_partitions": len(leaves), "by_state": by_state,
                "area_deg2": {"domain": total, "certified_searched": cert, "fraction_certified": cert / total if total else 0.0},
                "observations_indexed_in_certified_partitions": sum(r["n_obs"] or 0 for r in leaves if r["state"] in CERTIFIED),
                "metadata_coverage_partitions": sum(1 for r in leaves if r["state"] in CERTIFIED),
                "downloaded_product_partitions": len(with_products), "scientifically_used_partitions": len(used),
                "all_sky_complete": complete,
                "completeness_rule": "true only when every leaf partition of a whole-sky domain has a successful, untruncated (MAXREC-checked) answer",
                "offset": offset, "limit": limit, "page": page}


# ---------------------------------------------------------------------------------------------------- mirror planning

def measured_sizes(db) -> dict:
    """Mean stored size per product kind, from verified objects only (nothing assumed). Full frames are keyed ``|full``."""
    out = {}
    for label, where in (("full_frame", "k.kind='science' AND k.key LIKE '%|full'"), ("cutout", "k.kind='science' AND k.key LIKE '%|cut:%'"),
                         ("calibration", "k.kind='calibration'")):
        r = db.one(f"SELECT COUNT(*) n, AVG(p.size) s FROM product_keys k JOIN products p ON p.sha256=k.sha256 WHERE {where}")
        out[label] = {"n_samples": r["n"], "mean_bytes": r["s"]} if r["n"] else {"n_samples": 0, "mean_bytes": None}
    return out


def mirror_plan(service, dom: str, *, max_bytes: int, reserve_bytes: int = 1 << 30, max_partitions: int | None = None) -> dict:
    """Full-frame mirror plan for the certified partitions of a domain: products to fetch (deduplicated across overlapping cones), bytes
    estimated from *measured* full-frame sizes, disk/quota feasibility, and the protected (pinned) evidence that eviction may never touch.
    Without a measured full-frame size the estimate is ``UNMEASURED`` and execution is refused (fetch one sample first)."""
    import shutil
    rows = service.db.query("SELECT * FROM allsky_partitions WHERE domain=? AND state IN ('SEARCHED_HAS_OBS') ORDER BY level, partition_id", (dom,))
    rows = rows[:max_partitions] if max_partitions else rows
    wanted: set[tuple[str, str]] = set()
    dets: set[int] = set()
    for r in rows:
        s = service.db.one("SELECT members FROM syncs WHERE sync_id=?", (r["sync_id"],)) if r["sync_id"] else None
        for oid, rel in json.loads(s["members"]) if s else []:
            wanted.add((oid, rel))
            o = service.db.one("SELECT detector FROM observations WHERE obs_id=? AND release=?", (oid, rel))
            if o and o["detector"]:
                dets.add(int(o["detector"]))
    have = {(r["obs_id"], r["release"]) for r in service.db.query("SELECT obs_id, release FROM product_keys WHERE key LIKE '%|full'")}
    missing = sorted(wanted - have)
    calib_missing = [d for d in sorted(dets) if service.store.get(f"calib:epsf|D{d}") is None]
    sizes = measured_sizes(service.db)
    ff, cal = sizes["full_frame"]["mean_bytes"], sizes["calibration"]["mean_bytes"]
    est = None if ff is None else int(len(missing) * ff + len(calib_missing) * (cal or 0))
    usage = service.store.usage()
    free = shutil.disk_usage(service.store.root).free
    limit = min([x for x in (max_bytes, free - reserve_bytes, (service.cfg.quota_bytes - usage["bytes"]) if service.cfg.quota_bytes else None) if x is not None])
    pinned = service.db.one("SELECT COUNT(DISTINCT p.sha256) n, COALESCE(SUM(p.size),0) b FROM products p JOIN pins x ON x.sha256=p.sha256")
    return {"domain": dom, "mode": "full_frame", "certified_partitions": len(rows), "products_indexed": len(wanted), "products_already_local": len(wanted) - len(missing),
            "products_to_fetch": len(missing), "calibrations_to_fetch": [f"epsf D{d}" for d in calib_missing],
            "measured_sizes": sizes, "estimated_bytes": est, "estimate_state": "MEASURED" if est is not None else "UNMEASURED",
            "budget": {"max_bytes": max_bytes, "disk_free_bytes": free, "reserve_bytes": reserve_bytes, "quota_bytes": service.cfg.quota_bytes,
                       "effective_limit_bytes": limit},
            "feasible": est is not None and est <= limit,
            "protected_evidence": {"pinned_objects": pinned["n"], "pinned_bytes": pinned["b"], "rule": "pinned (manifest) objects are never evicted"},
            "execution": ("refused: no measured full-frame size; fetch one sample first (data prefetch with --cutout-size omitted, --max-observations 1)"
                          if est is None else "prefetch jobs per partition (resumable, SHA-256 verified, pinned on use) within the effective limit"),
            "note": "point-centred cutouts are not a mirror: they cover requested positions, not partitions"}
