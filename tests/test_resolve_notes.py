"""Explicit name resolution (CDS Sesame, mocked transport) and append-only human review notes."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from heimdall.api import Engine
from heimdall.config import load_settings
from heimdall.resolve_notes import ResolveError, ReviewNotes, _cache, parse_sesame, resolve_name
from heimdall.server import Api, ApiError

SESAME = b"""<?xml version="1.0"?><Sesame><Target option="SNV"><name>Barnard's star</name>
<Resolver name="S=Simbad (via url)"><otype>BY*</otype><jpos>17:57:48.49 +04:41:36.1</jpos><jradeg>269.45207695</jradeg>
<jdedeg>4.69339088</jdedeg><oname>NAME Barnard's star</oname></Resolver></Target></Sesame>"""


class ResolveTests(unittest.TestCase):
    def setUp(self):
        _cache.clear()

    def test_parses_position_and_caches_without_a_second_request(self):
        calls = []
        def transport(method, url, data, headers, timeout):
            calls.append(url)
            return SimpleNamespace(status=200, body=SESAME, headers={})
        out = resolve_name("Barnard's star", offline=False, transport=transport)
        self.assertAlmostEqual(out["ra_deg"], 269.45207695)
        self.assertIn("not a HEIMDALL measurement", out["note"])
        self.assertTrue(resolve_name("barnard's  star", offline=False, transport=transport)["cached"])
        self.assertEqual(len(calls), 1)

    def test_offline_unknown_unreachable_and_invalid_are_distinct(self):
        never = lambda *a: self.fail("must not contact the network")  # noqa: E731
        with self.assertRaises(ResolveError) as e:
            resolve_name("M31", offline=True, transport=never)
        self.assertEqual(e.exception.kind, "OFFLINE")
        with self.assertRaises(ResolveError) as e:
            resolve_name("<script>", offline=False, transport=never)
        self.assertEqual(e.exception.kind, "INPUT_INVALID")
        with self.assertRaises(ResolveError) as e:
            parse_sesame(b"<Sesame><Target><INFO>nothing</INFO></Target></Sesame>", "zzz")
        self.assertEqual(e.exception.kind, "NOT_FOUND")
        def down(*a):
            raise TimeoutError
        with self.assertRaises(ResolveError) as e:
            resolve_name("M31", offline=False, transport=down)
        self.assertEqual(e.exception.kind, "UNREACHABLE")


class NotesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = load_settings(environ={"HEIMDALL_STATE_DIR": str(Path(tmp.name) / "s"), "HEIMDALL_CACHE_DIR": str(Path(tmp.name) / "c"), "HEIMDALL_OFFLINE": "1"})
        self.api = Api(Engine(settings=settings))
        self.addCleanup(self.api.svc.close)

    def call(self, method, query=None, body=None):
        return self.api.handle(method, "/api/v1/notes", {k: [v] for k, v in (query or {}).items()}, body)

    def test_notes_are_append_only_labelled_and_persisted(self):
        rec = "tinv:NASA-02:0c714738"
        with self.assertRaises(ApiError) as missing:
            self.call("POST", body={"record": rec, "text": "no such record yet"})
        self.assertEqual(missing.exception.status, 404)
        d = Path(self.api.engine.settings.state_dir) / "targets" / "NASA-02"
        d.mkdir(parents=True)
        (d / "0c714738.json").write_text('{"investigation_id": "tinv:NASA-02:0c714738"}')
        (Path(self.api.engine.settings.state_dir) / "runs" / "R20261115T103457-def358a0").mkdir(parents=True)
        status, first = self.call("POST", body={"record": rec, "text": "Registration residuals look larger on D5.", "about": "observation:x1D5"})
        self.assertEqual((status, first["kind"]), (201, "HUMAN_REVIEW_NOTE"))
        _, second = self.call("POST", body={"record": rec, "text": "Checked: within the stated limitation.", "supersedes": first["note_id"]})
        _, listing = self.call("GET", {"record": rec})
        self.assertIn("not claims", listing["disclaimer"])
        self.assertEqual([n["note_id"] for n in listing["notes"]], [first["note_id"], second["note_id"]])
        self.assertTrue(listing["notes"][0]["superseded"])
        self.assertEqual(self.call("GET", {"record": "R20261115T103457-def358a0"})[1]["notes"], [])
        again = ReviewNotes(Path(self.api.engine.settings.state_dir)).list(rec)
        self.assertEqual(len(again["notes"]), 2)

    def test_invalid_notes_are_rejected(self):
        (Path(self.api.engine.settings.state_dir) / "targets" / "NASA-02").mkdir(parents=True)
        for body in ({"record": "../etc", "text": "x"}, {"record": "NASA-02", "text": ""}, {"record": "NASA-02", "text": "x" * 4001},
                     {"record": "NASA-02", "text": "x", "supersedes": "note:000000000000"}, {"record": "NASA-02", "text": "x", "verdict": "y"}):
            with self.assertRaises(ApiError) as e:
                self.call("POST", body=body)
            self.assertEqual(e.exception.status, 400)

    def test_resolve_route_refuses_offline_as_an_outage(self):
        with self.assertRaises(ApiError) as e:
            self.api.handle("GET", "/api/v1/resolve", {"name": ["M31"]}, None)
        self.assertEqual(e.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
