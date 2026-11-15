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
