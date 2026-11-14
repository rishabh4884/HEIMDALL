"""Typed evidence for scientific investigations, extracted from a serialized investigation result.

The extractor reads the plain-JSON sections the pipeline already produces (sources, transients, tracks, linking, candidates, context,
error models); it measures nothing new. Each item states what it is, where it came from, how admissible it is and what it depends on.

Admissibility: the pattern-level quality gate (candidate attention and validity, which already encode the critical checks) decides
whether a *measured pattern* may support an astrophysical hypothesis. Checks and validity are never evidence themselves. Identification
evidence (Gaia, JPL) is admissible when its query completed.

Dependency groups. Items are one group when they come from the same fit, the same external record, a declared dependency, or share
detections *within the same measurement domain* (flux with flux, position with position). Flux and position items that merely share a
detection are different measurements: they stay in separate groups and are linked by a ``SHARED_DETECTION_CROSS_DOMAIN`` relation.
Image-level items (error models) are never merged with anything: an ``IMAGE_CORRELATED`` relation records the limitation. Support is
counted in groups, not items.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

EVIDENCE_VERSION = "evidence-1.0.0-provisional"

DOMAIN = {
    "FLUX_CHANGE": "FLUX", "SINGLE_EPOCH_EXCESS": "FLUX", "SPECTRAL_SHAPE": "FLUX", "SPECTRAL_CHANGE": "FLUX",
    "POSITION_CHANGE": "POSITION", "PROPER_MOTION": "POSITION", "PARALLAX": "POSITION", "LINKED_MOTION": "POSITION",
    "TRACKLET_RATE": "POSITION", "CATALOGUE_STAR": "EXTERNAL_CATALOGUE", "KNOWN_SMALL_BODY": "EXTERNAL_EPHEMERIS",
    "ERROR_MODEL": "IMAGE_SYSTEMATIC",
}
ACTIVE_ATTENTION = ("ATTEND", "WATCH")
STAR_MAX_PARALLAX_ARCSEC = 1.0


@dataclass
class EvidenceItem:
    evidence_id: str
    type: str
    domain: str
    subject_id: str
    candidate_ids: list[str]
    ref: str
    value: dict
    uncertainty: dict | None
    quality_grade: str | None
    admissible: bool
    admissibility_reasons: list[str]
    basis: dict  # det_ids, obs_ids, fit_id, record_id, depends_on (evidence types of the same subject)
    relation: str  # CORRELATIONAL | PHYSICAL_CONSTRAINT | IDENTIFICATION | NON_DETECTION
    provenance: dict
    not_assessable_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class ResultIndex:
    """Lookup tables over one serialized result."""

    def __init__(self, result: dict) -> None:
        self.result = result
        self.sources = {s["source_id"]: s for s in result.get("sources") or []}
        self.transients = {t["source_id"]: t for t in result.get("transients") or []}
        self.tracks = {t["track_id"]: t for t in result.get("tracks") or []}
        self.not_linked = (result.get("linking") or {}).get("not_linked") or {}
        self.context = result.get("context") or {}


def candidate_gate(cand: dict) -> tuple[bool, list[str]]:
    """May this candidate's measured pattern support an astrophysical hypothesis? (Never raises attention.)"""
    cd = cand["candidate"]
    level = cd["attention_level"]
    reasons: list[str] = []
    ar = cd.get("attention_reason") or {}
    if level not in ACTIVE_ATTENTION:
        if level == "EXPLAINED":
            reasons.append("the pattern is attributed to an ordinary explanation")
        elif level == "NOT_ASSESSABLE":
            reasons.append("the candidate could not be assessed")
        else:
            failed, unresolved = ar.get("failed_checks") or [], ar.get("unresolved_checks") or []
            reasons.append("the pattern is not established: critical checks failed " + str(list(failed)) + " or unresolved " + str(list(unresolved)))
    state = (cand.get("cell_validity") or {}).get("state")
    if state == "MISCALIBRATED":
        reasons.append("the significance cell is MISCALIBRATED for this scope")
    return not reasons, reasons


def _sig(d: dict | None) -> float | None:
    return None if not d else d.get("sigma_equivalent")


def _wl_detector(coverage: list[dict]) -> list[int]:
    return sorted(c["detector"] for c in coverage if c.get("state") == "MEASURED")


