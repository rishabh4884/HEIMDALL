"""Known small bodies from the JPL Small-Body Identification API (sb_ident, version 1.1).

The interface follows the official documentation
(https://ssd-api.jpl.nasa.gov/doc/sb_ident.html):

- the observer is given as a geocentric ICRF state vector ``xobs`` (km, km/s);
  SPHEREx headers provide ``X_SC/Y_SC/Z_SC`` and ``VX_SC/VY_SC/VZ_SC`` at mid-exposure;
- ``obs-time`` is given as a JD (UT); the field as centre (``hh-mm-ss.ss``,
  ``dd-mm-ss.ss`` with ``M`` for negative) and half-widths in degrees;
- ``two-pass=true`` adds numerically integrated positions, and the first pass
  gives estimated position errors (``Est. error RA/Dec (")``).

A detection matches a predicted body when its separation is within
``sqrt(chi2_2^-1(gate))`` times the combined sigma (detection centroid, registration floor and
ephemeris error). When the API gives no ephemeris error, a stated provisional value is used.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from dataclasses import dataclass, field

from scipy import stats as _st

from ..engine.skygeom import angular_separation_deg
from . import CachedService

SBIDENT_URL = "https://ssd-api.jpl.nasa.gov/sb_ident.api"
EXPECTED_VERSION = "1.1"


def _sexagesimal(value_deg: float, hours: bool) -> str:
    v = value_deg / 15.0 if hours else value_deg
    sign = "M" if v < 0 else ""
    v = abs(v)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60.0) * 3600.0
    return f"{sign}{d:02d}-{m:02d}-{s:05.2f}"


def parse_ra(text: str) -> float:
    h, m, s = (float(x) for x in text.strip().split(":"))
    return 15.0 * (h + m / 60.0 + s / 3600.0)


def parse_dec(text: str) -> float:
    m = re.match(r"\s*([+-]?)(\d+)\s+(\d+)'\s*([\d.]+)", text)
    if not m:
        raise ValueError(f"unparseable declination {text!r}")
    value = float(m.group(2)) + float(m.group(3)) / 60.0 + float(m.group(4)) / 3600.0
    return -value if m.group(1) == "-" else value


@dataclass
class Prediction:
    name: str
    ra: float
    dec: float
    vmag: float | None
    error_arcsec: float | None  # uncertainty of the reported position; None for second-pass positions (not given)
    source_pass: str
    first_pass_error_arcsec: float | None = None  # describes the two-body first-pass position, not the second pass


@dataclass
class FieldQuery:
    obs_id: str
    status: str
    reason: str
    from_cache: bool
    params: dict
    predictions: list[Prediction] = field(default_factory=list)
    api_version: str | None = None


def parse_response(raw: bytes) -> tuple[list[Prediction], str | None]:
    payload = json.loads(raw.decode("utf-8"))
    version = (payload.get("signature") or {}).get("version")
    first, second = {}, {}
    for key_fields, key_data, store, label in (("fields_first", "data_first_pass", first, "first"),
                                               ("fields_second", "data_second_pass", second, "second")):
        fields_ = payload.get(key_fields) or []
        for row in payload.get(key_data) or []:
            rec = dict(zip(fields_, row))
            store[rec.get("Object name")] = rec
    out = []
    # When the second (numerically integrated) pass ran, it alone lists the bodies in the field; first-pass rows are
    # two-body screening candidates, many far outside it. The first pass only supplies error estimates.
    names = set(second) if "n_second_pass" in payload else set(first)
    for name in sorted(names):
        rec = second.get(name) or first[name]
        first_err = None
        f = first.get(name)
        if f is not None:
            vals = [float(f[k]) for k in ('Est. error RA (")', 'Est. error Dec (")') if k in f and _isnum(f[k])]
            first_err = max(vals) if vals else None
        vmag = float(rec["Visual magnitude (V)"]) if _isnum(rec.get("Visual magnitude (V)")) else None
        in_second = name in second
        out.append(Prediction(name=name, ra=parse_ra(rec["Astrometric RA (hh:mm:ss)"]),
                              dec=parse_dec(rec["Astrometric Dec (dd mm'ss\")"]), vmag=vmag,
                              error_arcsec=None if in_second else first_err, source_pass="second" if in_second else "first",
                              first_pass_error_arcsec=first_err))
    return out, version


def _isnum(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


class SmallBodyIdentifier:
    def __init__(self, service: CachedService, vmag_limit: float = 21.0) -> None:
        self.service = service
        self.vmag_limit = vmag_limit

    def field(self, obs_id: str, mjd_utc: float, ra: float, dec: float, half_width_deg: float,
              state_km: tuple | None) -> FieldQuery:
        if state_km is None or not all(v is not None and math.isfinite(v) for v in state_km[:3]):
            return FieldQuery(obs_id, "FAILED", "no observer state vector in the product", False, {})
        # mag-required=true and vmag-lim keep the query to bodies bright enough to matter (minutes -> about 25 s).
        # Bodies without magnitude parameters (mostly comets) are therefore not checked.
        params = {"xobs": ",".join(f"{v:.6E}" for v in state_km if v is not None), "obs-time": f"{mjd_utc + 2400000.5:.6f}",
                  "fov-ra-center": _sexagesimal(ra % 360.0, hours=True), "fov-dec-center": _sexagesimal(dec, hours=False),
                  "fov-ra-hwidth": f"{half_width_deg:.4f}", "fov-dec-hwidth": f"{half_width_deg:.4f}",
                  "two-pass": "true", "suppress-first-pass": "false", "mag-required": "true",
                  "vmag-lim": f"{self.vmag_limit:g}", "req-elem": "false"}
        url = SBIDENT_URL + "?" + urllib.parse.urlencode(params)

        def validate(raw: bytes):
            try:
                parse_response(raw)
            except (ValueError, KeyError, TypeError) as exc:
                return f"unexpected sb_ident response: {exc}"
            return None

        ans = self.service.fetch(url, validate=validate)
        if ans.status != "COMPLETED":
            return FieldQuery(obs_id, ans.status, ans.reason, False, params)
        preds, version = parse_response(ans.body)
        note = "" if version == EXPECTED_VERSION else f"API version {version} differs from documented {EXPECTED_VERSION}"
        return FieldQuery(obs_id, "COMPLETED", note, ans.from_cache, params, preds, version)


def match_detections(detections, query: FieldQuery, floor_arcsec: float, assumed_error_arcsec: float,
                     gate_probability: float) -> list[dict]:
    """Known bodies predicted within tolerance of each detection of one image."""
    k = math.sqrt(float(_st.chi2.ppf(gate_probability, 2)))
    out = []
    for d in detections:
        for p in query.predictions:
            err = p.error_arcsec if p.error_arcsec is not None else assumed_error_arcsec
            tol = k * math.sqrt(d.sigma_major_arcsec ** 2 + floor_arcsec ** 2 + err ** 2)
            sep = float(angular_separation_deg(d.ra, d.dec, p.ra, p.dec)) * 3600.0
            if sep <= tol:
                out.append({"det_id": d.det_id, "obs_id": d.obs_id, "name": p.name, "separation_arcsec": sep,
                            "tolerance_arcsec": tol, "vmag": p.vmag, "ephemeris_error_arcsec": p.error_arcsec,
                            "ephemeris_error_assumed": p.error_arcsec is None})
    return out


def confirm_across_subjects(status: dict, all_matches: list[dict]) -> dict:
    """Upgrade a one-image match when the same body is matched, at its predicted positions, in other images too.

    A body predicted by its ephemeris at several times and positions and found at each one is identified even
    when HEIMDALL's association split it into several single-image sources (it moves between images).
    """
    if status.get("status") != "PROBABLE_MATCH" or status.get("partial"):
        return status
    images = sorted({m["obs_id"] for m in all_matches if m["name"] == status["name"]})
    if len(images) < 2:
        return status
    return {**status, "status": "CONFIRMED_MATCH", "n_images_matched_all_sources": len(images), "images": images,
            "note": f"{status['name']} is found at its predicted position in {len(images)} images (as separate sources, "
                    "because it moves between images)"}


def subject_status(det_obs_ids: list[str], matches: list[dict], queries: dict[str, FieldQuery]) -> dict:
    """Known-object state for a subject (source or track) from its detections' images."""
    relevant = sorted(set(det_obs_ids))
    incomplete = [o for o in relevant if o not in queries or queries[o].status != "COMPLETED"]
    names: dict[str, set] = {}
    for m in matches:
        names.setdefault(m["name"], set()).add(m["obs_id"])
    if names:
        name, imgs = max(names.items(), key=lambda kv: len(kv[1]))
        # Confirmed only when the body explains every detection image of the subject. A body matching some images of a track while
        # the others are unexplained makes the track a mixed association, not an identified body.
        partial = len(imgs) < len(relevant)
        state = "CONFIRMED_MATCH" if len(imgs) >= 2 and not partial else "PROBABLE_MATCH"
        out = {"status": state, "name": name, "n_images_matched": len(imgs), "n_images": len(relevant),
               "matches": [m for m in matches if m["name"] == name], "incomplete_queries": incomplete}
        if partial:
            out["partial"] = True
            out["note"] = f"{name} matches {len(imgs)} of {len(relevant)} detection images; the other detections are not explained by it"
        return out
    if incomplete:
        return {"status": "NOT_ASSESSABLE", "reason": f"{len(incomplete)} of {len(relevant)} image queries did not complete",
                "incomplete_queries": incomplete, "n_images": len(relevant)}
    return {"status": "NO_MATCH", "n_images": len(relevant),
            "reason": "no known small body predicted within tolerance in any image (JPL SB Ident)"}
