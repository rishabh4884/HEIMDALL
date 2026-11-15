"""Read-only IRSA DR1 HiPS display imagery and a bounded local cache."""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, build_opener

from .data_access.http import _ssl_context

ROOT = "https://irsa.ipac.caltech.edu/data/hips/SPHEREx/spherex_allsky_cube"
PREFIX = "/sky/spherex/"
PNG = b"\x89PNG\r\n\x1a\n"
MAX_RESPONSE = 8 * 1024**2
MAX_CACHE = 192 * 1024**2


def location(path: str) -> tuple[str, str] | None:
    """Resolve only the published image service's finite set of legal paths."""
    if path == PREFIX + "properties":
        return "properties", "text/plain; charset=utf-8"
    match = re.fullmatch(r"/sky/spherex/(allsky|[0-5]/[0-9]{1,5})/([0-9]{1,3})\.png", path)
    if not match:
        return None
    value, frame = match.groups()
    index = int(frame)
    if index >= 102:
        return None
    suffix = f"_{index}" if index else ""
    if value == "allsky":
        return f"Norder3/Allsky{suffix}.png", "image/png"
    order, tile = map(int, value.split("/"))
    if tile >= 12 * 4**order:
        return None
    return f"Norder{order}/Dir{tile // 10000 * 10000}/Npix{tile}{suffix}.png", "image/png"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HiPSCache:
    def __init__(self, root: Path, offline: bool) -> None:
        self.root = root / "spherex-dr1-hips"
        self.offline = offline
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=_ssl_context()))
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(4)
        self.unavailable_until = 0.0

    @staticmethod
    def valid(data: bytes, mime: str) -> bool:
        if not data or len(data) > MAX_RESPONSE:
            return False
        if mime == "image/png":
            return data.startswith(PNG) and len(data) > 33 and all(0 < int.from_bytes(data[i:i + 4], "big") <= 4096 for i in (16, 20))
        return b"SPHEREx DR1" in data and b"data_cube_crval3_arr" in data

    def get(self, path: str) -> tuple[int, bytes, str]:
        loc = location(path)
        if loc is None:
            return 404, b"Unknown SPHEREx image resource.", "text/plain"
        relative, mime = loc
        cached = self.root / relative
        try:
            with cached.open("rb") as handle:
                data = handle.read(MAX_RESPONSE + 1)
            if self.valid(data, mime):
                return 200, data, mime
        except OSError:
            pass
        if self.offline:
            return 404, b"This SPHEREx image is not cached locally (offline mode).", "text/plain"
        if time.monotonic() < self.unavailable_until or not self.slots.acquire(blocking=False):
            return 503, b"SPHEREx image access is temporarily unavailable.", "text/plain"
        try:
            with self.opener.open(f"{ROOT}/{relative}", timeout=6) as response:
                data = response.read(MAX_RESPONSE + 1)
            if not self.valid(data, mime):
                raise ValueError("invalid HiPS response")
        except HTTPError as exc:
            with exc:
                if exc.code == 404:
                    return 404, b"Image tile not available from the archive.", "text/plain"
            self.unavailable_until = time.monotonic() + 10
            return 503, b"SPHEREx image access is temporarily unavailable.", "text/plain"
        except (OSError, URLError, ValueError):
            self.unavailable_until = time.monotonic() + 10
            return 503, b"SPHEREx image access is temporarily unavailable.", "text/plain"
        finally:
            self.slots.release()
        try:
            self._store(cached, data)
        except OSError:
            pass  # a cache write failure does not prevent use of the fetched image
        return 200, data, mime

    def _store(self, path: Path, data: bytes) -> None:
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".partial")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
            files = []
            for item in self.root.rglob("*.png"):
                stat = item.stat()
                files.append((stat.st_mtime, stat.st_size, item))
            size = sum(row[1] for row in files)
            # Keep the default and other overview atlases for offline starts.
            for _, length, item in sorted(files, key=lambda row: ("Allsky" in row[2].name, row[0])):
                if size <= MAX_CACHE:
                    break
                item.unlink(missing_ok=True)
                size -= length
