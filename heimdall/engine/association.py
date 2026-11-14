"""Cross-observation association of detections (stationary-source hypothesis).

Two detections i, j from different observations are *compatible* when

    d2 = delta^T (C_i + C_j + 2 s^2 I)^-1 delta  <=  chi2_2^-1(gate_probability)

with delta their tangent-plane separation (arcsec), C their measurement
covariances and s the per-image registration floor. Compatible pairs form a
graph; its connected components are candidate sources.

Ambiguity is kept explicit, never resolved silently:
- CONFLICT: a component holds two or more detections from one observation.
  It is split brightest first: each detection joins the nearest group within
  the gate that has no detection from its observation, or starts a new group
  (RELEASED_FROM_CONFLICT). Every alternative grouping is recorded.
- CHAINED: a member lies outside the gate from the component mean (A~B, B~C
  but A!~C).

Spatial search uses a k-d tree on unit vectors (chord radius), so cost grows as
O(N log N) rather than O(N^2).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from scipy import stats as _st
from scipy.spatial import cKDTree

from .config import AssociationParams
from .detection import DetectionRecord
from .skygeom import inverse_gnomonic, offsets_arcsec, unit_vector

ASSOCIATION_METHOD_ID = "mahalanobis_gate_components"
ASSOCIATION_METHOD_VERSION = "1.1.0"  # 1.1.0: conflicting components are split brightest-first


@dataclass
class SourceCluster:
    source_id: str
    ra: float
    dec: float
    cov_arcsec2: list[list[float]]
    det_ids: list[str]
    obs_ids: list[str]
    n_obs: int
    ambiguous: bool = False
    ambiguity_kinds: list[str] = field(default_factory=list)
    released_det_ids: list[str] = field(default_factory=list)
    alternative_groupings: list[list[str]] = field(default_factory=list)
    released_from: str | None = None
    max_member_d2: float = 0.0


def gate_d2(params: AssociationParams) -> float:
    return float(_st.chi2.ppf(params.gate_probability, 2))


def _weighted_mean(dets: list[DetectionRecord], floor2: float) -> tuple[float, float, np.ndarray]:
    ra0, dec0 = dets[0].ra, dets[0].dec
    pos = offsets_arcsec([d.ra for d in dets], [d.dec for d in dets], ra0, dec0)
    weights = [np.linalg.inv(d.cov() + floor2 * np.eye(2)) for d in dets]
    w_sum = sum(weights)
    cov = np.linalg.inv(w_sum)
    mean = cov @ sum(w @ p for w, p in zip(weights, pos))
    ra, dec = inverse_gnomonic(mean[0] / 3600.0, mean[1] / 3600.0, ra0, dec0)
    return float(ra), float(dec), cov


def _d2_to(det: DetectionRecord, ra: float, dec: float, cov: np.ndarray, floor2: float) -> float:
    delta = offsets_arcsec(det.ra, det.dec, ra, dec)
    total = det.cov() + cov + floor2 * np.eye(2)
    return float(delta @ np.linalg.solve(total, delta))


def associate(detections: list[DetectionRecord], params: AssociationParams, id_prefix: str = "S") -> list[SourceCluster]:
    """Group detections into stationary-source candidates with explicit ambiguity."""
    dets = sorted(detections, key=lambda d: (-d.snr_fit if math.isfinite(d.snr_fit) else 0.0, d.det_id))
    n = len(dets)
    if n == 0:
        return []
    floor2 = params.registration_floor_arcsec ** 2
    g2 = gate_d2(params)
    sig_max = max(math.sqrt(max(np.linalg.eigvalsh(d.cov())[-1], 0.0) + floor2) for d in dets)
    radius_arcsec = min(params.max_search_arcsec, math.sqrt(g2) * math.sqrt(2.0) * sig_max)
    chord = 2.0 * math.sin(math.radians(radius_arcsec / 3600.0) / 2.0)
    tree = cKDTree(unit_vector([d.ra for d in dets], [d.dec for d in dets]))
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in sorted(tree.query_pairs(chord)):
        a, b = dets[i], dets[j]
        if a.obs_id == b.obs_id:
            continue
        delta = offsets_arcsec(b.ra, b.dec, a.ra, a.dec)
        total = a.cov() + b.cov() + 2.0 * floor2 * np.eye(2)
        if float(delta @ np.linalg.solve(total, delta)) <= g2:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[max(ri, rj)] = min(ri, rj)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    clusters: list[SourceCluster] = []
    for root in sorted(groups):
        members = [dets[i] for i in groups[root]]  # brightest first (dets is sorted by decreasing SNR)
        if len({d.obs_id for d in members}) == len(members):
            subs = [{"dets": members, "kinds": [], "alts": [], "released": [], "blocked_by": None}]
        else:
            subs = _split_conflicting(members, floor2, g2)
        for sub in subs:
            chosen = sorted(sub["dets"], key=lambda d: d.det_id)
            ra, dec, cov = _weighted_mean(chosen, floor2)
            d2s = [_d2_to(d, ra, dec, cov, floor2) for d in chosen] if len(chosen) > 1 else [0.0]
            kinds = list(sub["kinds"]) + (["CHAINED"] if max(d2s) > g2 else [])
            clusters.append(SourceCluster(
                source_id="", ra=ra, dec=dec, cov_arcsec2=cov.tolist(), det_ids=[d.det_id for d in chosen],
                obs_ids=sorted({d.obs_id for d in chosen}), n_obs=len({d.obs_id for d in chosen}),
                ambiguous=bool(kinds), ambiguity_kinds=kinds, released_det_ids=sorted(sub["released"]),
                alternative_groupings=sub["alts"][:10], max_member_d2=float(max(d2s)), released_from=sub["blocked_by"]))

    clusters.sort(key=lambda c: (round(c.dec, 7), round(c.ra, 7), c.det_ids[0]))
    by_det = {}
    for k, c in enumerate(clusters):
        c.source_id = f"{id_prefix}{k:05d}"
        for det_id in c.det_ids:
            by_det[det_id] = c.source_id
    for c in clusters:
        if c.released_from is not None:  # a member det_id of the blocking group until ids exist
            c.released_from = by_det.get(c.released_from)
    return clusters


def _split_conflicting(members: list[DetectionRecord], floor2: float, g2: float) -> list[dict]:
    """Split a component that holds several detections from one observation.

    Members are taken brightest first. Each joins the nearest group within the gate that has no
    detection from its observation yet; otherwise it starts a new group. A bright source therefore
    keeps all its detections, and fainter duplicates (for example PSF-fit residuals near a bright
    star) form their own groups. When a detection was within the gate of a group that already had its
    observation, that choice is explicit: the group is marked CONFLICT with the alternative grouping,
    and a new group started by the detection is marked RELEASED_FROM_CONFLICT.
    """
    subs: list[dict] = []
    for d in members:
        best, best_d2, blocked = None, math.inf, []
        for s in subs:
            d2 = _d2_to(d, s["ra"], s["dec"], s["cov"], floor2)
            if d2 > g2:
                continue
            if d.obs_id in s["obs"]:
                blocked.append(s)
            elif d2 < best_d2:
                best, best_d2 = s, d2
        if best is None:
            best = {"dets": [], "obs": set(), "kinds": [], "alts": [], "released": [],
                    "blocked_by": blocked[0]["dets"][0].det_id if blocked else None}
            if blocked:
                best["kinds"].append("RELEASED_FROM_CONFLICT")
            subs.append(best)
        best["dets"].append(d)
        best["obs"].add(d.obs_id)
        best["ra"], best["dec"], best["cov"] = _weighted_mean(best["dets"], floor2)
        for s in blocked:
            if "CONFLICT" not in s["kinds"]:
                s["kinds"].append("CONFLICT")
            s["released"].append(d.det_id)
            s["alts"].append(sorted([x.det_id for x in s["dets"] if x.obs_id != d.obs_id] + [d.det_id]))
    return subs
