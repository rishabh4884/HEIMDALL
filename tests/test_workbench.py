"""Workbench routes, assets, and provenance for locally displayed FITS pixels."""

import base64
import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
from astropy.io import fits

from heimdall.api import Engine
from heimdall.config import load_settings
from heimdall.server import Api, ApiError, make_handler


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        settings = load_settings(environ={
            "HEIMDALL_STATE_DIR": str(self.root / "state"),
            "HEIMDALL_CACHE_DIR": str(self.root / "cache"),
            "HEIMDALL_OFFLINE": "1",
        })
        self.api = Api(Engine(settings=settings))
        self.addCleanup(self.api.svc.close)

    def get(self, path, **query):
        return self.api.handle("GET", "/api/v1" + path, {k: [v] for k, v in query.items()}, None)[1]

    def test_root_and_modules_are_served_without_exposing_service_data(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.api, "test-token", None))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for path, mime, excerpt in [
            ("/", "text/html", "SPHEREx DR1 sky"), ("/workbench.css", "text/css", ".map-stage"),
            ("/workbench.js", "text/javascript", "./sky.js"), ("/sky.js", "text/javascript", "SkyMap"),
            ("/charts.js", "text/javascript", "renderChart"), ("/records.js", "text/javascript", "traceView"),
            ("/hips.js", "text/javascript", "tileSample"),
        ]:
            with self.subTest(path=path), urllib.request.urlopen(base + path, timeout=5) as response:
                self.assertEqual(response.headers.get_content_type(), mime)
                self.assertIn(excerpt, response.read().decode())
                self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/api/v1/targets", timeout=5)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

    def test_saved_run_map_and_list_use_local_records(self):
        run_id = "R20261115T100000-deadbeef"
        directory = self.root / "state/runs" / run_id
        directory.mkdir(parents=True)
        doc = {"run_id": run_id, "data_origin": "SYNTHETIC",
               "request": {"ra_deg": 359.9, "dec_deg": 0, "radius_deg": 0.05}}
        (directory / "investigation.json").write_text(json.dumps(doc))
        row = self.get("/runs")["runs"][0]
        self.assertEqual(row["run_id"], run_id)
        self.assertNotIn("path", row)
        sky = self.get(f"/runs/{run_id}/skymap")
        self.assertEqual(sky["field"], doc["request"])
        self.assertEqual(sky["watermark"], "SYNTHETIC - NOT REAL DATA")
        self.assertEqual(self.get("/inventory/domains"), {"domains": []})

    def test_immutable_pattern_and_report_are_derived_from_selected_version(self):
        directory = self.root / "state/targets/NASA-02"
        directory.mkdir(parents=True)
        for hash_, name in [("0000abcd", "Older"), ("1111abcd", "Newer")]:
            doc = {"investigation_id": f"tinv:NASA-02:{hash_}", "target": {"target_id": "NASA-02", "name": name}}
            (directory / f"{hash_}.json").write_text(json.dumps(doc))
        path = "/investigations/tinv:NASA-02:0000abcd"
        self.assertEqual(self.get(path + "/patterns")["investigation_id"], "tinv:NASA-02:0000abcd")
        self.assertEqual(self.get(path + "/report")["target"]["name"], "Older")
        self.assertIn("report: Older", self.get(path + "/report", format="md")["markdown"])

    def test_preview_uses_exact_hash_and_refuses_changed_bytes(self):
        image = io.BytesIO()
        fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.arange(40, dtype=np.float32).reshape(5, 8), name="IMAGE")]).writeto(image)
        data = image.getvalue()
        sha = hashlib.sha256(data).hexdigest()
        directory = self.root / "cache/irsa-spherex/ab"
        directory.mkdir(parents=True)
        content = directory / "entry.bin"
        content.write_bytes(data)
        (directory / "entry.json").write_text(json.dumps({"sha256": sha}))
        preview = self.get("/evidence/preview", sha256=sha)
        self.assertEqual((preview["width"], preview["height"], preview["hdu"]), (8, 5, "IMAGE"))
        self.assertEqual(len(base64.b64decode(preview["pixels"])), 40)
        content.write_bytes(data + b"corrupt")
        with self.assertRaises(ApiError) as missing:
            self.get("/evidence/preview", sha256=sha)
        self.assertEqual(missing.exception.status, 404)
        self.assertTrue(content.exists())
        with self.assertRaises(ApiError) as invalid:
            self.get("/evidence/preview", sha256="../not-a-hash")
        self.assertEqual(invalid.exception.status, 400)
