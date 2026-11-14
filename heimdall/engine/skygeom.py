"""Spherical geometry on the celestial sphere (ICRS, degrees).

Tangent-plane offsets use the gnomonic projection with xi increasing to the
east and eta to the north. Covariances of sky positions are 2x2 matrices in
(xi, eta) in arcsec^2.
"""

from __future__ import annotations

import math

import numpy as np

DEG = math.pi / 180.0
ARCSEC = DEG / 3600.0
ARCSEC2_TO_SR = ARCSEC * ARCSEC


def unit_vector(ra_deg, dec_deg) -> np.ndarray:
    ra = np.radians(np.asarray(ra_deg, dtype=float))
    dec = np.radians(np.asarray(dec_deg, dtype=float))
    cd = np.cos(dec)
    return np.stack([cd * np.cos(ra), cd * np.sin(ra), np.sin(dec)], axis=-1)


def radec_from_vector(v) -> tuple[np.ndarray, np.ndarray]:
    v = np.asarray(v, dtype=float)
    x, y, z = v[..., 0], v[..., 1], v[..., 2]
    ra = np.degrees(np.arctan2(y, x)) % 360.0
    dec = np.degrees(np.arctan2(z, np.hypot(x, y)))
    return ra, dec


def angular_separation_deg(ra1, dec1, ra2, dec2) -> np.ndarray:
    """Vincenty formula, accurate at all separations."""
    r1, d1, r2, d2 = (np.radians(np.asarray(a, dtype=float)) for a in (ra1, dec1, ra2, dec2))
    dra = r2 - r1
    num1 = np.cos(d2) * np.sin(dra)
    num2 = np.cos(d1) * np.sin(d2) - np.sin(d1) * np.cos(d2) * np.cos(dra)
    den = np.sin(d1) * np.sin(d2) + np.cos(d1) * np.cos(d2) * np.cos(dra)
    return np.degrees(np.arctan2(np.hypot(num1, num2), den))


def tangent_basis(ra0_deg: float, dec0_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Unit vectors (u0, east, north) at a reference direction."""
    a = math.radians(ra0_deg)
    d = math.radians(dec0_deg)
    u0 = np.array([math.cos(d) * math.cos(a), math.cos(d) * math.sin(a), math.sin(d)])
    east = np.array([-math.sin(a), math.cos(a), 0.0])
    north = np.array([-math.sin(d) * math.cos(a), -math.sin(d) * math.sin(a), math.cos(d)])
    return u0, east, north


def gnomonic(ra_deg, dec_deg, ra0_deg: float, dec0_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Gnomonic (TAN) projection about (ra0, dec0). Returns (xi, eta) in degrees.

    Points 90 degrees or more from the tangent point are returned as NaN.
    """
    ra = np.radians(np.asarray(ra_deg, dtype=float))
    dec = np.radians(np.asarray(dec_deg, dtype=float))
    a0 = math.radians(ra0_deg)
    d0 = math.radians(dec0_deg)
    dra = ra - a0
    cosc = math.sin(d0) * np.sin(dec) + math.cos(d0) * np.cos(dec) * np.cos(dra)
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = np.cos(dec) * np.sin(dra) / cosc
        eta = (math.cos(d0) * np.sin(dec) - math.sin(d0) * np.cos(dec) * np.cos(dra)) / cosc
    bad = cosc <= 0
    xi = np.where(bad, np.nan, xi)
    eta = np.where(bad, np.nan, eta)
    return np.degrees(xi), np.degrees(eta)


def inverse_gnomonic(xi_deg, eta_deg, ra0_deg: float, dec0_deg: float) -> tuple[np.ndarray, np.ndarray]:
    xi = np.radians(np.asarray(xi_deg, dtype=float))
    eta = np.radians(np.asarray(eta_deg, dtype=float))
    a0 = math.radians(ra0_deg)
    d0 = math.radians(dec0_deg)
    rho = np.hypot(xi, eta)
    c = np.arctan(rho)
    with np.errstate(divide="ignore", invalid="ignore"):
        dec = np.arcsin(np.cos(c) * math.sin(d0) + np.where(rho > 0, eta * np.sin(c) * math.cos(d0) / rho, 0.0))
        ra = a0 + np.arctan2(xi * np.sin(c), rho * math.cos(d0) * np.cos(c) - eta * math.sin(d0) * np.sin(c))
    return np.degrees(ra) % 360.0, np.degrees(dec)


def offsets_arcsec(ra_deg, dec_deg, ra0_deg: float, dec0_deg: float) -> np.ndarray:
    """Tangent-plane offsets (xi east, eta north) in arcsec, shape (..., 2)."""
    xi, eta = gnomonic(ra_deg, dec_deg, ra0_deg, dec0_deg)
    return np.stack([xi * 3600.0, eta * 3600.0], axis=-1)


def mahalanobis2(delta, cov) -> float:
    delta = np.asarray(delta, dtype=float)
    cov = np.asarray(cov, dtype=float)
    return float(delta @ np.linalg.solve(cov, delta))


def covariance_ellipse(cov) -> tuple[float, float, float]:
    """1-sigma ellipse (semi-major, semi-minor, position angle in degrees east of north)."""
    cov = np.asarray(cov, dtype=float)
    vals, vecs = np.linalg.eigh(cov)
    vals = np.clip(vals, 0.0, None)
    major = vecs[:, 1]  # (east, north) components of the major axis
    pa = math.degrees(math.atan2(major[0], major[1])) % 180.0
    return float(math.sqrt(vals[1])), float(math.sqrt(vals[0])), pa


def is_positive_definite(cov) -> bool:
    cov = np.asarray(cov, dtype=float)
    if cov.shape != (2, 2) or not np.all(np.isfinite(cov)) or not np.allclose(cov, cov.T):
        return False
    try:
        np.linalg.cholesky(cov)
    except np.linalg.LinAlgError:
        return False
    return True


def polygon_contains(poly_ra, poly_dec, ra: float, dec: float) -> bool:
    """Point-in-polygon on the sphere, via the tangent plane at the polygon centre.

    Valid for polygons much smaller than a hemisphere (all image footprints).
    """
    vecs = unit_vector(poly_ra, poly_dec)
    centre = vecs.mean(axis=0)
    centre /= np.linalg.norm(centre)
    ra0, dec0 = radec_from_vector(centre)
    px, py = gnomonic(poly_ra, poly_dec, float(ra0), float(dec0))
    qx, qy = gnomonic(ra, dec, float(ra0), float(dec0))
    if not (np.isfinite(qx) and np.isfinite(qy)) or not np.all(np.isfinite(px)):
        return False
    inside = False
    n = len(px)
    for i in range(n):
        x1, y1, x2, y2 = px[i], py[i], px[(i + 1) % n], py[(i + 1) % n]
        if (y1 > qy) != (y2 > qy):
            x_cross = x1 + (qy - y1) * (x2 - x1) / (y2 - y1)
            if qx < x_cross:
                inside = not inside
    return inside
