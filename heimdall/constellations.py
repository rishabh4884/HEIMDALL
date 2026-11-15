"""IAU constellation boundaries for the sky-map reference layer (``heimdall.constellation_boundaries/1.0``).

Source: Davenhall A.C. & Leggett S.K. (1989), *Constellation Boundary Data*, CDS catalogue VI/49.
* ``constbnd.dat`` gives every constellation's outline as ordered, closed B1875.0 polygons (counter-clockwise, first point repeated
  last). Delporte's 1930 boundaries run along lines of constant B1875 right ascension or declination.
* ``bound_20.dat`` gives J2000 boundary points (CDS revision 2024-07-23) but not in outline order, so it cannot be drawn directly.

Method (the catalogue's own construction of its J2000 file): each B1875 edge is interpolated along its constant-RA or
constant-Dec line in steps of at most 0.5 degree and precessed with astropy (FK4, equinox B1875 -> FK5, equinox J2000). The
result is then checked against the original vertices of ``bound_20`` (type ``O``) and the maximum deviation is reported with the
data. Files are fetched at run time from CDS or its CfA VizieR mirror and cached under the cache directory; nothing is committed
to the repository. Offline mode serves only the cache. These are the official boundaries (areas), not stick figures, which have
no single authoritative definition and are not drawn.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import threading
from datetime import UTC, datetime
from pathlib import Path

from .data_access.http import urllib_transport

SCHEMA = "heimdall.constellation_boundaries/1.1"
MIRRORS = ("https://cdsarc.cds.unistra.fr/ftp/VI/49/", "https://vizier.cfa.harvard.edu/VizieR/ftp/cats/VI/49/")
ATTRIBUTION = ("Constellation boundaries: Davenhall & Leggett (1989), CDS catalogue VI/49 (constbnd B1875 outlines precessed to "
               "J2000; checked against bound_20, CDS revision 2024-07-23). Data from CDS, Strasbourg, used with acknowledgement.")
MAX_BYTES = 4 * 1024 * 1024
STEP_DEG = 0.5
_lock = threading.Lock()


class BoundaryError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def parse_constbnd(text: str) -> list[dict]:
    """Ordered B1875 outlines from ``constbnd.dat`` (F8.5 RAhr, F9.5 DEdeg, A4 cst, A4 adjacent; blank adjacent = origin)."""
    outlines: list[dict] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            ra_h, dec, cst, adj = float(line[0:8]), float(line[9:18]), line[19:23].strip(), line[24:28].strip()
        except ValueError:
            raise BoundaryError("MALFORMED", f"line {n} does not match the VI/49 constbnd format") from None
        if not (0 <= ra_h <= 24 and -90 <= dec <= 90 and cst.isalnum()):
            raise BoundaryError("MALFORMED", f"line {n} is out of range")
        if not adj or not outlines or outlines[-1]["cst"] != cst:
            outlines.append({"cst": cst, "ra1875": [], "dec1875": []})
        outlines[-1]["ra1875"].append(ra_h * 15.0)
        outlines[-1]["dec1875"].append(dec)
    names = {o["cst"] for o in outlines}
    if len(names) < 88:
        raise BoundaryError("MALFORMED", f"only {len(names)} constellations; expected all 88 (Serpens in two parts)")
    return outlines


def densify(ra: list[float], dec: list[float], step: float = STEP_DEG) -> tuple[list[float], list[float]]:
    """Points along each edge's constant-RA or constant-Dec line (B1875), never a straight chord across the sphere."""
    out_ra, out_dec = [ra[0]], [dec[0]]
    for (a0, d0), (a1, d1) in zip(zip(ra, dec), zip(ra[1:], dec[1:]), strict=False):
        da = ((a1 - a0 + 180.0) % 360.0) - 180.0
        dd = d1 - d0
        span = max(abs(da) * math.cos(math.radians(max(abs(d0), abs(d1)) if abs(dd) < 1e-9 else 0.0)), abs(dd))
        n = max(1, math.ceil(span / step))
        for k in range(1, n + 1):
            out_ra.append((a0 + da * k / n) % 360.0)
            out_dec.append(d0 + dd * k / n)
    return out_ra, out_dec


