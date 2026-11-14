"""Reusable conformance tests for any ``DataSource`` adapter.

Subclass ``DataSourceContract`` together with ``unittest.TestCase`` and set
``make_source`` and ``expect_synthetic``. The real adapter must pass the same
suite (with ``expect_synthetic = False``) once it exists. That is what keeps
the boundary stable: a new source is accepted only if it honours this contract.
"""

from __future__ import annotations

from heimdall.contracts import HeimdallError, ResultOutcome
from heimdall.data_access import (
    AccessReport,
    DataSource,
    DiscoveryQuery,
    DiscoveryResult,
    SkyRegion,
    SourceDescription,
)

ACCESS_ERROR_CODES = {
    "CREDENTIALS_MISSING",
    "AUTHENTICATION_FAILED",
    "ACCESS_DENIED",
    "ARCHIVE_UNREACHABLE",
    "ARCHIVE_TIMEOUT",
}


class DataSourceContract:
    expect_synthetic: bool = True

    def make_source(self) -> DataSource:  # pragma: no cover - provided by subclasses
        raise NotImplementedError

    def query(self) -> DiscoveryQuery:
        # A neutral, syntactically valid region; not a scientific target.
        return DiscoveryQuery(region=SkyRegion(ra_deg=0.0, dec_deg=0.0, radius_deg=0.1))

    def test_describe_matches_source_id(self):
        source = self.make_source()
        description = source.describe()
        self.assertIsInstance(description, SourceDescription)
        self.assertEqual(description.source_id, source.source_id)

    def test_check_access_returns_report_or_typed_access_error(self):
        source = self.make_source()
        try:
            report = source.check_access()
        except HeimdallError as exc:
            self.assertIn(str(exc.code), ACCESS_ERROR_CODES)
        else:
            self.assertIsInstance(report, AccessReport)
            self.assertEqual(report.source_id, source.source_id)

    def test_discover_returns_valid_result_or_typed_error(self):
        source = self.make_source()
        try:
            result = source.discover(self.query())
        except HeimdallError:
            return
        self.assertIsInstance(result, DiscoveryResult)
        self.assertIn(result.outcome, (ResultOutcome.OK, ResultOutcome.NO_OBSERVATIONS, ResultOutcome.PARTIAL))
        self.assertEqual(result.provenance.is_synthetic, self.expect_synthetic)
        for observation in result.observations:
            self.assertEqual(observation.source_id, source.source_id)
            self.assertEqual(observation.is_synthetic, self.expect_synthetic)
