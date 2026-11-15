"""Validation matrix across the saved NASA target investigations (markdown), generated from the saved JSON results only."""

from __future__ import annotations

from .registry import NASA_TARGETS


def _fmt(x, nd=3):
    return "n/a" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def build_matrix(engine) -> str:
    lines = ["# NASA target validation matrix", "", "Generated from saved results by `python3 -m heimdall target matrix`. Every statement below is read from the saved "
             "investigation JSON; nothing here is hand-written. Test status: PASSED = analysis completed with a reported verdict (a non-detection is a valid verdict); "
             "LIMITED_BY_DATA = the observations could not support the test; FAILED/BLOCKED = the engine could not run it.", ""]
    for tid, spec in NASA_TARGETS.items():
        r = engine.target_result(tid, missing_ok=True)
        lines += [f"## {tid} {spec.name} ({spec.category})", ""]
        if r is None:
            lines += ["Not yet run.", ""]
            continue
        ref, cov, st = r["reference"], r.get("coverage", {}), r["status"]
        reason = st.get("reason") or "no reason recorded"
        reference_reason = f"; {ref['reason']}" if ref.get("reason") else ""
        sup = r.get("supersedes")
        if sup:
            vc = sup.get("verdict_change") or {}
            lines += [f"- Supersedes `{sup['investigation_id']}` (re-evaluated from the same saved measurements, no new data; changed: "
                      f"{', '.join(sup.get('changed_sections', []))}; {vc.get('from')} -> {vc.get('to')}). Reason: {sup.get('reason')}"]
        lines += [f"- Investigation: `{r['investigation_id']}`; execution **{st['execution']}**, test **{st['test_status']}** ({reason})",
                  f"- Identification: {ref.get('source_id')} ({ref.get('catalogue')}), status {ref['status']}{reference_reason}",
                  f"- Position at reference epoch {_fmt(ref.get('epoch_jyear'), 1)}: RA {_fmt(ref.get('ra_deg'), 6)} Dec {_fmt(ref.get('dec_deg'), 6)}; "
                  f"PM ({_fmt(ref.get('pmra_mas_yr'), 1)}, {_fmt(ref.get('pmdec_mas_yr'), 1)}) mas/yr [{ref.get('pm_source')}]",
                  f"- SPHEREx observations found {cov.get('n_discovered')}, selected {cov.get('n_selected')}, measured {cov.get('n_measured')}; "
                  f"MJD {_fmt(cov.get('mjd_min'), 1)}-{_fmt(cov.get('mjd_max'), 1)}; epochs {cov.get('n_epochs')}; releases {cov.get('per_release')}; detectors {cov.get('per_detector')}",
                  f"- Products retrieved: {len(r.get('products', []))} (origins {sorted({p['origin'] for p in r.get('products', [])})}); wavelengths measured (um): {cov.get('wavelengths_um')}",
                  f"- Quality flags in footprints: {cov.get('quality_flag_counts')}"]
        a = r.get("analyses", {})
        if "motion" in a:
            m = a["motion"]
            lines.append(f"- Motion: **{m.get('verdict')}**; usable {m.get('n_usable')}/{m.get('n_images')}, baseline {_fmt(m.get('baseline_days'), 1)} d; "
                         f"fitted v {m.get('fitted_velocity_arcsec_yr', {}).get('total')} vs catalogue {m.get('catalogue_velocity_arcsec_yr', {}).get('total')} arcsec/yr; "
                         f"control null: {m.get('control_null_test', {}).get('status')} {m.get('reasons') or ''}")
        if "variability" in a:
            v = a["variability"]
            lines.append(f"- Variability: **{v.get('verdict')}** ({v.get('verdict_reason')}); {v.get('n_points_usable')} usable points, {v.get('n_epochs')} epochs, "
                         f"{len(v.get('series', []))} comparable series; period: {v.get('period_assessment', {}).get('status')}; caveats: {v.get('caveats')}")
        b = r.get("blind_pipeline", {})
        lines.append(f"- Blind pipeline: {b.get('status')} run `{b.get('run_id')}`; detections near expected target {b.get('blind_detection_near_expected_target', {}).get('n_with_blind_detection')}"
                     f"/{b.get('blind_detection_near_expected_target', {}).get('n_images_covered')}")
        lines.append(f"- Detectability: {r.get('detectability', {}).get('forced_snr')}")
        hypotheses = "; ".join(f"{h['title']} [{h['status']}]" for h in r.get("hypotheses", [])) or "not run"
        lines.append("- Hypotheses: " + hypotheses)
        from .claims import build_claims
        cl = build_claims(r)
        lines += [f"- Claims ({cl['method_version']}): " + "; ".join(f"{c['analysis']} {c['claim_type']}" + (f" ({c['strength']})" if c.get("strength", "").startswith("PROVISIONAL") else "")
                                                         for c in cl["claims"]),
                  f"- Quick answer (from the admissible claims): {cl['quick_answer']}",
                  f"- Saved conclusion text (not authoritative): {r['conclusion']['quick_answer']}", f"- Reproduce: `{r.get('reproduce')}`", ""]
    return "\n".join(lines)
