"""Evidence bundles: export -> verify -> import into fresh state, path re-rooting, refusal of corrupted/missing evidence, replay labelling."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from heimdall.contracts import HeimdallError
from heimdall.dataservice.db import ServiceDB
from heimdall.evidence import export_bundle, import_bundle, verify_bundle


class EvidenceBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        st, cache = self.tmp / "src" / "state", self.tmp / "src" / "cache"
        db = ServiceDB(st / "dataservice" / "service.sqlite3")
        sha = "ab" * 32
        obj = st / "dataservice" / "store" / "objects" / sha[:2] / f"{sha}.fits"
        obj.parent.mkdir(parents=True)
        obj.write_bytes(b"SIMPLE  =                    T" + b" " * 2850)
        db.execute("INSERT INTO products(sha256,path,size,created_at,validated_at) VALUES(?,?,?,?,?)", (sha, ".heimdall/rel/path.fits", obj.stat().st_size, "t", "t"))
        db.execute("INSERT INTO products(sha256,path,size,created_at,validated_at) VALUES(?,?,?,?,?)", ("cd" * 32, "x", 10, "t", "t"))  # unpinned, not exported
        db.execute("INSERT INTO product_keys(key,sha256,kind,origin,retrieved_at) VALUES(?,?,?,?,?)", ("obs:A|r|full", sha, "obs", "LIVE", "t"))
        db.execute("INSERT INTO product_keys(key,sha256,kind,origin,retrieved_at) VALUES(?,?,?,?,?)", ("obs:B|r|full", "cd" * 32, "obs", "LIVE", "t"))
        db.execute("INSERT INTO manifests(manifest_id,created_at,body) VALUES(?,?,?)", ("man:00", "t", json.dumps({"products": [{"sha256": sha}], "calibrations": []})))
        db.execute("INSERT INTO investigation_results(key,investigation_id,saved_to,manifest_id,created_at) VALUES(?,?,?,?,?)",
                   ("tres:1", "tinv:NASA-01:0000aaaa", ".heimdall/state/targets/NASA-01/latest.json", "man:00", "t"))
        db.execute("INSERT INTO jobs(job_id,kind,payload,state,created_at,updated_at,deadline_at) VALUES(?,?,?,?,?,?,?)",
                   ("job:index:000000000000", "INDEX", "{}", "PREPARING_DATA", "t", "t", "t"))
        db.close()
        t = st / "targets" / "NASA-01"
        t.mkdir(parents=True)
        (t / "0000aaaa.json").write_text(json.dumps({"investigation_id": "tinv:NASA-01:0000aaaa"}))
        (t / "latest.json").write_text(json.dumps({"investigation_id": "tinv:NASA-01:0000aaaa"}))
        (cache / "context" / "gaia").mkdir(parents=True)
        (cache / "context" / "gaia" / "answer.csv").write_text("source_id\n1\n")
        self.st, self.cache = st, cache
        self.bundle = self.tmp / "bundle"
        self.out = export_bundle(st, cache, self.bundle)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip_into_fresh_state_re_roots_paths_and_prunes_absent_objects(self):
        self.assertTrue(verify_bundle(self.bundle)["ok"])
        self.assertEqual(self.out["products"], 1)
        dst = self.tmp / "dst"
        rec = import_bundle(self.bundle, dst / "state", dst / "cache")
        self.assertEqual(rec["product_rows_removed_absent_objects"], 1)
        self.assertEqual(rec["open_jobs_cancelled"], ["job:index:000000000000"])
        c = sqlite3.connect(str(dst / "state" / "dataservice" / "service.sqlite3"))
        try:
            path = c.execute("SELECT path FROM products").fetchall()
            saved = c.execute("SELECT saved_to FROM investigation_results").fetchone()[0]
            keys = [r[0] for r in c.execute("SELECT key FROM product_keys")]
        finally:
            c.close()
        self.assertEqual(len(path), 1)
        self.assertTrue(Path(path[0][0]).is_file())
        self.assertTrue(saved.startswith(str((dst / "state").resolve())))
        self.assertEqual(keys, ["obs:A|r|full"])
        self.assertTrue((dst / "cache" / "context" / "gaia" / "answer.csv").is_file())
        self.assertEqual(json.loads((dst / "state" / "evidence_import.json").read_text())["bundle_id"], self.out["bundle_id"])
        with self.assertRaises(HeimdallError):  # never silently merged into existing state
            import_bundle(self.bundle, dst / "state", dst / "cache")

    def test_missing_or_corrupted_evidence_is_refused_and_nothing_is_written(self):
        obj = next((self.bundle / "state" / "dataservice" / "store" / "objects").rglob("*.fits"))
        data = bytearray(obj.read_bytes())
        data[100] ^= 0xFF  # same-size corruption
        obj.write_bytes(bytes(data))
        rep = verify_bundle(self.bundle)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("checksum mismatch" in p for p in rep["problems"]))
        with self.assertRaises(HeimdallError):
            import_bundle(self.bundle, self.tmp / "x" / "state", self.tmp / "x" / "cache")
        self.assertFalse((self.tmp / "x").exists())
        (self.bundle / "cache" / "context" / "gaia" / "answer.csv").unlink()
        self.assertTrue(any(p.startswith("missing") for p in verify_bundle(self.bundle)["problems"]))
        (self.bundle / "bundle.json").write_text("{not json")
        self.assertFalse(verify_bundle(self.bundle)["ok"])
