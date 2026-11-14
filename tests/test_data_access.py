import json
import unittest
from datetime import UTC, datetime

from heimdall import __version__
from heimdall.config import ENV_DATA_SOURCE, ENV_SOURCE_CREDENTIAL, load_settings
from heimdall.contracts import (
    ContractViolation,
    DataOrigin,
    ErrorCategory,
    ErrorCode,
    ErrorInfo,
    FailedItem,
    HeimdallError,
    Observation,
    Provenance,
    ResultOutcome,
)
from heimdall.data_access import AdapterRegistry, DiscoveryQuery, DiscoveryResult, RetrievalRequest, SkyRegion, default_registry
from heimdall.serialization import dumps
from tests.contract.data_source_contract import DataSourceContract
from tests.fixtures.synthetic_source import SOURCE_ID, Mode, SyntheticFixtureSource

WITH_CREDENTIAL = load_settings(environ={ENV_DATA_SOURCE: SOURCE_ID, ENV_SOURCE_CREDENTIAL: "test-only-value"})
WITHOUT_CREDENTIAL = load_settings(environ={ENV_DATA_SOURCE: SOURCE_ID})
QUERY = DiscoveryQuery(region=SkyRegion(ra_deg=0.0, dec_deg=0.0, radius_deg=0.1))


def provenance():
    return Provenance(origin=DataOrigin.SYNTHETIC_TEST_FIXTURE, retrieved_at=datetime.now(UTC),
                      software_version=__version__)


class SyntheticSourceConformance(DataSourceContract, unittest.TestCase):
    expect_synthetic = True

    def make_source(self):
        return SyntheticFixtureSource(WITH_CREDENTIAL)


class FailureModeTests(unittest.TestCase):
    def test_empty_success_is_a_result_not_an_error(self):
        result = SyntheticFixtureSource(WITH_CREDENTIAL, Mode.EMPTY).discover(QUERY)
        self.assertEqual(result.outcome, ResultOutcome.NO_OBSERVATIONS)
        self.assertEqual(result.observations, ())

    def test_each_failure_mode_has_a_distinct_code(self):
        expected = {
            Mode.UNREACHABLE: ErrorCode.ARCHIVE_UNREACHABLE,
            Mode.TIMEOUT: ErrorCode.ARCHIVE_TIMEOUT,
            Mode.AUTH_FAILED: ErrorCode.AUTHENTICATION_FAILED,
            Mode.ACCESS_DENIED: ErrorCode.ACCESS_DENIED,
            Mode.MALFORMED: ErrorCode.DATA_MALFORMED,
        }
        seen = set()
        for mode, code in expected.items():
            with self.subTest(mode=mode):
                with self.assertRaises(HeimdallError) as ctx:
                    SyntheticFixtureSource(WITH_CREDENTIAL, mode).discover(QUERY)
                self.assertEqual(ctx.exception.code, code)
                seen.add(ctx.exception.code)
        self.assertEqual(len(seen), len(expected))

    def test_missing_credentials_distinct_from_auth_failure(self):
        with self.assertRaises(HeimdallError) as ctx:
            SyntheticFixtureSource(WITHOUT_CREDENTIAL).check_access()
        self.assertEqual(ctx.exception.code, ErrorCode.CREDENTIALS_MISSING)
        self.assertEqual(ctx.exception.info.category, ErrorCategory.CONFIGURATION)
        self.assertFalse(ctx.exception.info.retryable)

    def test_network_errors_are_retryable_access_errors_are_not(self):
        self.assertTrue(ErrorInfo(code=ErrorCode.ARCHIVE_TIMEOUT, message="m").retryable)
        self.assertFalse(ErrorInfo(code=ErrorCode.AUTHENTICATION_FAILED, message="m").retryable)
        self.assertFalse(ErrorInfo(code=ErrorCode.DATA_MALFORMED, message="m").retryable)

    def test_partial_result_lists_failed_items(self):
        result = SyntheticFixtureSource(WITH_CREDENTIAL, Mode.PARTIAL).discover(QUERY)
        self.assertEqual(result.outcome, ResultOutcome.PARTIAL)
        self.assertEqual(len(result.failed_items), 1)

    def test_retrieval_data_unavailable_distinct_from_failure(self):
        result = SyntheticFixtureSource(WITH_CREDENTIAL, Mode.EMPTY).retrieve(RetrievalRequest(obs_id="SYNTHETIC-OBS-001"))
        self.assertEqual(result.outcome, ResultOutcome.DATA_UNAVAILABLE)
        with self.assertRaises(HeimdallError):
            SyntheticFixtureSource(WITH_CREDENTIAL, Mode.TIMEOUT).retrieve(RetrievalRequest(obs_id="x"))