def extract(index: ResultIndex, subject_kind: str, subject_id: str, candidates: list[dict]) -> list[EvidenceItem]:
    """All evidence for one subject (a source, or a track), in a deterministic order."""
    items: list[EvidenceItem] = []
    counter: dict[str, int] = {}

    def add(typ, ref, value, *, cands, quality=None, uncertainty=None, admissible=True, why=(), basis=None, relation="CORRELATIONAL",
            provenance=None, na=None):
        k = counter[typ] = counter.get(typ, 0) + 1
        b = {"det_ids": [], "obs_ids": [], "fit_id": None, "record_id": None, "depends_on": []}
        b.update(basis or {})
        items.append(EvidenceItem(evidence_id=f"E:{subject_id}:{typ}:{k}", type=typ, domain=DOMAIN[typ], subject_id=subject_id,
                                  candidate_ids=sorted(cands), ref=ref, value=value, uncertainty=uncertainty, quality_grade=quality,
                                  admissible=admissible, admissibility_reasons=list(why), basis=b, relation=relation,
                                  provenance=provenance or {}, not_assessable_reason=na))

    by_kind = {(c["kind"], c["subject_kind"]): c for c in candidates}
    src = index.sources.get(subject_id) if subject_kind == "SOURCE" else None
    trk = index.tracks.get(subject_id) if subject_kind == "TRACK" else None

    def gate(c):
        ok, reasons = candidate_gate(c)
        return ok, reasons

    # ---- flux change (variability candidate) ----
    c = by_kind.get(("VARIABILITY", "SOURCE"))
    if c and src is not None:
        done = [v for v in src["variability"] if v.get("status") == "COMPLETED" and v.get("significance")]
        if done:
            best = max(done, key=lambda v: (_sig(v["significance"]), v["series_id"]))
            series = next((s for s in src["series"] if s["series_id"] == best["series_id"]), {})
            obs = sorted(series.get("obs_ids") or [])
            dets = sorted(d for d in src["det_ids"] if d.split(":")[0] in set(obs))
            ok, why = gate(c)
            add("FLUX_CHANGE", f"sources[{subject_id}].variability[{best['series_id']}]",
                {"series_id": best["series_id"], "chi2": best["chi2"], "dof": best["dof"], "sigma_equivalent": _sig(best["significance"]),
                 "leave_one_out_sigma": _sig(best.get("leave_one_out")), "max_abs_pull": best["max_abs_pull"],
                 "peak_to_peak_ujy": best["peak_to_peak_ujy"], "wavelength_um": series.get("wavelength_um"),
                 "rescaled_sigma_equivalent": _sig(c["statistic"].get("empirically_rescaled"))},
                cands=[c["candidate_id"]], quality=c["quality_grade"], uncertainty={"flux_error_scale": c["statistic"].get("flux_error_scale")},
                admissible=ok, why=why, basis={"det_ids": dets, "obs_ids": obs, "fit_id": f"{subject_id}:flux_series:{best['series_id']}"},
                provenance={"method_id": c["statistic"]["method_id"]})
    # ---- single-epoch excess (transient candidate) ----
    c = by_kind.get(("TRANSIENT", "SOURCE"))
    if c and src is not None:
        t = index.transients.get(subject_id)
        if t:
            ok, why = gate(c)
            refs = sorted({r["obs_id"] for r in t.get("references") or []})
            add("SINGLE_EPOCH_EXCESS", f"transients[{subject_id}]",
                {"status": t["status"], "z": t.get("z"), "sigma_equivalent": _sig(t.get("significance")), "n_references": t.get("n_references"),
                 "probe_det_id": t.get("probe_det_id"), "probe_flux_ujy": t.get("probe_flux_ujy"), "reference_flux_ujy": t.get("reference_flux_ujy")},
                cands=[c["candidate_id"]], quality=c["quality_grade"], uncertainty={"probe_sigma_ujy": t.get("probe_sigma_ujy"),
                                                                                    "flux_error_scale": c["statistic"].get("flux_error_scale")},
                admissible=ok and t["status"] != "NOT_ASSESSABLE", why=why + (list(t.get("reasons") or [])),
                basis={"det_ids": [t["probe_det_id"]] if t.get("probe_det_id") else [], "obs_ids": sorted({t.get("probe_obs_id")} - {None} | set(refs)),
                       "fit_id": f"{subject_id}:transient_test"},
                provenance={"method_id": c["statistic"]["method_id"]},
                na=("; ".join(t.get("reasons") or []) or None) if t["status"] == "NOT_ASSESSABLE" else None)
    # ---- source astrometry (motion candidate on a source) ----
    c = by_kind.get(("MOTION", "SOURCE"))
    if c and src is not None:
        ok, why = gate(c)
        mo, px = src.get("motion") or {}, src.get("parallax") or {}
        basis = {"det_ids": sorted(src["det_ids"]), "obs_ids": sorted(src["obs_ids"]), "fit_id": f"{subject_id}:astrometric_fit"}
        add("POSITION_CHANGE", f"sources[{subject_id}].motion",
            {"stationarity_sigma_equivalent": _sig(mo.get("gof_stationary")), "linear_sigma_equivalent": _sig(mo.get("gof_linear")), "n_points": mo.get("n_points"),
             "baseline_days": mo.get("baseline_days"), "rescaled_sigma_equivalent": _sig(c["statistic"].get("empirically_rescaled")),
             "leave_one_image_out_sigma": _sig((c["statistic"].get("leave_one_image_out") or {}))},
            cands=[c["candidate_id"]], quality=c["quality_grade"], uncertainty={"astrometric_error_scale": c["statistic"].get("error_scale")},
            admissible=ok, why=why, basis=basis, provenance={"method_id": c["statistic"]["method_id"]})
        if mo.get("mu_arcsec_per_yr"):
            add("PROPER_MOTION", f"sources[{subject_id}].motion.mu_arcsec_per_yr", {"mu_arcsec_per_yr": mo["mu_arcsec_per_yr"]},
                cands=[c["candidate_id"]], quality=c["quality_grade"], uncertainty={"mu_cov": mo.get("mu_cov")}, admissible=ok, why=why, basis=basis,
                provenance={"method_id": "linear_motion_fit"})
        if px and px.get("parallax_arcsec") is not None:
            add("PARALLAX", f"sources[{subject_id}].parallax", {"parallax_arcsec": px["parallax_arcsec"], "distance_au": px.get("distance_au"),
                                                                "distance_interval_au": px.get("distance_interval_au"), "dof": px.get("dof")},
                cands=[c["candidate_id"]], quality=c["quality_grade"], uncertainty={"parallax_sigma_arcsec": px.get("parallax_sigma_arcsec")},
                admissible=ok, why=why, basis=basis, provenance={"method_id": "parallax_motion_fit"})
        nl = index.not_linked.get(subject_id)
        if nl and "exceeds the maximum" in nl:
            add("TRACKLET_RATE", f"linking.not_linked[{subject_id}]", {"reason": nl}, cands=[c["candidate_id"]], quality=c["quality_grade"], admissible=True,
                basis={"det_ids": sorted(src["det_ids"]), "obs_ids": sorted(src["obs_ids"]), "fit_id": f"{subject_id}:tracklet_rate"},
                relation="PHYSICAL_CONSTRAINT", provenance={"method_id": "tracklet_rate_bound"})
    elif src is not None and (src.get("parallax") or {}).get("parallax_arcsec") is not None:
        px = src["parallax"]  # a completed parallax fit exists without a motion candidate: still a measurement, gated by nothing
        add("PARALLAX", f"sources[{subject_id}].parallax", {"parallax_arcsec": px["parallax_arcsec"], "distance_au": px.get("distance_au"),
                                                            "distance_interval_au": px.get("distance_interval_au"), "dof": px.get("dof")},
            cands=[x["candidate_id"] for x in candidates], uncertainty={"parallax_sigma_arcsec": px.get("parallax_sigma_arcsec")},
            basis={"det_ids": sorted(src["det_ids"]), "obs_ids": sorted(src["obs_ids"]), "fit_id": f"{subject_id}:astrometric_fit"},
            provenance={"method_id": "parallax_motion_fit"})
    # transient sources can also carry a rate-bound exclusion (their motion candidate may not exist)
    if src is not None and not any(i.type == "TRACKLET_RATE" for i in items):
        nl = index.not_linked.get(subject_id)
        if nl and "exceeds the maximum" in nl:
            add("TRACKLET_RATE", f"linking.not_linked[{subject_id}]", {"reason": nl}, cands=[x["candidate_id"] for x in candidates],
                admissible=True, basis={"det_ids": sorted(src["det_ids"]), "obs_ids": sorted(src["obs_ids"]), "fit_id": f"{subject_id}:tracklet_rate"},
                relation="PHYSICAL_CONSTRAINT", provenance={"method_id": "tracklet_rate_bound"})
    # ---- linked motion (track) ----
    c = by_kind.get(("MOTION", "TRACK"))
    if c and trk is not None:
        ok, why = gate(c)
        px, diag = trk.get("parallax") or {}, trk.get("diagnostics") or {}
        basis = {"det_ids": sorted(trk["det_ids"]), "obs_ids": sorted(trk["obs_ids"]), "fit_id": f"{subject_id}:track_fit"}
        add("LINKED_MOTION", f"tracks[{subject_id}]",
            {"n_points": trk["n_points"], "epochs": trk["epoch_indices"], "effective_dof": diag.get("effective_dof"), "ambiguous": trk.get("ambiguous"),
             "absorbed_chains": diag.get("n_absorbed_chains"), "stationarity_sigma_equivalent": _sig(c["statistic"].get("significance"))},
            cands=[c["candidate_id"]], quality=c["quality_grade"], uncertainty={"astrometric_error_scale": c["statistic"].get("error_scale")},
            admissible=ok, why=why, basis=basis, provenance={"method_id": "link_moving"})
        if px.get("parallax_arcsec") is not None:
            add("PARALLAX", f"tracks[{subject_id}].parallax", {"parallax_arcsec": px["parallax_arcsec"], "distance_au": px.get("distance_au"),
                                                               "distance_interval_au": px.get("distance_interval_au"), "dof": px.get("dof")},
                cands=[c["candidate_id"]], quality=c["quality_grade"], uncertainty={"parallax_sigma_arcsec": px.get("parallax_sigma_arcsec")},
                admissible=ok, why=why, basis=basis, provenance={"method_id": "parallax_motion_fit"})
    # ---- spectra (descriptive only) ----
    if src is not None and src.get("spectrum"):
        sp = src["spectrum"]
        slope = sp.get("slope")
        pts = sp.get("points") or []
        add("SPECTRAL_SHAPE", f"sources[{subject_id}].spectrum",
            {"slope": slope["slope"] if slope else None, "slope_sigma": slope["slope_sigma"] if slope else None,
             "n_points": slope["n_points"] if slope else None, "detectors_measured": _wl_detector(sp.get("coverage") or []),
             "wavelength_sources": sp.get("wavelength_sources"), "meaning": "d log F_nu / d log lambda; descriptive, not a physical model"},
            cands=[x["candidate_id"] for x in candidates], admissible=slope is not None,
            why=[] if slope else ["no usable spectral slope"], basis={"det_ids": sorted({p.get("det_id") for p in pts if p.get("det_id")}),
                                                                       "obs_ids": sorted({p.get("obs_id") for p in pts if p.get("obs_id")})},
            provenance={"method_id": "spectral_slope_fit"}, na=None if slope else "no usable spectral slope")
        for comp in src.get("spectral_comparisons") or []:
            if comp.get("status") == "COMPLETED" and comp.get("significance"):
                add("SPECTRAL_CHANGE", f"sources[{subject_id}].spectral_comparisons[{comp['label']}]",
                    {"label": comp["label"], "sigma_equivalent": _sig(comp["significance"]), "n_pairs": comp["n_pairs"]},
                    cands=[x["candidate_id"] for x in candidates], admissible=True, basis={"depends_on": ["FLUX_CHANGE", "SINGLE_EPOCH_EXCESS"]},
                    provenance={"method_id": "spectral_comparison"})
    # ---- external identification (one item per record) ----
    for c in candidates:
        ctx = c.get("context") or {}
        cat = ctx.get("catalogue")
        if cat and not any(i.type == "CATALOGUE_STAR" for i in items):
            done = cat.get("status") in ("MATCH", "NO_MATCH")
            rid = f"gaia-dr3:{cat['gaia_source_id']}" if cat.get("status") == "MATCH" else f"gaia-dr3:cone:{subject_id}"
            add("CATALOGUE_STAR", f"candidates[{c['candidate_id']}].context.catalogue", dict(cat), cands=[x["candidate_id"] for x in candidates],
                admissible=done, why=[] if done else [cat.get("reason") or "catalogue query not completed"], relation="IDENTIFICATION" if cat.get("status") == "MATCH" else "NON_DETECTION",
                basis={"record_id": rid}, provenance={"service": "gaia_dr3_source@IRSA"}, na=None if done else (cat.get("reason") or "catalogue query not completed"))
        ko = ctx.get("known_object")
        if ko and not any(i.type == "KNOWN_SMALL_BODY" for i in items):
            st = ko.get("status")
            done = st in ("CONFIRMED_MATCH", "PROBABLE_MATCH", "NO_MATCH", "EXCLUDED_BY_MOTION")
            rel = "IDENTIFICATION" if st in ("CONFIRMED_MATCH", "PROBABLE_MATCH") else ("PHYSICAL_CONSTRAINT" if st == "EXCLUDED_BY_MOTION" else "NON_DETECTION")
            add("KNOWN_SMALL_BODY", f"candidates[{c['candidate_id']}].context.known_object", {k: v for k, v in ko.items() if k != "matches"},
                cands=[x["candidate_id"] for x in candidates], admissible=done, why=[] if done else [ko.get("reason") or "known-object query not completed"],
                relation=rel, basis={"record_id": f"jpl-sbident:{ko.get('name') or subject_id}"},
                provenance={"service": "jpl_sb_ident", "n_matches": len(ko.get("matches") or [])}, na=None if done else (ko.get("reason") or "known-object query not completed"))
    # ---- image-level error models (limitations; never merged with other evidence) ----
    for key, name, kinds in (("astrometric_error_model", "astrometric", ("MOTION",)), ("photometric_error_model", "photometric", ("VARIABILITY", "TRANSIENT"))):
        em = index.result.get(key)
        if em and any(c["kind"] in kinds for c in candidates):
            add("ERROR_MODEL", key, {"model": name, "status": em.get("status"), "scale": em.get("scale"), "n": em.get("n_sources") or em.get("n_series")},
                cands=[c["candidate_id"] for c in candidates if c["kind"] in kinds], uncertainty={"scale_se": em.get("scale_se")},
                admissible=em.get("status") == "COMPLETED", why=[] if em.get("status") == "COMPLETED" else [str(em.get("reason") or em.get("status"))],
                basis={"record_id": f"field:{key}"}, provenance={"method_id": key})
    return items


