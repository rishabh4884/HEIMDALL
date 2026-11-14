"""Sky-map data contract (schema ``heimdall.skymap/1.0``), built from an investigation result.

Coordinates: ICRS right ascension and declination in degrees, as measured at
each observation's time (no proper-motion propagation). Uncertainties are 1-sigma
ellipses (semi-axes in arcsec, position angle east of north). Projections the
renderer may use: gnomonic (TAN) about the field centre for local views and
Mollweide for all-sky views (``heimdall.skymap.projection``).

Only data HEIMDALL measured or received is drawn. Background stars, catalogue
overlays and solar-system ephemerides are external layers that are NOT
available until an authoritative catalogue or service is configured.

Line semantics:
- association segments join a source's mean position to each member detection:
  membership under the stationary-source gate, not a trajectory;
- track polylines join time-ordered detections linked under the distant-body
  motion hypothesis: a candidate linkage, not a confirmed orbit.
"""

from __future__ import annotations

import math

import numpy as np

from ..engine.skygeom import covariance_ellipse, gnomonic
from .index import SkyIndex, level_for_fov

SKYMAP_SCHEMA = "heimdall.skymap/1.0"


def _ellipse(cov) -> dict | None:
    if cov is None:
        return None
    a, b, pa = covariance_ellipse(np.asarray(cov, dtype=float))
    return {"a_arcsec": a, "b_arcsec": b, "pa_deg": pa}


def _inside(px: np.ndarray, py: np.ndarray, qx: np.ndarray, qy: np.ndarray) -> np.ndarray:
    """Vectorised even-odd ray casting: points (qx, qy) inside polygon (px, py)."""
    inside = np.zeros(qx.shape, dtype=bool)
    n = len(px)
    for i in range(n):
        x1, y1, x2, y2 = px[i], py[i], px[(i + 1) % n], py[(i + 1) % n]
        crosses = (y1 > qy) != (y2 > qy)
        with np.errstate(divide="ignore", invalid="ignore"):
            xc = x1 + (qy - y1) * (x2 - x1) / (y2 - y1)
        inside ^= crosses & (qx < xc)
    return inside


def coverage_depth(polygons: list[dict], ra0: float, dec0: float, radius_deg: float, n: int = 40) -> dict:
    """Number of included footprints covering each point of a grid over the field (gnomonic, degrees)."""
    axis = np.linspace(-radius_deg, radius_deg, n)
    gx, gy = np.meshgrid(axis, axis)
    depth = np.zeros(gx.shape, dtype=int)
    for poly in polygons:
        px, py = gnomonic(poly["ra"], poly["dec"], ra0, dec0)
        if np.all(np.isfinite(px)):
            depth += _inside(np.asarray(px), np.asarray(py), gx, gy)
    in_field = np.hypot(gx, gy) <= radius_deg
    covered = depth[in_field]
    return {"projection": "gnomonic about the field centre", "axis_deg": axis.tolist(), "depth": depth.tolist(),
            "in_field_points": int(in_field.sum()),
            "fraction_uncovered": float(np.mean(covered == 0)) if covered.size else None,
            "max_depth": int(depth.max()) if depth.size else 0,
            "median_depth_in_field": float(np.median(covered)) if covered.size else None}


def _external_layers(context: dict) -> dict:
    """Real catalogue stars and predicted known bodies from the run's context stage; never invented."""
    gaia, sb = context.get("gaia") or {}, context.get("small_bodies") or {}
    if gaia.get("status") == "COMPLETED":
        stars = {"status": "AVAILABLE", "source": gaia.get("service"), "g_max": gaia.get("g_max"),
                 "epoch": "catalogue reference epoch (ref_epoch, J2016.0 for DR3); propagate with pmra/pmdec (mas/yr)",
                 "items": gaia.get("stars", [])}
    else:
        stars = {"status": "NOT_AVAILABLE", "items": [],
                 "reason": context.get("reason") or f"Gaia DR3 query {gaia.get('status')}: {gaia.get('reason')}; "
                           "HEIMDALL never draws invented background stars"}
    queries = sb.get("queries") or []
    if queries:
        done = [q for q in queries if q.get("status") == "COMPLETED"]
        bodies = {"status": "AVAILABLE" if len(done) == len(queries) else "PARTIAL", "source": sb.get("service"),
                  "meaning": "JPL-predicted positions of known small bodies at the times of the queried images only",
                  "queried_obs_ids": [q["obs_id"] for q in queries], "failed_obs_ids": [q["obs_id"] for q in queries if q not in done],
                  "items": [{**p, "obs_id": q["obs_id"], "mjd": q.get("mjd")} for q in done for p in q.get("predictions", [])]}
    else:
        bodies = {"status": "NOT_AVAILABLE", "items": [],
                  "reason": context.get("reason") or "no image needed a known-object query"}
    return {"star_catalogue": stars, "solar_system_ephemerides": bodies}