def precess(ra: list[float], dec: list[float]) -> tuple[list[float], list[float]]:
    import astropy.units as u
    from astropy.coordinates import FK4, FK5, SkyCoord
    from astropy.time import Time
    c = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame=FK4(equinox=Time("B1875"), obstime=Time("B1875")))
    j = c.transform_to(FK5(equinox=Time("J2000")))
    return [round(float(x), 5) for x in j.ra.deg], [round(float(x), 5) for x in j.dec.deg]


def build(constbnd_text: str) -> list[dict]:
    out = []
    for o in parse_constbnd(constbnd_text):
        ra, dec = densify(o["ra1875"], o["dec1875"])
        ra2, dec2 = precess(ra, dec)
        out.append({"cst": o["cst"], "ra": ra2, "dec": dec2})
    return out


def check_against_bound20(outlines: list[dict], bound20_text: str) -> dict:
    """Largest distance (deg) from each original J2000 vertex of bound_20 to the derived outline of the same constellation."""
    import numpy as np
    by = {}
    for o in outlines:
        key = o["cst"].upper()[:3]
        by.setdefault(key, []).append(np.radians(np.column_stack([o["ra"], o["dec"]])))
    worst, n = 0.0, 0
    for line in bound20_text.splitlines():
        if len(line) < 30 or line[29] != "O":
            continue
        ra, dec, cst = math.radians(float(line[0:11])), math.radians(float(line[12:23])), line[24:28].strip().upper()[:3]
        pts = by.get(cst)
        if not pts:
            continue
        p = np.vstack(pts)
        cosd = np.sin(dec) * np.sin(p[:, 1]) + np.cos(dec) * np.cos(p[:, 1]) * np.cos(p[:, 0] - ra)
        worst = max(worst, float(np.degrees(np.arccos(np.clip(cosd.max(), -1, 1)))))
        n += 1
    return {"vertices_checked": n, "max_deviation_deg": round(worst, 5)}


def _fetch(transport, name: str) -> tuple[bytes, str]:
    errors = []
    for base in MIRRORS:
        try:
            resp = transport("GET", base + name, None, {}, 20.0)
        except Exception as e:  # noqa: BLE001 - next mirror
            errors.append(f"{base}{name}: {type(e).__name__}")
            continue
        if resp.status == 200 and 0 < len(resp.body) <= MAX_BYTES:
            return resp.body, base + name
        errors.append(f"{base}{name}: HTTP {resp.status}")
    raise BoundaryError("UNREACHABLE", "; ".join(errors))


def boundaries(cache_dir: Path, offline: bool, transport=urllib_transport) -> dict:
    cached = Path(cache_dir) / "reference" / "vi49_constellations.json"
    with _lock:
        if cached.is_file():
            try:
                return json.loads(cached.read_text(encoding="utf-8")) | {"cached": True}
            except ValueError:
                pass
        if offline:
            raise BoundaryError("OFFLINE", "offline mode and no cached copy of the CDS VI/49 boundaries")
        raw, url = _fetch(transport, "constbnd.dat")
        outlines = build(raw.decode("ascii"))
        try:
            gz, url20 = _fetch(transport, "bound_20.dat.gz")
            check = check_against_bound20(outlines, gzip.decompress(gz).decode("ascii")) | {"reference": url20}
        except (BoundaryError, OSError, ValueError) as e:
            check = {"vertices_checked": 0, "note": f"bound_20 check not performed: {e}"}
        doc = {"schema": SCHEMA, "source_url": url, "sha256": hashlib.sha256(raw).hexdigest(), "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
               "frame": "FK5 J2000 (ICRS-aligned to well below the display resolution)", "method": "B1875 edges along constant RA/Dec, 0.5 deg steps, precessed FK4 B1875 -> FK5 J2000",
               "validation": check, "attribution": ATTRIBUTION, "kind": "IAU boundaries (not stick figures)",
               "n_constellations": len({o["cst"] for o in outlines}), "outlines": outlines}
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(".partial")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        tmp.replace(cached)
        return doc | {"cached": False}
