"""Runtime output must carry the fields docs/OUTPUT_SCHEMA.md documents for the UI. SYNTHETIC run; software test only."""
import tempfile
import unittest
from pathlib import Path

from heimdall.api import Engine

DOC = (Path(__file__).resolve().parent.parent / "docs" / "OUTPUT_SCHEMA.md").read_text(encoding="utf-8")
INV_KEYS = {"investigation_id", "subject", "candidate_ids", "patterns", "evidence", "dependency_groups", "evidence_relations", "hypotheses",
            "interpretation", "missing_evidence", "follow_ups", "planet_x", "upstream", "versions"}
SKY_CAND = {"candidate_id", "kind", "category", "subject_id", "attention_level", "validity", "ra", "dec", "ellipse_1sigma", "sigma_equivalent",
            "planet_x_status", "investigation_id", "interpretation_status", "evidence_refs", "provenance_refs"}


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.e = Engine()
        cls.res, cls.rd = cls.e.demo(out_dir=Path(cls.tmp.name), validate=False, small=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_investigation_keys_present_and_documented(self):
        for i in self.res["investigations"]["investigations"]:
            self.assertTrue(INV_KEYS <= set(i))
        for k in INV_KEYS:
            self.assertIn(k, DOC)

    def test_skymap_candidate_fields_match_documentation(self):
        cands = self.e.skymap(self.rd)["layers"]["candidates"]
        for c in cands:
            self.assertEqual(set(c), SKY_CAND)
        for k in SKY_CAND:
            self.assertIn(k, DOC)

    def test_statuses_are_in_documented_vocabularies(self):
        interp = {"EXPLAINED", "PROVISIONAL", "UNRESOLVED", "UNEXPLAINED_UNDER_INVESTIGATION", "NOT_ASSESSABLE", "INCONCLUSIVE"}
        hyp = {"SUPPORTED", "CONSISTENT", "UNRESOLVED", "CONTRADICTED", "NOT_ASSESSABLE"}
        for i in self.res["investigations"]["investigations"]:
            self.assertIn(i["interpretation"]["status"], interp)
            for h in i["hypotheses"]:
                self.assertIn(h["status"], hyp)

    def test_coordinates_are_real_numbers(self):
        for c in self.e.skymap(self.rd)["layers"]["candidates"]:
            self.assertTrue(-360 <= c["ra"] <= 360 and -90 <= c["dec"] <= 90)

    def test_every_evidence_reference_resolves(self):
        ids = {e["evidence_id"] for i in self.res["investigations"]["investigations"] for e in i["evidence"]}
        for c in self.e.skymap(self.rd)["layers"]["candidates"]:
            self.assertTrue(set(c["evidence_refs"]) <= ids)


if __name__ == "__main__":
    unittest.main()
