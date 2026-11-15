"""HTTP transport (heimdall.server): routing, validation, readiness-first behaviour, stable errors. Offline engine; no network."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from heimdall.api import Engine
from heimdall.config import load_settings
from heimdall.server import Api, ApiError, make_handler


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        env = {"HEIMDALL_STATE_DIR": str(self.tmp / "state"), "HEIMDALL_CACHE_DIR": str(self.tmp / "cache"), "HEIMDALL_OFFLINE": "1"}
        self.engine = Engine(settings=load_settings(environ=env))
        self.api = Api(self.engine)

    def tearDown(self):
        self.engine.data_service().close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, method, path, query=None, body=None):
        return self.api.handle(method, path, query or {}, body)

    def err(self, method, path, query=None, body=None):
        with self.assertRaises(ApiError) as cm:
            self.call(method, path, query, body)
        return cm.exception

    def test_health_reports_versions_offline_and_worker(self):
        code, h = self.call("GET", "/api/v1/health")
        self.assertEqual(code, 200)
        self.assertEqual(h["schema"], "heimdall.http/1.0")
        self.assertTrue(h["offline"])
        self.assertFalse(h["worker"]["running"])

    def test_submit_returns_202_job_and_reconnects_to_the_same_job(self):
        code, a = self.call("POST", "/api/v1/investigations", body={"target_id": "NASA-04"})
        self.assertEqual(code, 202)
        self.assertEqual(a["readiness"], "PREPARING")
        self.assertFalse(a["must_resubmit"])
        _, b = self.call("POST", "/api/v1/investigations", body={"target_id": "NASA-04"})
        self.assertEqual(a["job"]["job_id"], b["job"]["job_id"])
        code, st = self.call("GET", f"/api/v1/jobs/{a['job']['job_id']}")
        self.assertEqual((code, st["status"]["execution"]), (200, "PREPARING_DATA"))
        self.assertIn("not a non-detection", st["investigation"]["note"])
        _, lst = self.call("GET", "/api/v1/jobs", {"state": ["QUEUED"]})
        self.assertEqual(lst["total"], 1)
        code, c = self.call("POST", f"/api/v1/jobs/{a['job']['job_id']}/cancel")
        self.assertEqual(c["readiness"], "CANCELLED")
        self.assertTrue(c["must_resubmit"])

    def test_coordinate_submission_and_option_validation(self):
        code, a = self.call("POST", "/api/v1/investigations", body={"ra": 150.1, "dec": 2.2, "category": "VARIABLE_STAR", "options": {"max_observations": 8}})
        self.assertEqual(code, 202)
        self.assertEqual(a["investigation"]["target"], "COORD-150.10000+2.20000")
        for bad in ({"ra": 1.0}, {"ra": 1.0, "dec": 95.0}, {"target_id": "../etc"}, {"target_id": "NASA-04", "options": {"max_observations": 10_000}},
                    {"target_id": "NASA-04", "options": {"rm": 1}}, {"target_id": "NASA-04", "evil": 1}, {"ra": 1, "dec": 1, "category": "PLANET_X"},
                    {"target_id": "NASA-04", "options": {"detectors": [7]}}):
            self.assertEqual(self.err("POST", "/api/v1/investigations", body=bad).status, 400, bad)

    def test_coverage_of_an_unindexed_region_is_a_job_not_no_observations(self):
        code, c = self.call("GET", "/api/v1/coverage", {"ra": ["10"], "dec": ["-5"], "radius": ["0.05"]})
        self.assertEqual(code, 200)
        self.assertEqual(c["readiness"], "PREPARING")
        self.assertIsNone(c["observations"])
        self.assertIn("not 'no observations'", c["note"])
        self.assertEqual(self.err("GET", "/api/v1/coverage", {"ra": ["10"], "dec": ["-5"], "radius": ["40"]}).status, 400)
        self.assertEqual(self.err("GET", "/api/v1/coverage", {"ra": ["x"], "dec": ["-5"]}).status, 400)

    def test_saved_results_are_read_safely_and_versions_by_id(self):
        self.assertEqual(self.err("GET", "/api/v1/targets/NASA-01").status, 404)
        d = Path(self.engine.settings.state_dir) / "targets" / "NASA-01"
        d.mkdir(parents=True)
        doc = {"investigation_id": "tinv:NASA-01:0123abcd", "status": {"execution": "COMPLETED"}}
        (d / "0123abcd.json").write_text(json.dumps(doc))
        (d / "latest.json").write_text(json.dumps(doc))
        self.assertEqual(self.call("GET", "/api/v1/targets/NASA-01")[1]["investigation_id"], doc["investigation_id"])
        self.assertEqual(self.call("GET", "/api/v1/investigations/tinv:NASA-01:0123abcd")[1], doc)
        (d / "deadbeef.json").write_text(json.dumps(doc))  # file name and id disagree -> not served under the wrong id
        self.assertEqual(self.err("GET", "/api/v1/investigations/tinv:NASA-01:deadbeef").status, 404)
        self.assertEqual(self.err("GET", "/api/v1/targets/NASA-01/attempt").status, 404)
        newer = {"investigation_id": "tinv:NASA-01:1111beef", "created_at": "2026-11-15T09:00:00+00:00", "status": {"execution": "COMPLETED"},
                 "supersedes": {"investigation_id": "tinv:NASA-01:0123abcd", "reason": "rule fix"}}
        (d / "1111beef.json").write_text(json.dumps(newer))
        (d / "latest.json").write_text(json.dumps(newer))
        v = self.call("GET", "/api/v1/targets/NASA-01/versions")[1]
        self.assertEqual(v["latest_usable"], "tinv:NASA-01:1111beef")
        self.assertEqual(v["versions"][0]["investigation_id"], "tinv:NASA-01:1111beef")
        old = next(x for x in v["versions"] if x["investigation_id"] == "tinv:NASA-01:0123abcd")
        self.assertEqual(old["superseded_by"], "tinv:NASA-01:1111beef")
        self.assertEqual(self.err("GET", "/api/v1/targets/NASA-03/versions").status, 404)
        for bad in ("/api/v1/targets/..", "/api/v1/jobs/job;drop", "/api/v1/manifests/x", "/api/v1/targets/NASA-01%2F..%2F..%2Fx"):
            self.assertEqual(self.err("GET", bad).status, 400, bad)
        self.assertIn(self.err("GET", "/api/v1/investigations/tinv:NASA-01:../../x").status, (400, 404))  # rejected, never read
        self.assertEqual(self.err("GET", "/api/v1/nope").status, 404)
        self.assertEqual(self.err("GET", "/elsewhere").status, 404)


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        env = {"HEIMDALL_STATE_DIR": str(self.tmp / "state"), "HEIMDALL_CACHE_DIR": str(self.tmp / "cache"), "HEIMDALL_OFFLINE": "1"}
        self.engine = Engine(settings=load_settings(environ=env))

    def tearDown(self):
        self.engine.data_service().close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _serve(self, token=None):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api(self.engine), token, None))
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def _req(self, url, data=None, headers=None):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read()), dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read()), dict(e.headers)

    def test_round_trip_errors_and_cors(self):
        base = self._serve()
        code, body, hdr = self._req(base + "/api/v1/health", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(code, 200)
        self.assertEqual(hdr.get("Access-Control-Allow-Origin"), "http://localhost:5173")
        code, body, hdr = self._req(base + "/api/v1/health", headers={"Origin": "http://evil.example"})
        self.assertIsNone(hdr.get("Access-Control-Allow-Origin"))
        code, body, _ = self._req(base + "/api/v1/investigations", data=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual((code, body["error"]["code"]), (400, "INPUT_INVALID"))
        code, body, _ = self._req(base + "/api/v1/investigations", data=json.dumps({"target_id": "NASA-05"}).encode())
        self.assertEqual(code, 202)
        self.assertEqual(body["schema"], "heimdall.data_status/1.0")

    def test_bearer_token_is_enforced_when_configured(self):
        base = self._serve(token="t0k")
        self.assertEqual(self._req(base + "/api/v1/health")[0], 401)
        self.assertEqual(self._req(base + "/api/v1/health", headers={"Authorization": "Bearer t0k"})[0], 200)


if __name__ == "__main__":
    unittest.main()