def dependency_groups(items: list[EvidenceItem]) -> tuple[list[dict], list[dict]]:
    """Independent evidence groups and the (unmerged) relations between items. Deterministic."""
    parent = {i.evidence_id: i.evidence_id for i in items}
    why: dict[str, set[str]] = {i.evidence_id: set() for i in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b, reason):
        ra, rb = find(a.evidence_id), find(b.evidence_id)
        why[a.evidence_id].add(reason)
        why[b.evidence_id].add(reason)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    relations = []
    for n, a in enumerate(items):
        for b in items[n + 1:]:
            if a.domain == "IMAGE_SYSTEMATIC" or b.domain == "IMAGE_SYSTEMATIC":
                other = b if a.domain == "IMAGE_SYSTEMATIC" else a
                if other.domain in ("FLUX", "POSITION") and a.domain != b.domain:
                    relations.append({"a": a.evidence_id, "b": b.evidence_id, "kind": "IMAGE_CORRELATED", "merged": False})
                continue
            ba, bb = a.basis, b.basis
            if ba.get("fit_id") and ba["fit_id"] == bb.get("fit_id"):
                union(a, b, "SHARED_FIT")
            elif ba.get("record_id") and ba["record_id"] == bb.get("record_id"):
                union(a, b, "SAME_RECORD")
            elif b.type in (ba.get("depends_on") or []) or a.type in (bb.get("depends_on") or []):
                union(a, b, "DECLARED_DEPENDENCY")
            elif set(ba.get("det_ids") or []) & set(bb.get("det_ids") or []):
                if a.domain == b.domain and a.domain in ("FLUX", "POSITION"):
                    union(a, b, "SHARED_DETECTIONS_SAME_DOMAIN")
                else:
                    relations.append({"a": a.evidence_id, "b": b.evidence_id, "kind": "SHARED_DETECTION_CROSS_DOMAIN", "merged": False})
    buckets: dict[str, list[str]] = {}
    for i in items:
        buckets.setdefault(find(i.evidence_id), []).append(i.evidence_id)
    groups = []
    for n, (root, ids) in enumerate(sorted(buckets.items(), key=lambda kv: min(kv[1]))):
        reasons = sorted(set().union(*(why[e] for e in ids))) if len(ids) > 1 else []
        groups.append({"group_id": f"G{n}", "evidence_ids": sorted(ids), "merged_because": reasons or ["INDEPENDENT"]})
    return groups, relations
