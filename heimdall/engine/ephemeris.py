"""Observer positions for parallax calculations.

The observer's heliocentric position is Earth (barycentric) minus Sun
(barycentric), from astropy's built-in ERFA ephemeris (``epv00``; no download),
plus the spacecraft's geocentric offset ``X_SC/Y_SC/Z_SC`` when the product
provides it. Axes are ICRS/BCRS equatorial; the SPHEREx header vector is
assumed to use the same equatorial axes (GCRS); its frame is not stated in the
header. At distances above about 10 AU the spacecraft offset (about 1e-5 AU) is
negligible.
"""

from __future__ import annotations

import numpy as np
from astropy import units as u
from astropy.coordinates import get_body_barycentric, solar_system_ephemeris
from astropy.time import Time

KM_PER_AU = 149597870.7


def observer_heliocentric_au(mjd_utc, spacecraft_geocentric_km=None) -> np.ndarray:
    """Heliocentric observer positions (N, 3) in AU for MJD (UTC) epochs."""
    mjd = np.atleast_1d(np.asarray(mjd_utc, dtype=float))
    t = Time(mjd, format="mjd", scale="utc").tdb
    with solar_system_ephemeris.set("builtin"):
        earth = get_body_barycentric("earth", t).xyz.to_value(u.AU)
        sun = get_body_barycentric("sun", t).xyz.to_value(u.AU)
    pos = (earth - sun).T.reshape(-1, 3)
    if spacecraft_geocentric_km is not None:
        sc = np.asarray(spacecraft_geocentric_km, dtype=float).reshape(-1, 3)
        ok = np.all(np.isfinite(sc), axis=1)
        pos[ok] += sc[ok] / KM_PER_AU
    return pos
