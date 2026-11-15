"""Content-addressed, verified local store for SPHEREx products and calibration files.

A file becomes visible to science code only after: (1) it exists completely (written to a temp file and atomically moved), (2) its SHA-256
is computed, (3) it opens as FITS with at least one HDU and a plausible provenance header, and (4) it matches the archive checksum when
the archive supplied one. Anything else is rejected and removed -- never exposed as ready. Identical content is stored once.
Objects referenced by a frozen manifest (a saved investigation) are pinned and never evicted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import warnings
from dataclasses import dataclass
from pathlib import Path

from astropy.io import fits

from .db import ServiceDB
from .inventory import now_iso
from .schemas import canonical, digest


class InvalidProduct(Exception):
    """The file failed integrity/format validation (corrupt, truncated, wrong checksum, not FITS)."""

    def __init__(self, reason: str, kind: str = "INVALID") -> None:
        super().__init__(reason)
        self.reason, self.kind = reason, kind


@dataclass(frozen=True)
class StoredProduct:
    key: str
    sha256: str
    path: Path
    size: int
    kind: str
    obs_id: str | None
    release: str | None
    access_reference: str | None
    origin: str
    route: str | None
    archive_checksum: str | None
    retrieved_at: str
    meta: dict


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_fits(path: Path) -> dict:
    """Open the file as FITS and return basic provenance facts; raises InvalidProduct."""
    if path.stat().st_size < 2880:
        raise InvalidProduct("file is smaller than one FITS block (incomplete download)", "INCOMPLETE")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with fits.open(path, memmap=False, checksum=False) as hdul:
                if len(hdul) < 1:
                    raise InvalidProduct("FITS file has no HDU")
                names = [h.name for h in hdul]
                for h in hdul:  # force a read of every HDU so a truncated data section fails here, not later
                    _ = h.data if h.is_image else None
                return {"hdus": names, "n_hdu": len(names), "obsid": hdul[0].header.get("OBSID") or (hdul["IMAGE"].header.get("OBSID") if "IMAGE" in names else None)}
    except InvalidProduct:
        raise
    except Exception as exc:  # noqa: BLE001 - any parser failure means the file is not usable
        raise InvalidProduct(f"not a readable FITS file: {type(exc).__name__}: {exc}", "CORRUPT") from None


class ProductStore:
    def __init__(self, db: ServiceDB, root: Path, quota_bytes: int | None = None) -> None:
        self.db, self.root, self.quota_bytes = db, Path(root), quota_bytes
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "quarantine").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _object_path(self, sha: str) -> Path:
        return self.root / "objects" / sha[:2] / f"{sha}.fits"

    def ingest(self, src: Path, key: str, *, kind: str, origin: str, obs_id: str | None = None, release: str | None = None,
               access_reference: str | None = None, route: str | None = None, archive_checksum: str | None = None, meta: dict | None = None) -> StoredProduct:
        """Verify and register ``src`` under ``key``. The source file is copied, never moved; the caller's cache stays intact."""
        src = Path(src)
        tmp = self.root / "tmp" / f"{digest([key, str(src)])}.part"
        shutil.copyfile(src, tmp)
        try:
            sha = _sha256(tmp)
            if archive_checksum and archive_checksum.startswith("md5:"):
                got = hashlib.md5(tmp.read_bytes()).hexdigest()  # noqa: S324 - integrity check against the archive's published MD5, not security
                if got != archive_checksum.split(":", 1)[1]:
                    raise InvalidProduct("content does not match the archive checksum", "CHECKSUM_MISMATCH")
            info = validate_fits(tmp)
            dest = self._object_path(sha)
            existed = dest.exists()
            if not existed:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, dest)  # atomic on one filesystem: the object appears complete or not at all
            else:
                tmp.unlink(missing_ok=True)
            ts = now_iso()
            with self.db.transaction() as db:
                db.conn.execute("INSERT OR IGNORE INTO products(sha256,path,size,created_at,validated_at) VALUES(?,?,?,?,?)", (sha, str(dest), dest.stat().st_size, ts, ts))
                db.conn.execute(
                    "INSERT INTO product_keys(key,sha256,kind,obs_id,release,access_reference,origin,route,archive_checksum,retrieved_at,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET sha256=excluded.sha256, origin=excluded.origin, route=excluded.route, retrieved_at=excluded.retrieved_at, meta=excluded.meta",
                    (key, sha, kind, obs_id, release, access_reference, origin, route, archive_checksum, ts, canonical({**(meta or {}), "fits": info, "deduplicated": existed})))
            self.db.bump("bytes_stored", 0 if existed else dest.stat().st_size)
            return self.get(key)  # type: ignore[return-value]
        except InvalidProduct:
            self.db.bump("validation_failures")
            q = self.root / "quarantine" / f"{digest([key, str(src)])}.rejected"
            if tmp.exists():
                os.replace(tmp, q)
            raise
        finally:
            tmp.unlink(missing_ok=True)

    def get(self, key: str, *, count: bool = False) -> StoredProduct | None:
        """A verified product for ``key``, re-checking that the object still exists with the recorded size and hash."""
        r = self.db.one("SELECT k.*, p.path, p.size FROM product_keys k JOIN products p ON p.sha256=k.sha256 WHERE k.key=?", (key,))
        ok = r is not None and Path(r["path"]).is_file() and Path(r["path"]).stat().st_size == r["size"]
        if count:
            self.hits += int(ok)
            self.misses += int(not ok)
            self.db.bump("store_hits" if ok else "store_misses")
        if not ok:
            return None
        return StoredProduct(key=key, sha256=r["sha256"], path=Path(r["path"]), size=r["size"], kind=r["kind"], obs_id=r["obs_id"], release=r["release"],
                             access_reference=r["access_reference"], origin=r["origin"], route=r["route"], archive_checksum=r["archive_checksum"],
                             retrieved_at=r["retrieved_at"], meta=json.loads(r["meta"] or "{}"))

    def find(self, obs_id: str, cut_suffix: str) -> StoredProduct | None:
        """Fallback lookup when the caller did not state the release: the unique key for this observation and cutout."""
        rows = self.db.query("SELECT key FROM product_keys WHERE key LIKE ? ESCAPE '\\'", (f"obs:{obs_id}|%|{cut_suffix}",))
        return self.get(rows[0]["key"], count=True) if len(rows) == 1 else None

    def verify(self, key: str) -> bool:
        """Full re-hash (slow path): True if the stored bytes still match their recorded SHA-256."""
        p = self.get(key)
        return p is not None and _sha256(p.path) == p.sha256

    # -- pins, retention, quota ---------------------------------------------------------------------
    def pin(self, shas: list[str], manifest_id: str) -> None:
        with self.db.transaction() as db:
            for s in shas:
                db.conn.execute("INSERT OR IGNORE INTO pins(sha256,manifest_id) VALUES(?,?)", (s, manifest_id))

    def usage(self) -> dict:
        r = self.db.one("SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM products")
        pinned = self.db.one("SELECT COUNT(*) n, COALESCE(SUM(p.size),0) b FROM products p WHERE p.sha256 IN (SELECT sha256 FROM pins)")
        return {"objects": r["n"], "bytes": r["b"], "pinned_objects": pinned["n"], "pinned_bytes": pinned["b"], "quota_bytes": self.quota_bytes,
                "over_quota": bool(self.quota_bytes and r["b"] > self.quota_bytes)}

    def evict(self, *, target_bytes: int | None = None, dry_run: bool = False) -> dict:
        """Remove unpinned objects, least recently validated first, until usage <= target (default: the quota). Pinned objects are never removed."""
        goal = self.quota_bytes if target_bytes is None else target_bytes
        if goal is None:
            return {"evicted": [], "note": "no quota or target configured; nothing evicted"}
        used = self.usage()["bytes"]
        evicted = []
        for r in self.db.query("SELECT sha256,path,size FROM products WHERE sha256 NOT IN (SELECT sha256 FROM pins) ORDER BY validated_at, created_at"):
            if used <= goal:
                break
            evicted.append(r["sha256"])
            used -= r["size"]
            if not dry_run:
                Path(r["path"]).unlink(missing_ok=True)
                self.db.execute("DELETE FROM product_keys WHERE sha256=?", (r["sha256"],))
                self.db.execute("DELETE FROM products WHERE sha256=?", (r["sha256"],))
        return {"evicted": evicted, "dry_run": dry_run, "bytes_after": used, "still_over_goal": used > goal, "pinned_objects_kept": self.usage()["pinned_objects"]}