def build_skymap(result: dict, viewport: tuple[float, float, float] | None = None, max_items: int = 2000) -> dict:
    req = result["request"]
    ra0, dec0, radius = float(req["ra_deg"]) % 360.0, float(req["dec_deg"]), float(req["radius_deg"])
    frames = {f["obs_id"]: f for f in ((result.get("playback") or {}).get("frames") or [])}
    coverage = []
    for o in result.get("observations", []):
        meta = o.get("meta") or {}
        frame = frames.get(o["obs_id"], {})
        coverage.append({"obs_id": o["obs_id"], "detector": meta.get("detector"), "mjd": meta.get("mjd_avg"),
                         "epoch_index": frame.get("epoch_index"), "wavelength_um": frame.get("wavelength_um"),
                         "polygon": o.get("footprint"), "included": o.get("included"),
                         "exclusion_reasons": o.get("exclusion_reasons", [])})
    polys = [c["polygon"] for c in coverage if c["included"] and c["polygon"]]
    source_of = {d: s["source_id"] for s in result.get("sources", []) for d in s.get("det_ids", [])}
    detections = [{"det_id": d["det_id"], "obs_id": d["obs_id"], "source_id": source_of.get(d["det_id"]), "ra": d["ra"],
                   "dec": d["dec"], "ellipse_1sigma": _ellipse(d["cov_sky_arcsec2"]), "snr": d["snr_fit"],
                   "flux_ujy": d["flux_ujy"], "grade": d["quality_grade"], "mjd": d["mjd"], "detector": d["detector"],
                   "wavelength_um": d["wavelength_um"]} for d in result.get("detections", [])]
    det_pos = {d["det_id"]: (d["ra"], d["dec"], d["mjd"]) for d in detections}
    sources, associations = [], []
    for s in result.get("sources", []):
        var = [v for v in s.get("variability", []) if v.get("status") == "COMPLETED" and v.get("significance")]
        sources.append({"source_id": s["source_id"], "ra": s["ra"], "dec": s["dec"], "ellipse_1sigma": s.get("ellipse_1sigma"),
                        "n_obs": s["n_obs"], "n_epochs": s["n_epochs"], "ambiguous": s["ambiguous"],
                        "max_variability_sigma": max((v["significance"]["sigma_equivalent"] for v in var), default=None),
                        "stationarity_sigma": ((s.get("motion") or {}).get("gof_stationary") or {}).get("sigma_equivalent")})
        if len(s.get("det_ids", [])) >= 2:
            associations.append({"source_id": s["source_id"], "ambiguous": s["ambiguous"],
                                 "segments": [{"det_id": d, "from": [s["ra"], s["dec"]], "to": list(det_pos[d][:2])}
                                              for d in s["det_ids"] if d in det_pos]})
    inv_of = {cid: i for i in (result.get("investigations") or {}).get("investigations", []) for cid in i["candidate_ids"]}

    def investigation_fields(cid: str) -> dict:
        i = inv_of.get(cid)
        if i is None:
            return {"investigation_id": None, "interpretation_status": None, "evidence_refs": [], "provenance_refs": []}
        return {"investigation_id": i["investigation_id"], "interpretation_status": i["interpretation"]["status"],
                "evidence_refs": [e["evidence_id"] for e in i["evidence"] if cid in e["candidate_ids"]],
                "provenance_refs": sorted({f"{i['versions']['source_run_id']}#{e['ref']}" for e in i["evidence"] if cid in e["candidate_ids"]})}

    candidates = [{"candidate_id": c["candidate_id"], "kind": c["kind"], "category": c["category"], "subject_id": c["subject_id"],
                   "attention_level": c["candidate"]["attention_level"], "validity": c["candidate"]["validity"]["state"],
                   "ra": c["position"]["ra_deg"], "dec": c["position"]["dec_deg"],
                   "ellipse_1sigma": c["position"]["ellipse_1sigma"],
                   "sigma_equivalent": c["statistic"]["significance"]["sigma_equivalent"],
                   "planet_x_status": (c.get("planet_x") or {}).get("status"),
                   **investigation_fields(c["candidate_id"])} for c in result.get("candidates", [])]
    tracks = []
    for t in result.get("tracks", []):
        pts = sorted((det_pos[d][2] or math.inf, d) for d in t["det_ids"] if d in det_pos)
        tracks.append({"track_id": t["track_id"], "ambiguous": t["ambiguous"],
                       "points": [{"det_id": d, "ra": det_pos[d][0], "dec": det_pos[d][1], "mjd": det_pos[d][2]} for _, d in pts],
                       "distance_au": (t.get("parallax") or {}).get("distance_au"),
                       "meaning": "candidate linkage under the distant-body motion hypothesis; not a confirmed orbit"})
    bands = {}
    for c in coverage:
        if c["included"] and c["detector"] is not None:
            b = bands.setdefault(c["detector"], {"detector": c["detector"], "n_observations": 0, "wavelengths_um": []})
            b["n_observations"] += 1
            if c["wavelength_um"] is not None:
                b["wavelengths_um"].append(c["wavelength_um"])
    for b in bands.values():
        w = b.pop("wavelengths_um")
        b["wavelength_range_um"] = [min(w), max(w)] if w else None
    det_index = SkyIndex(detections)
    view = None
    if viewport is not None:
        view = {"centre": [viewport[0], viewport[1]], "fov_deg": viewport[2], "level": level_for_fov(viewport[2]),
                "detections": det_index.viewport(viewport[0], viewport[1], viewport[2], max_items)}
    return {
        "schema": SKYMAP_SCHEMA, "run_id": result.get("run_id"), "data_origin": result.get("data_origin"),
        "watermark": "SYNTHETIC - NOT REAL DATA" if result.get("data_origin") == "SYNTHETIC" else None,
        "frame": {"system": "ICRS", "units": "deg", "epoch": "positions as measured at each observation time",
                  "projections": {"local": "gnomonic (TAN) about the field centre", "all_sky": "Mollweide"},
                  "orientation": "east to the left, north up (astronomical convention)",
                  "uncertainty": "1-sigma ellipse: semi-axes in arcsec, position angle east of north"},
        "field": {"ra_deg": ra0, "dec_deg": dec0, "radius_deg": radius},
        "layers": {"coverage": coverage, "coverage_depth": coverage_depth(polys, ra0, dec0, radius) if polys else None,
                   "detections": detections, "sources": sources, "candidates": candidates, "associations": associations,
                   "tracks": tracks, "bands": sorted(bands.values(), key=lambda b: b["detector"]),
                   "time": {"epochs": (result.get("playback") or {}).get("epochs", []),
                            "frames": [{"index": f["index"], "obs_id": f["obs_id"], "mjd": f["mjd"], "iso_utc": f["iso_utc"]}
                                       for f in (result.get("playback") or {}).get("frames", [])]}},
        "line_semantics": {"associations": "membership under the stationary-source gate (not a trajectory)",
                           "tracks": "time-ordered candidate linkage under the distant-body hypothesis (not an orbit)"},
        "external_layers": _external_layers(result.get("context") or {}),
        "index": {"scheme": "k-d tree on unit vectors for cones; equal-area-like dec-band/RA-cell tiles for aggregation",
                  "tile_id": "L<level>:<band>:<cell>", "band_height_deg": "180 / 2^(level+1)"},
        "viewport": view,
    }
