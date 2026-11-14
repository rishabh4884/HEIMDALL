"""Content-verified local cache for retrieved products.

Entries are keyed by the SHA-256 of the request URL (which includes the
release path and cutout parameters, so a new release or a different cutout is
a different entry). Each entry stores the bytes and a JSON sidecar with the
SHA-256 of the bytes; a mismatch on read (corruption, partial write) deletes
the entry so it is fetched again. Writes are atomic (temporary file + rename).
URLs stored in sidecars must not contain credentials (the archive is public).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class CacheEntry:
    path: Path
    sha256: str
    size: int
    fetched_at: str
    url: str
    meta: dict


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ProductCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.corrupted_removed = 0

    @staticmethod
    def key_for(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.root / key[:2] / f"{key}.bin", self.root / key[:2] / f"{key}.json"

    def get(self, url: str) -> CacheEntry | None:
        data_path, meta_path = self._paths(self.key_for(url))
        if not (data_path.is_file() and meta_path.is_file()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if meta.get("url") != url or meta.get("sha256") != sha256_file(data_path):
            for p in (data_path, meta_path):
                p.unlink(missing_ok=True)
            self.corrupted_removed += 1
            return None
        return CacheEntry(path=data_path, sha256=meta["sha256"], size=int(meta.get("size", 0)),
                          fetched_at=meta.get("fetched_at", ""), url=url, meta=meta.get("meta", {}))

    def put(self, url: str, data: bytes, meta: dict | None = None) -> CacheEntry:
        data_path, meta_path = self._paths(self.key_for(url))
        data_path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        tmp = data_path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, data_path)
        record = {"url": url, "sha256": digest, "size": len(data), "fetched_at": datetime.now(UTC).isoformat(), "meta": meta or {}}
        tmp_meta = meta_path.with_suffix(f".tmp{os.getpid()}")
        tmp_meta.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp_meta, meta_path)
        return CacheEntry(path=data_path, sha256=digest, size=len(data), fetched_at=record["fetched_at"], url=url, meta=meta or {})

    def entries(self) -> list[dict]:
        out = []
        for meta_path in sorted(self.root.glob("*/*.json")):
            try:
                record = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({k: record.get(k) for k in ("url", "sha256", "size", "fetched_at")})
        return out

    def invalidate(self, *, older_than_days: float | None = None, url_contains: str | None = None) -> int:
        removed = 0
        now = time.time()
        for meta_path in sorted(self.root.glob("*/*.json")):
            data_path = meta_path.with_suffix(".bin")
            try:
                record = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = {}
            if url_contains is not None and url_contains not in str(record.get("url", "")):
                continue
            if older_than_days is not None and now - meta_path.stat().st_mtime < older_than_days * 86400:
                continue
            for p in (data_path, meta_path):
                p.unlink(missing_ok=True)
            removed += 1
        return removed
