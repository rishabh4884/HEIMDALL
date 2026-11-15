"""Portable evidence bundles (``heimdall.evidence_bundle/1.0``): export saved results with the exact inputs they used, verify, and import
into a fresh state directory for offline replay.

A bundle is a directory (optionally packed as ``.tar.gz``) containing:

* ``bundle.json`` -- id, creation time, software version and source commit, every file with SHA-256, size and role, a contents summary,
  and the data sources with their redistribution terms;
* ``state/targets/**`` -- every saved target investigation version and supersession log;
* ``state/runs/<run_id>/**`` -- blind-pipeline runs referenced by the data service (and any requested);
* ``state/dataservice/service.sqlite3`` -- a consistent snapshot (SQLite backup API) of the inventory, manifests, jobs and result index;
* ``state/dataservice/store/objects/**`` -- the verified SPHEREx products and calibrations pinned by the frozen manifests (content-addressed);
* ``cache/context/**`` -- cached catalogue answers (Gaia, CatWISE, JPL) so reference astrometry resolves offline.

Import verifies every file before writing anything; a missing or corrupted file aborts with a precise list and leaves the destination
untouched. After import, product rows whose objects are not in the bundle are removed (the store never claims data it does not hold),
open jobs from the source system are cancelled (a snapshot is replayed, not resumed), and an import record is written. Large FITS
collections belong in bundles kept outside Git; secrets are never included (settings and ``.env`` are not exported).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .contracts import ErrorCode, HeimdallError

BUNDLE_SCHEMA = "heimdall.evidence_bundle/1.0"
DATA_SOURCES = [
    {"name": "SPHEREx spectral images and ePSF calibration", "provider": "NASA/IPAC Infrared Science Archive (IRSA)",
     "terms": "public NASA mission data; IRSA asks users to acknowledge IRSA and the SPHEREx mission in publications",
     "redistribution": "permitted for these public products with acknowledgement; confirm the current IRSA/SPHEREx data policy before public posting"},
    {"name": "Gaia DR3 query results (cached)", "provider": "ESA Gaia archive / IRSA mirror",
     "terms": "ESA Gaia data are freely available; acknowledgement of ESA/Gaia/DPAC is required",
     "redistribution": "small cached query results for reproduction; confirm the ESA Gaia terms before public posting"},
    {"name": "CatWISE2020 / JPL small-body answers (cached)", "provider": "IRSA / JPL SSD",
     "terms": "public catalogue / service results; acknowledgement requested", "redistribution": "small cached answers for reproduction"},
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, cwd=Path(__file__).resolve().parent)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def export_bundle(state_dir: Path, cache_dir: Path, out: Path, *, runs: list[str] | None = None, archive: bool = False) -> dict:
    state_dir, cache_dir, out = Path(state_dir), Path(cache_dir), Path(out)
    if out.exists() and any(out.iterdir()):
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"bundle directory {out} is not empty")
    db_path = state_dir / "dataservice" / "service.sqlite3"
    if not db_path.is_file():
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"no data-service database at {db_path}; nothing to export")
    out.mkdir(parents=True, exist_ok=True)
    files: list[dict] = []

    def add(src: Path, rel: str, role: str) -> None:
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        files.append({"path": rel, "sha256": _sha256(dest), "size": dest.stat().st_size, "role": role})

    # consistent database snapshot
    snap = out / "state" / "dataservice" / "service.sqlite3"
    snap.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(snap))
    try:
        src.backup(dst)
        conn = dst
        manifests = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT manifest_id, body FROM manifests")}
        frun = [r[0] for r in conn.execute("SELECT investigation_id FROM investigation_results WHERE key LIKE 'frun:%'")]
    finally:
        dst.close()
        src.close()
    files.append({"path": "state/dataservice/service.sqlite3", "sha256": _sha256(snap), "size": snap.stat().st_size, "role": "database"})
    shas = sorted({p["sha256"] for m in manifests.values() for p in m.get("products", []) + m.get("calibrations", [])})
    missing_objects = []
    for sha in shas:
        obj = state_dir / "dataservice" / "store" / "objects" / sha[:2] / f"{sha}.fits"
        if obj.is_file():
            add(obj, f"state/dataservice/store/objects/{sha[:2]}/{sha}.fits", "product")
        else:
            missing_objects.append(sha)
    for p in sorted((state_dir / "targets").rglob("*")) if (state_dir / "targets").is_dir() else []:
        if p.is_file() and p.suffix in (".json", ".jsonl"):
            add(p, f"state/{p.relative_to(state_dir)}", "target_result")
    run_ids = sorted(set(frun) | set(runs or []))
    for rid in run_ids:
        d = state_dir / "runs" / rid
        for p in sorted(d.rglob("*")) if d.is_dir() else []:
            if p.is_file():
                add(p, f"state/{p.relative_to(state_dir)}", "run")
    ctx = cache_dir / "context"
    for p in sorted(ctx.rglob("*")) if ctx.is_dir() else []:
        if p.is_file():
            add(p, f"cache/{p.relative_to(cache_dir)}", "context_cache")
    body = {"schema": BUNDLE_SCHEMA, "bundle_id": None, "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "software_version": __version__, "source_commit": _commit(),
            "contents": {"manifests": sorted(manifests), "products": len(shas) - len(missing_objects), "products_missing_at_export": missing_objects,
                         "target_files": sum(f["role"] == "target_result" for f in files), "runs": run_ids,
                         "context_cache_files": sum(f["role"] == "context_cache" for f in files), "total_bytes": sum(f["size"] for f in files)},
            "data_sources": DATA_SOURCES, "excluded": ["settings and .env (secrets)", "unpinned store objects", "the legacy adapter cache"],
            "files": sorted(files, key=lambda f: f["path"])}
    body["bundle_id"] = "evb:" + hashlib.sha256(json.dumps(body["files"], sort_keys=True).encode()).hexdigest()[:16]
    (out / "bundle.json").write_text(json.dumps(body, indent=1, sort_keys=True), encoding="utf-8")
    result = {"bundle_id": body["bundle_id"], "path": str(out), "files": len(files), "bytes": body["contents"]["total_bytes"],
              "products": body["contents"]["products"], "manifests": body["contents"]["manifests"], "runs": run_ids,
              "products_missing_at_export": missing_objects}
    if archive:
        tar = out.with_suffix(".tar.gz")
        with tarfile.open(tar, "w:gz") as tf:
            tf.add(out, arcname=out.name)
        result["archive"] = str(tar)
    return result


def _open(bundle: Path) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    bundle = Path(bundle)
    if bundle.is_dir():
        return bundle, None
    if bundle.is_file() and bundle.name.endswith((".tar.gz", ".tgz")):
        tmp = tempfile.TemporaryDirectory()
        with tarfile.open(bundle, "r:gz") as tf:
            tf.extractall(tmp.name, filter="data")  # rejects absolute paths, links outside the tree and special files
        roots = [p for p in Path(tmp.name).iterdir() if p.is_dir()]
        if len(roots) != 1:
            tmp.cleanup()
            raise HeimdallError.of(ErrorCode.DATA_MALFORMED, "archive must contain exactly one bundle directory")
        return roots[0], tmp
    raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"no evidence bundle at {bundle}")


def verify_bundle(bundle: Path) -> dict:
    """Check every listed file (presence, size, SHA-256) and the bundle id. Never raises for content problems; reports them."""
    root, tmp = _open(bundle)
    try:
        try:
            body = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "problems": [f"bundle.json unreadable: {type(exc).__name__}"], "bundle_id": None}
        problems = []
        if body.get("schema") != BUNDLE_SCHEMA:
            problems.append(f"unsupported schema {body.get('schema')!r}")
        for f in body.get("files", []):
            p = root / f["path"]
            if ".." in Path(f["path"]).parts or Path(f["path"]).is_absolute():
                problems.append(f"unsafe path {f['path']}")
            elif not p.is_file():
                problems.append(f"missing {f['path']}")
            elif p.stat().st_size != f["size"] or _sha256(p) != f["sha256"]:
                problems.append(f"checksum mismatch {f['path']}")
        expect = "evb:" + hashlib.sha256(json.dumps(body.get("files", []), sort_keys=True).encode()).hexdigest()[:16]
        if body.get("bundle_id") != expect:
            problems.append("bundle id does not match its file list")
        return {"ok": not problems, "problems": problems, "bundle_id": body.get("bundle_id"), "files": len(body.get("files", [])),
                "contents": body.get("contents"), "source_commit": body.get("source_commit")}
    finally:
        if tmp is not None:
            tmp.cleanup()


def import_bundle(bundle: Path, state_dir: Path, cache_dir: Path, *, force: bool = False) -> dict:
    state_dir, cache_dir = Path(state_dir).resolve(), Path(cache_dir).resolve()
    if not force and state_dir.exists() and any(state_dir.iterdir()):
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"destination state directory {state_dir} is not empty (use force to merge)")
    root, tmp = _open(bundle)
    try:
        check = verify_bundle(root)
        if not check["ok"]:
            raise HeimdallError.of(ErrorCode.DATA_MALFORMED, "evidence bundle failed verification; nothing was imported: " + "; ".join(check["problems"][:10]),
                                   problems=str(len(check["problems"])))
        body = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
        for f in body["files"]:
            rel = Path(f["path"])
            dest = (state_dir / rel.relative_to("state")) if rel.parts[0] == "state" else (cache_dir / rel.relative_to("cache"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, dest)
        db = sqlite3.connect(str(state_dir / "dataservice" / "service.sqlite3"))
        try:
            store = state_dir / "dataservice" / "store" / "objects"
            present = {p.stem for p in store.rglob("*.fits")}
            rows = db.execute("SELECT sha256 FROM products").fetchall()
            gone = [r[0] for r in rows if r[0] not in present]
            for sha in gone:
                db.execute("DELETE FROM pins WHERE sha256=?", (sha,))
                db.execute("DELETE FROM product_keys WHERE sha256=?", (sha,))
                db.execute("DELETE FROM products WHERE sha256=?", (sha,))
            for sha in present:
                db.execute("UPDATE products SET path=? WHERE sha256=?", (str(store / sha[:2] / f"{sha}.fits"), sha))
            for key, saved in db.execute("SELECT key, saved_to FROM investigation_results").fetchall():
                for marker in ("/targets/", "/runs/"):
                    norm = "/" + str(saved).replace("\\", "/").lstrip("/")
                    if marker in norm:
                        db.execute("UPDATE investigation_results SET saved_to=? WHERE key=?",
                                   (str(state_dir / marker.strip("/") / norm.split(marker, 1)[1]), key))
                        break
            open_states = ("QUEUED", "DISCOVERING", "DOWNLOADING", "VALIDATING", "PREPARING_DATA", "UNAVAILABLE_AFTER_DEADLINE")
            now = datetime.now(UTC).isoformat(timespec="seconds")
            cancelled = [r[0] for r in db.execute(f"SELECT job_id FROM jobs WHERE state IN ({','.join('?' * len(open_states))})", open_states)]
            for jid in cancelled:
                db.execute("UPDATE jobs SET state='CANCELLED', updated_at=?, reason=? WHERE job_id=?", (now, "imported evidence snapshot: open jobs are not resumed", jid))
                db.execute("INSERT INTO job_events(job_id,at,from_state,to_state,reason) VALUES(?,?,?,?,?)",
                           (jid, now, None, "CANCELLED", "imported evidence snapshot: open jobs are not resumed"))
            db.commit()
        finally:
            db.close()
        record = {"bundle_id": body["bundle_id"], "imported_at": datetime.now(UTC).isoformat(timespec="seconds"), "source_commit": body.get("source_commit"),
                  "files": len(body["files"]), "products": len(present), "product_rows_removed_absent_objects": len(gone),
                  "open_jobs_cancelled": cancelled, "data_sources": body.get("data_sources"),
                  "note": "replay of saved evidence: results served from this state are stored results unless recomputed"}
        (state_dir / "evidence_import.json").write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
        return record
    finally:
        if tmp is not None:
            tmp.cleanup()