class ResultInvariantTests(unittest.TestCase):
    def test_no_observations_must_be_empty(self):
        obs = Observation(obs_id="SYNTHETIC-1", source_id=SOURCE_ID, provenance=provenance())
        with self.assertRaises(ContractViolation):
            DiscoveryResult(outcome=ResultOutcome.NO_OBSERVATIONS, observations=(obs,), provenance=provenance())

    def test_ok_requires_observations_and_partial_requires_failures(self):
        with self.assertRaises(ContractViolation):
            DiscoveryResult(outcome=ResultOutcome.OK, observations=(), provenance=provenance())
        with self.assertRaises(ContractViolation):
            DiscoveryResult(outcome=ResultOutcome.PARTIAL, observations=(), provenance=provenance())
        failed = FailedItem(ref="x", error=ErrorInfo(code=ErrorCode.RETRIEVAL_FAILED, message="m"))
        DiscoveryResult(outcome=ResultOutcome.PARTIAL, observations=(), provenance=provenance(), failed_items=(failed,))

    def test_scientific_outcomes_are_not_valid_discovery_outcomes(self):
        with self.assertRaises(ContractViolation):
            DiscoveryResult(outcome=ResultOutcome.NOT_DETECTED, observations=(), provenance=provenance())

    def test_sky_region_validation(self):
        for kwargs in ({"ra_deg": 360.0, "dec_deg": 0.0, "radius_deg": 1.0},
                       {"ra_deg": 0.0, "dec_deg": 91.0, "radius_deg": 1.0},
                       {"ra_deg": 0.0, "dec_deg": 0.0, "radius_deg": 0.0},
                       {"ra_deg": float("nan"), "dec_deg": 0.0, "radius_deg": 1.0}):
            with self.assertRaises(ContractViolation):
                SkyRegion(**kwargs)

    def test_provenance_requires_timezone(self):
        with self.assertRaises(ContractViolation):
            Provenance(origin=DataOrigin.LIVE, retrieved_at=datetime(2026, 1, 1), software_version="x")

    def test_synthetic_records_are_marked_in_serialized_output(self):
        result = SyntheticFixtureSource(WITH_CREDENTIAL).discover(QUERY)
        payload = json.loads(dumps(result))
        self.assertEqual(payload["data"]["provenance"]["origin"], "SYNTHETIC_TEST_FIXTURE")
        self.assertTrue(all(o.is_synthetic for o in result.observations))


class RegistryTests(unittest.TestCase):
    def test_unconfigured_source(self):
        with self.assertRaises(HeimdallError) as ctx:
            AdapterRegistry().create(load_settings(environ={}))
        self.assertEqual(ctx.exception.code, ErrorCode.SOURCE_NOT_CONFIGURED)

    def test_unknown_source(self):
        with self.assertRaises(HeimdallError) as ctx:
            AdapterRegistry().create(WITH_CREDENTIAL)
        self.assertEqual(ctx.exception.code, ErrorCode.CONFIG_INVALID)

    def test_registered_adapter_is_created(self):
        registry = AdapterRegistry()
        registry.register(SOURCE_ID, SyntheticFixtureSource)
        self.assertEqual(registry.create(WITH_CREDENTIAL).source_id, SOURCE_ID)

    def test_default_registry_has_real_adapters_and_never_the_fixture(self):
        self.assertEqual(default_registry.available(), ("irsa-spherex", "local-fits"))
        self.assertNotIn(SOURCE_ID, default_registry.available())


if __name__ == "__main__":
    unittest.main()
