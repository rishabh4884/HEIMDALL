import unittest

from heimdall.contracts import ContractViolation, ErrorCode, ErrorInfo
from heimdall.pipeline import ComputationStatus, StageConfig, StageOutcome, config_hash


class ConfigHashTests(unittest.TestCase):
    def test_hash_is_deterministic_and_order_independent(self):
        self.assertEqual(config_hash({"a": 1, "b": "x"}), config_hash({"b": "x", "a": 1}))
        self.assertNotEqual(config_hash({"a": 1}), config_hash({"a": 2}))

    def test_stage_config_hash_includes_stage_and_version(self):
        a = StageConfig(stage_id="s", version="1", params={"p": 1})
        b = StageConfig(stage_id="s", version="2", params={"p": 1})
        self.assertNotEqual(a.hash, b.hash)

    def test_stage_config_rejects_non_scalar_params(self):
        with self.assertRaises(ContractViolation):
            StageConfig(stage_id="s", version="1", params={"p": [1, 2]})


class StageOutcomeTests(unittest.TestCase):
    def test_failed_requires_error_and_no_outputs(self):
        with self.assertRaises(ContractViolation):
            StageOutcome(status=ComputationStatus.FAILED, config_hash="h")
        StageOutcome(status=ComputationStatus.FAILED, config_hash="h",
                     error=ErrorInfo(code=ErrorCode.ANALYSIS_STAGE_FAILED, message="m"))

    def test_not_applicable_distinct_from_failed_and_requires_reason(self):
        with self.assertRaises(ContractViolation):
            StageOutcome(status=ComputationStatus.NOT_APPLICABLE, config_hash="h")
        outcome = StageOutcome(status=ComputationStatus.NOT_APPLICABLE, config_hash="h", reasons=("not applicable here",))
        self.assertIsNone(outcome.error)

    def test_insufficient_inputs_carries_no_outputs(self):
        with self.assertRaises(ContractViolation):
            StageOutcome(status=ComputationStatus.INSUFFICIENT_INPUTS, config_hash="h", reasons=("r",), outputs=(1,))

    def test_completed_cannot_carry_error(self):
        with self.assertRaises(ContractViolation):
            StageOutcome(status=ComputationStatus.COMPLETED, config_hash="h",
                         error=ErrorInfo(code=ErrorCode.INTERNAL, message="m"))


if __name__ == "__main__":
    unittest.main()
