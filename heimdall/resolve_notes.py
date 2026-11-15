"""Two narrowly scoped browser services.

* ``resolve_name``: object-name resolution through CDS Sesame (SIMBAD/NED/VizieR). It is called only when the user explicitly asks
  for it (the request sends the typed name to CDS). The result is a position with its source, never a HEIMDALL finding. Offline
  mode refuses without contacting the network. Results are cached in memory for the process lifetime.
* Review notes: append-only human notes attached to a saved record (``tinv:``/run/target). They are persisted as JSON lines under
  ``<state>/review_notes/`` and are always labelled as human annotations, separate from claims, verdicts and hypotheses. A note is
  never edited or deleted; a later note may supersede it (``supersedes``), and both stay readable.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from .data_access.http import urllib_transport

SESAME_URL = "https://cds.unistra.fr/cgi-bin/nph-sesame/-ox/~SNV?"
NAME_RE = re.compile(r"^[\w .+\-*'()\[\]/:]{2,80}$")
RECORD_RE = re.compile(r"^(tinv:[A-Za-z0-9.+-]+:[0-9a-f]{8}|R\d{8}T\d{6}-[0-9a-f]{8}|NASA-0[1-5])$")
NOTES_SCHEMA = "heimdall.review_notes/1.0"
MAX_NOTE = 4000
_cache: dict[str, dict] = {}
_lock = threading.Lock()


class ResolveError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def resolve_name(name: str, offline: bool, transport=urllib_transport, timeout_s: float = 8.0) -> dict:
    name = " ".join(str(name or "").split())
    if not NAME_RE.match(name):
        raise ResolveError("INPUT_INVALID", "name must be 2-80 characters of letters, digits, spaces and . + - * ' ( ) [ ] / :")
    key = name.lower()
    if key in _cache:
        return _cache[key] | {"cached": True}
    if offline:
        raise ResolveError("OFFLINE", "offline mode: name resolution needs the CDS Sesame service and is disabled")
    try:
        resp = transport("GET", SESAME_URL + urllib.parse.quote(name), None, {"Accept": "text/xml"}, timeout_s)
    except Exception as exc:  # noqa: BLE001 - classified for the caller
        raise ResolveError("UNREACHABLE", f"CDS Sesame could not be reached ({type(exc).__name__})") from None
    if resp.status >= 500:
        raise ResolveError("UNREACHABLE", f"CDS Sesame answered HTTP {resp.status}")
    if resp.status != 200:
        raise ResolveError("UNREACHABLE", f"CDS Sesame answered HTTP {resp.status}")
    out = parse_sesame(resp.body, name)
    with _lock:
        _cache[key] = out
    return out | {"cached": False}


def parse_sesame(body: bytes, name: str) -> dict:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise ResolveError("MALFORMED", "CDS Sesame returned unreadable XML") from None
    for res in root.iter("Resolver"):
        ra, dec = res.findtext("jradeg"), res.findtext("jdedeg")
        if ra and dec:
            try:
                ra_f, dec_f = float(ra), float(dec)
            except ValueError:
                continue
            if 0 <= ra_f < 360 and -90 <= dec_f <= 90:
                return {"schema": "heimdall.name_resolution/1.0", "query": name, "ra_deg": ra_f, "dec_deg": dec_f,
                        "resolver": res.get("name", "Sesame"), "object": res.findtext("oname"), "object_type": res.findtext("otype"),
                        "frame": "ICRS J2000 (catalogue position; proper motion not applied)",
                        "note": "a catalogue position for navigation, not a HEIMDALL measurement"}
    raise ResolveError("NOT_FOUND", f"no resolver returned a position for {name!r}")


class ReviewNotes:
    def __init__(self, state_dir: Path) -> None:
        self.root = Path(state_dir) / "review_notes"

    def _path(self, record: str) -> Path:
        if not RECORD_RE.match(record or ""):
            raise ValueError("record must be a tinv: version, a run id or NASA-0N")
        return self.root / (hashlib.sha256(record.encode()).hexdigest()[:24] + ".jsonl")

    def list(self, record: str) -> dict:
        path = self._path(record)
        notes = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("record") == record:
                    notes.append(row)
        superseded = {n.get("supersedes") for n in notes if n.get("supersedes")}
        for n in notes:
            n["superseded"] = n["note_id"] in superseded
        return {"schema": NOTES_SCHEMA, "record": record, "notes": notes,
                "kind": "HUMAN_REVIEW_NOTE", "disclaimer": "human annotations; they are not claims, verdicts or evidence and do not change any result"}

    def add(self, record: str, text: str, author: str = "", about: str | None = None, supersedes: str | None = None) -> dict:
        path = self._path(record)
        text = str(text or "").strip()
        if not text or len(text) > MAX_NOTE:
            raise ValueError(f"note text must be 1-{MAX_NOTE} characters")
        author = str(author or "").strip()[:80]
        if about is not None and (not isinstance(about, str) or len(about) > 200):
            raise ValueError("about must be a short identifier")
        existing = {n["note_id"] for n in self.list(record)["notes"]}
        if supersedes and supersedes not in existing:
            raise ValueError("supersedes must name an existing note of this record")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        note = {"note_id": "note:" + hashlib.sha256(f"{record}|{now}|{text}|{len(existing)}".encode()).hexdigest()[:12], "record": record,
                "about": about, "text": text, "author": author or None, "created_at": now, "supersedes": supersedes, "kind": "HUMAN_REVIEW_NOTE"}
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(note, sort_keys=True) + "\n")
        return note
