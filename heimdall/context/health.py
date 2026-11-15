"""Reachability of the external context services, classified from the observed evidence (one bounded attempt each, in parallel).

Classification: ``OK`` (a usable answer), ``SERVICE_ANSWERED_ERROR`` (the service itself answered with an HTTP error),
``UPSTREAM_HTTP_5XX`` (5xx, from the service or an intermediate proxy -- they cannot be told apart from the status alone),
``TIMEOUT``, ``DNS_FAILURE``, ``CONNECTION_REFUSED``, ``TLS_FAILURE``, ``NETWORK_UNREACHABLE``. Whether an HTTP(S) proxy is configured is
reported (host only, never credentials), so proxy-mediated failures are not attributed to the service without evidence.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from ..data_access.http import TransportTimeout, TransportTlsError, TransportUnreachable, urllib_transport
from .gaia import ESA_TAP_SYNC, TAP_SYNC
from .small_bodies import SBIDENT_URL

PROBES = (
    ("gaia_dr3@IRSA", "POST", TAP_SYNC, {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": "SELECT TOP 1 source_id FROM gaia_dr3_source"}, b"source_id"),
    ("gaia_dr3@ESA", "POST", ESA_TAP_SYNC, {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": "SELECT TOP 1 source_id FROM gaiadr3.gaia_source"}, b"source_id"),
    ("catwise2020@IRSA", "POST", TAP_SYNC, {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": "SELECT TOP 1 source_name FROM catwise_2020"}, b"source_name"),
    ("jpl_sb_ident", "GET", SBIDENT_URL, None, None),
)


def _proxy() -> str | None:
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        v = os.environ.get(k)
        if v:
            return urllib.parse.urlsplit(v if "://" in v else "http://" + v).hostname
    return None


def _classify_exc(exc: Exception) -> str:
    msg = str(exc).lower()
    if isinstance(exc, TransportTimeout) or "timed out" in msg:
        return "TIMEOUT"
    if isinstance(exc, TransportTlsError):
        return "TLS_FAILURE"
    if "nodename" in msg or "name or service not known" in msg or "getaddrinfo" in msg or "temporary failure in name resolution" in msg:
        return "DNS_FAILURE"
    if "refused" in msg:
        return "CONNECTION_REFUSED"
    return "NETWORK_UNREACHABLE" if isinstance(exc, TransportUnreachable) else "UNEXPECTED"


def probe_one(name, method, url, form, expect, timeout_s: float = 20.0, transport=urllib_transport) -> dict:
    t0 = time.monotonic()
    rec = {"service": name, "host": urllib.parse.urlsplit(url).hostname, "checked_at": datetime.now(UTC).isoformat(timespec="seconds")}
    try:
        data = urllib.parse.urlencode(form).encode() if form else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if form else {}
        resp = transport(method, url, data, headers, timeout_s)
        rec["http_status"] = resp.status
        if 200 <= resp.status < 300 and (expect is None or resp.body.lstrip().lower().startswith(expect)):
            rec["classification"] = "OK"
        elif resp.status >= 500:
            rec["classification"] = "UPSTREAM_HTTP_5XX"
        elif expect is None and 400 <= resp.status < 500:
            rec["classification"] = "OK"  # e.g. JPL answers a parameterless request with a 4xx JSON error: the service is reachable
            rec["note"] = "service answered (parameterless probe)"
        else:
            rec["classification"] = "SERVICE_ANSWERED_ERROR"
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        rec["classification"] = _classify_exc(exc)
        rec["detail"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    rec["elapsed_s"] = round(time.monotonic() - t0, 2)
    return rec


def context_health(timeout_s: float = 20.0, transport=urllib_transport) -> dict:
    with ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
        recs = list(pool.map(lambda p: probe_one(*p, timeout_s=timeout_s, transport=transport), PROBES))
    return {"schema": "heimdall.context_health/1.0", "proxy_configured": _proxy(), "services": recs,
            "note": "a 5xx cannot be attributed to the service rather than an intermediate proxy from the status alone"}
