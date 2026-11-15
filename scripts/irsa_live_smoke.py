"""Live IRSA end-to-end smoke check through the final adapter (network; bounded; writes an evidence record).

discover (SIA2 preferred, TAP fallback) -> retrieve one real DR1/QR3 cutout -> ePSF calibration -> ingest -> ONE forced PSF measurement
at the *supplied sky position*. That measurement is not associated with any catalogue source and is NOT a detection or a target result;
it only proves the access + ingestion + measurement chain on a genuine SPHEREx image.

    python3 scripts/irsa_live_smoke.py [--ra 269.4485 --dec 4.7394 --detector 2 --attempts 6] [--out PATH]
Exit 0 only if a real image was retrieved, ingested and measured; 1 otherwise (the record then says which step failed and why).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heimdall.api import Engine  # noqa: E402
from heimdall.contracts import HeimdallError, ResultOutcome  # noqa: E402
from heimdall.data_access import DiscoveryQuery, RetrievalRequest, SkyRegion  # noqa: E402
from heimdall.engine.config import EngineConfig  # noqa: E402
from heimdall.engine.detection import forced_photometry, prepare  # noqa: E402
from heimdall.ingest.spherex import read_spherex_mef  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ra", type=float, default=269.4485)
    ap.add_argument("--dec", type=float, default=4.7394)
    ap.add_argument("--detector", default="2")
    ap.add_argument("--attempts", type=int, default=6, help="bounded discovery attempts (spaced >= 20 s, and past any circuit cooldown)")
    ap.add_argument("--cutout-size", type=float, default=0.05)
    ap.add_argument("--out", type=Path, default=Path(".heimdall/verification/2026-11-15-irsa/live_smoke.json"))
    args = ap.parse_args()
    rec: dict = {"started_at": datetime.now(UTC).isoformat(timespec="seconds"), "position_deg": [args.ra, args.dec],
                 "label": "forced measurement at an UNASSOCIATED sky position; not a detection and not a target result",
                 "steps": [], "status": "FAILED"}

    def done(code: int) -> int:
        rec["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rec, indent=1, sort_keys=True, default=str))
        print(json.dumps({k: rec[k] for k in ("status", "reason") if k in rec}), f"-> {args.out}")
        return code

    engine = Engine.from_environment(None, None)
    src = engine.source("irsa-spherex")
    region = SkyRegion(ra_deg=args.ra, dec_deg=args.dec, radius_deg=0.02)
    disc = None
    for attempt in range(1, args.attempts + 1):
        try:
            disc = src.discover(DiscoveryQuery(region=region, filters={"detectors": args.detector}))
            rec["steps"].append({"step": "discover", "attempt": attempt, "outcome": disc.outcome.value, "n": len(disc.observations),
                                 "route": src.last_discovery, "origin": disc.provenance.origin.value})
            break
        except HeimdallError as exc:
            rec["steps"].append({"step": "discover", "attempt": attempt, "error": exc.code.value, "message": exc.info.message,
                                 "route": src.last_discovery})
            if attempt < args.attempts:
                # wait out an open circuit (bounded) rather than burning attempts on skipped routes
                time.sleep(min(130.0, max(20.0, min(b.retry_in_s() for b in src.breakers.values()) + 1.0)))
    if disc is None:
        rec["reason"] = "discovery failed on every route within the attempt budget (service failure, NOT 'no observations')"
        return done(1)
    if disc.outcome is ResultOutcome.NO_OBSERVATIONS or not disc.observations:
        rec["reason"] = "the archive answered validly and lists no image at this position"
        return done(1)
    obs = sorted(disc.observations, key=lambda o: o.obs_id)[len(disc.observations) // 2]
    prod = obs.provenance.products[0]
    cutout = args.cutout_size
    reg = SkyRegion(ra_deg=args.ra, dec_deg=args.dec, radius_deg=cutout / 2)
    try:
        rr = src.retrieve(RetrievalRequest(obs_id=obs.obs_id, region=reg, options={
            "access_reference": prod.access_reference, "release": prod.release or "", "cutout_size_deg": repr(cutout)}))
        det = int(obs.metadata["detector"].value)
        epsf, cprod = src.retrieve_calibration("epsf", det)
    except HeimdallError as exc:
        rec["reason"] = f"retrieval failed: {exc.code.value}: {exc.info.message}"
        return done(1)
    p = rr.products[0]
    rec["image"] = {"obs_id": obs.obs_id, "release": prod.release, "access_reference": prod.access_reference,
                    "retrieved_at": rr.provenance.retrieved_at.isoformat(), "origin": rr.provenance.origin.value,
                    "sha256": p.product.checksum, "bytes": Path(p.location).stat().st_size, "cutout_size_deg": cutout,
                    "discovery_route": (src.last_discovery or {}).get("route_used"),
                    "calibration": {"product": cprod.product_id, "sha256": cprod.checksum, "access_reference": cprod.access_reference}}
    cfg = EngineConfig()
    ing = read_spherex_mef(p.location, source_id=src.source_id, provenance=rr.provenance, config=cfg, release=prod.release,
                           obs_id=obs.obs_id, crop=(args.ra, args.dec, cutout / 2), epsf_calibration=epsf)
    if ing.image is None:
        rec["reason"] = f"ingestion excluded the image: {ing.qc.exclusion_reasons}"
        return done(1)
    meta = ing.image.meta
    rec["ingest"] = {"shape": list(ing.image.data.shape) if hasattr(ing.image, "data") else None, "mjd_avg": meta.mjd_avg,
                     "detector": meta.detector, "pipeline_version": meta.pipeline_version, "pixel_scale_arcsec": meta.pixel_scale_arcsec,
                     "qc_exclusions": ing.qc.exclusion_reasons}
    fm = forced_photometry(prepare(ing.image, cfg), args.ra, args.dec, cfg)
    rec["forced_measurement"] = {k: getattr(fm, k) for k in ("status", "reasons", "x", "y", "flux_ujy", "flux_sigma_ujy", "snr", "chi2_red",
                                                          "peak_flags", "masked_fraction")}
    rec["status"] = "OK" if fm.status == "MEASURED" else "INGESTED_BUT_NOT_MEASURED"
    if fm.status != "MEASURED":
        rec["reason"] = f"forced measurement status {fm.status}: {fm.reasons}"
    return done(0 if fm.status == "MEASURED" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
