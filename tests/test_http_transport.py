"""Real-socket transport behaviour: error responses, timeouts and success all release their resources (no ResourceWarning)."""

from __future__ import annotations

import gc
import threading
import time
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from heimdall.data_access.http import TransportTimeout, urllib_transport


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/slow"):
            time.sleep(0.5)
        code = 502 if self.path.startswith("/bad") else 429 if self.path.startswith("/limit") else 200
        body = b"<html>Proxy Error</html>" if code != 200 else b"SIMPLE  =                    T"
        self.send_response(code)
        if code == 429:
            self.send_header("Retry-After", "7")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TransportCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_repeated_error_responses_timeouts_and_successes_close_their_resources(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            for _ in range(25):
                r = urllib_transport("GET", self.base + "/bad", None, {}, 5.0)
                self.assertEqual((r.status, r.body), (502, b"<html>Proxy Error</html>"))
            r = urllib_transport("GET", self.base + "/limit", None, {}, 5.0)
            self.assertEqual((r.status, r.headers.get("Retry-After")), (429, "7"))
            for _ in range(5):
                self.assertEqual(urllib_transport("GET", self.base + "/ok", None, {}, 5.0).status, 200)
            with self.assertRaises(TransportTimeout):
                urllib_transport("GET", self.base + "/slow", None, {}, 0.1)
            gc.collect()
        leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(leaks, [], [str(w.message) for w in leaks])


if __name__ == "__main__":
    unittest.main()


class ContextHealthTests(unittest.TestCase):
    def test_failures_are_classified_from_evidence(self):
        from heimdall.context.health import context_health
        from heimdall.data_access.http import HttpResponse, TransportUnreachable

        def transport(method, url, data, headers, timeout):
            if "gea.esac" in url:
                raise TransportTimeout("timed out")
            if "ssd-api" in url:
                raise TransportUnreachable("[Errno 8] nodename nor servname provided, or not known")
            if b"catwise" in (data or b""):
                return HttpResponse(502, b"<html>Proxy Error</html>")
            return HttpResponse(200, b"source_id\n1\n")
        h = context_health(transport=transport)
        got = {r["service"]: r["classification"] for r in h["services"]}
        self.assertEqual(got, {"gaia_dr3@IRSA": "OK", "gaia_dr3@ESA": "TIMEOUT", "catwise2020@IRSA": "UPSTREAM_HTTP_5XX", "jpl_sb_ident": "DNS_FAILURE"})
