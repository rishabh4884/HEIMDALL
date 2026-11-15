"""HTTP access with verified TLS, typed failures and bounded retries.

Failure mapping (never "empty result"):
- connection refused, DNS failure     -> ARCHIVE_UNREACHABLE (retried)
- TLS verification failure            -> ARCHIVE_UNREACHABLE (not retried)
- timeout, HTTP 408/504               -> ARCHIVE_TIMEOUT (retried)
- HTTP 401                            -> AUTHENTICATION_FAILED
- HTTP 403                            -> ACCESS_DENIED
- HTTP 404                            -> RETRIEVAL_FAILED (a listed product was not found; not proof of absence)
- HTTP 429/500/502/503                -> RETRIEVAL_FAILED (retried)
- other non-2xx                       -> RETRIEVAL_FAILED

Error details carry the host only, never full URLs with parameters or
credentials. The transport is injectable so tests never touch the network.
"""

from __future__ import annotations

import os
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from ..contracts.outcomes import ErrorCode, HeimdallError


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class TransportTimeout(Exception):
    pass


class TransportUnreachable(Exception):
    pass


class TransportTlsError(Exception):
    pass


Transport = Callable[[str, str, bytes | None, dict[str, str], float], HttpResponse]


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
        if ctx.cert_store_stats().get("x509_ca", 0) == 0 and os.path.exists("/etc/ssl/cert.pem"):
            ctx.load_verify_locations("/etc/ssl/cert.pem")
        return ctx


_CONTEXT: ssl.SSLContext | None = None


def urllib_transport(method: str, url: str, data: bytes | None, headers: dict[str, str], timeout: float) -> HttpResponse:
    global _CONTEXT
    if _CONTEXT is None:
        _CONTEXT = _ssl_context()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_CONTEXT) as resp:
            return HttpResponse(status=resp.status, body=resp.read(), headers=dict(resp.headers))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except OSError:
            body = b""
        return HttpResponse(status=exc.code, body=body, headers=dict(exc.headers or {}))
    except (socket.timeout, TimeoutError) as exc:
        raise TransportTimeout(str(exc)) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise TransportTimeout(str(reason)) from None
        if isinstance(reason, ssl.SSLError):
            raise TransportTlsError(str(reason)) from None
        raise TransportUnreachable(str(reason)) from None
    except ssl.SSLError as exc:
        raise TransportTlsError(str(exc)) from None
    except OSError as exc:
        raise TransportUnreachable(str(exc)) from None


_RETRY_STATUS = {408: ErrorCode.ARCHIVE_TIMEOUT, 504: ErrorCode.ARCHIVE_TIMEOUT, 429: ErrorCode.RETRIEVAL_FAILED,
                 500: ErrorCode.RETRIEVAL_FAILED, 502: ErrorCode.RETRIEVAL_FAILED, 503: ErrorCode.RETRIEVAL_FAILED}


class HttpClient:
    def __init__(self, source_id: str, timeout_s: float, max_retries: int, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, backoff_s: float = 1.0, offline: bool = False,
                 jitter: float = 0.0, rng: random.Random | None = None) -> None:
        self.source_id = source_id
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.backoff_s = backoff_s
        self.offline = offline
        # Random extra delay of up to ``jitter`` x the backoff, so that parallel clients do not retry in lock-step.
        # Zero (the default) keeps backoff deterministic; the IRSA adapter enables it.
        self.jitter = jitter
        self.rng = rng or random.Random()
        self.attempts = 0

    def request(self, method: str, url: str, data: bytes | None = None, headers: dict[str, str] | None = None,
                timeout_s: float | None = None, max_retries: int | None = None) -> HttpResponse:
        """One logical request. ``timeout_s`` and ``max_retries`` override the client defaults for this call (route-specific budgets)."""
        timeout = self.timeout_s if timeout_s is None else timeout_s
        retries = self.max_retries if max_retries is None else max_retries
        host = urllib.parse.urlsplit(url).netloc
        if self.offline:
            raise HeimdallError.of(ErrorCode.ARCHIVE_UNREACHABLE, f"offline mode: no network request to {host} is made",
                                   host=host, offline="true")
        last: HeimdallError | None = None
        retry_after: float | None = None
        for attempt in range(retries + 1):
            if attempt:
                # Honour a server Retry-After (rate limiting), bounded to 60 s; otherwise exponential backoff.
                if retry_after is not None:
                    self.sleep(min(retry_after, 60.0))
                else:
                    base = self.backoff_s * 2 ** (attempt - 1)
                    self.sleep(base * (1.0 + self.jitter * self.rng.random()) if self.jitter else base)
            retry_after = None
            self.attempts += 1
            try:
                resp = self.transport(method, url, data, dict(headers or {}), timeout)
            except TransportTimeout:
                last = HeimdallError.of(ErrorCode.ARCHIVE_TIMEOUT, f"request to {host} timed out", host=host, source=self.source_id)
                continue
            except TransportTlsError:
                raise HeimdallError.of(ErrorCode.ARCHIVE_UNREACHABLE, f"TLS verification failed for {host}", host=host,
                                       source=self.source_id) from None
            except TransportUnreachable:
                last = HeimdallError.of(ErrorCode.ARCHIVE_UNREACHABLE, f"{host} is unreachable", host=host, source=self.source_id)
                continue
            if 200 <= resp.status < 300:
                return resp
            if resp.status == 401:
                raise HeimdallError.of(ErrorCode.AUTHENTICATION_FAILED, f"{host} rejected the credentials (HTTP 401)", host=host)
            if resp.status == 403:
                raise HeimdallError.of(ErrorCode.ACCESS_DENIED, f"{host} denied access (HTTP 403)", host=host)
            if resp.status == 404:
                raise HeimdallError.of(ErrorCode.RETRIEVAL_FAILED, f"{host} did not find the requested product (HTTP 404)",
                                       host=host, http_status="404")
            if resp.status in _RETRY_STATUS:
                last = HeimdallError.of(_RETRY_STATUS[resp.status], f"{host} returned HTTP {resp.status}", host=host,
                                        http_status=str(resp.status))
                value = {k.lower(): v for k, v in resp.headers.items()}.get("retry-after", "")
                retry_after = float(value) if value.strip().isdigit() else None
                continue
            raise HeimdallError.of(ErrorCode.RETRIEVAL_FAILED, f"{host} returned HTTP {resp.status}", host=host,
                                   http_status=str(resp.status))
        assert last is not None
        raise last
