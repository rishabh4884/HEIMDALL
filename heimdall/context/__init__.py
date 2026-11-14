"""External astronomical context: Gaia DR3 stars (IRSA TAP) and known small bodies (JPL SB Identification API).

Query states, never conflated:
- ``COMPLETED``: the service answered. "No match" is then a real result within the stated criteria.
- ``FAILED``: the request failed (network, HTTP, malformed response). Nothing is concluded.
- ``OFFLINE_NOT_CACHED``: offline mode and no cached answer.
- ``NOT_APPLICABLE``: for example synthetic data, which no real catalogue describes.
- ``NOT_REQUESTED``: context disabled for the run.

Per-subject match states: ``CONFIRMED_MATCH`` (consistent in at least two images),
``PROBABLE_MATCH`` (one image), ``NO_MATCH`` (every relevant query completed, nothing within
tolerance) and ``NOT_ASSESSABLE`` (a relevant query did not complete).
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..contracts.outcomes import ErrorCode, HeimdallError
from ..data_access.cache import ProductCache
from ..data_access.http import HttpClient, Transport


@dataclass
class ServiceAnswer:
    status: str  # COMPLETED | FAILED | OFFLINE_NOT_CACHED
    body: bytes | None
    from_cache: bool
    reason: str = ""
    error_code: str | None = None


class CachedService:
    """GET/POST with the shared HTTP client, a response cache and a minimum interval between live calls."""

    def __init__(self, name: str, settings: Settings, transport: Transport | None = None, sleep=time.sleep,
                 min_interval_s: float = 1.0) -> None:
        self.name = name
        self.settings = settings
        self.http = HttpClient(name, settings.request_timeout_s, settings.max_retries, transport, sleep,
                               offline=settings.offline)
        self.cache = ProductCache(Path(settings.cache_dir) / "context" / name)
        self.sleep = sleep
        self.min_interval_s = min_interval_s
        self._last = 0.0

    def fetch(self, url: str, data: bytes | None = None, *, validate=None) -> ServiceAnswer:
        key = url + ("?" + data.decode() if data else "")
        entry = self.cache.get(key)
        if entry is not None:
            return ServiceAnswer("COMPLETED", entry.path.read_bytes(), True)
        if self.settings.offline:
            return ServiceAnswer("OFFLINE_NOT_CACHED", None, False, "offline mode and no cached response")
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            self.sleep(wait)
        try:
            headers = {"Content-Type": "application/x-www-form-urlencoded"} if data else None
            resp = self.http.request("POST" if data else "GET", url, data=data, headers=headers)
        except HeimdallError as exc:
            return ServiceAnswer("FAILED", None, False, exc.info.message, exc.code.value)
        finally:
            self._last = time.monotonic()
        problem = validate(resp.body) if validate else None
        if problem:
            return ServiceAnswer("FAILED", None, False, problem, ErrorCode.DATA_MALFORMED.value)
        self.cache.put(key, resp.body, {"service": self.name})
        return ServiceAnswer("COMPLETED", resp.body, False)


def form(params: dict) -> bytes:
    return urllib.parse.urlencode(params).encode()
