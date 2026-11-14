import unittest

from heimdall.contracts import (
    Alternative,
    AlternativeState,
    Applicability,
    Check,
    CheckState,
    ContractViolation,
    EvidenceKind,
    EvidenceRef,
    Statement,
    StatementKind,
    Validity,
    ValidityReason,
    ValidityState,
)

EVIDENCE = (EvidenceRef(kind=EvidenceKind.CHECK, ref_id="check-1"),)


class ValidityTests(unittest.TestCase):
    def test_validated_requires_scope_cell_and_run(self):
        with self.assertRaises(ContractViolation):
            Validity(state=ValidityState.VALIDATED_FOR_CONFIG)
        with self.assertRaises(ContractViolation):
            Validity(state=ValidityState.VALIDATED_FOR_CONFIG, scope_cell="cell-1")
        Validity(state=ValidityState.VALIDATED_FOR_CONFIG, scope_cell="cell-1", validation_run_ref="run-1")

    def test_validated_carries_no_reasons(self):
        with self.assertRaises(ContractViolation):
            Validity(state=ValidityState.VALIDATED_FOR_CONFIG, scope_cell="c", validation_run_ref="r",
                     reasons=(ValidityReason.OUT_OF_SCOPE,))

    def test_unvalidated_requires_reason_and_orders_by_precedence(self):
        with self.assertRaises(ContractViolation):
            Validity(state=ValidityState.UNVALIDATED)
        v = Validity.unvalidated(ValidityReason.OUT_OF_SCOPE, ValidityReason.ASSUMPTION_CHECK_UNRESOLVED,
                                 ValidityReason.ASSUMPTION_CHECK_FAILED)
        self.assertEqual(v.primary_reason, ValidityReason.ASSUMPTION_CHECK_FAILED)
        self.assertEqual(v.reasons[1], ValidityReason.ASSUMPTION_CHECK_UNRESOLVED)

    def test_unvalidated_rejects_misordered_reasons(self):
        with self.assertRaises(ContractViolation):
            Validity(state=ValidityState.UNVALIDATED,
                     reasons=(ValidityReason.OUT_OF_SCOPE, ValidityReason.ASSUMPTION_CHECK_FAILED))

    def test_miscalibrated_requires_criteria_failed_and_cell(self):
        with self.assertRaises(ContractViolation):
            Validity(state=ValidityState.MISCALIBRATED, reasons=(ValidityReason.CRITERIA_FAILED,))
        Validity(state=ValidityState.MISCALIBRATED, reasons=(ValidityReason.CRITERIA_FAILED,), scope_cell="cell-1")

    def test_default_is_not_assessed(self):
        self.assertEqual(Validity.not_assessed().state, ValidityState.NOT_ASSESSED)


class CheckTests(unittest.TestCase):
    def test_not_applicable_requires_rule_id_and_reason(self):
        with self.assertRaises(ContractViolation):
            Applicability(applies=False)
        with self.assertRaises(ContractViolation):
            Check(check_id="gof", state=CheckState.NOT_APPLICABLE, applicability=Applicability(applies=False, rule_id="r1"))
        check = Check(check_id="gof", state=CheckState.NOT_APPLICABLE, critical=True,
                      applicability=Applicability(applies=False, rule_id="r1"), reason="dof is zero for this case")
        self.assertFalse(check.is_applicable_critical)

    def test_inapplicable_rule_cannot_report_pass_or_fail(self):
        for state in (CheckState.PASSED, CheckState.FAILED, CheckState.NOT_ASSESSABLE):
            with self.assertRaises(ContractViolation):
                Check(check_id="c", state=state, applicability=Applicability(applies=False, rule_id="r"), reason="x")

    def test_applicable_check_cannot_be_not_applicable(self):
        with self.assertRaises(ContractViolation):
            Check(check_id="c", state=CheckState.NOT_APPLICABLE, applicability=Applicability(applies=True), reason="x")

    def test_failed_and_not_assessable_require_reason(self):
        for state in (CheckState.FAILED, CheckState.NOT_ASSESSABLE):
            with self.assertRaises(ContractViolation):
                Check(check_id="c", state=state, applicability=Applicability(applies=True))

    def test_applicable_not_assessable_is_still_applicable_critical(self):
        check = Check(check_id="c", state=CheckState.NOT_ASSESSABLE, critical=True,
                      applicability=Applicability(applies=True), reason="input could not be evaluated")
        self.assertTrue(check.is_applicable_critical)


class AlternativeAndStatementTests(unittest.TestCase):
    def test_ruled_out_and_supported_require_evidence(self):
        for state in (AlternativeState.RULED_OUT, AlternativeState.SUPPORTED):
            with self.assertRaises(ContractViolation):
                Alternative(alt_id="a", description="artefact", state=state, ordinary=True)
            Alternative(alt_id="a", description="artefact", state=state, ordinary=True, evidence=EVIDENCE)

    def test_untested_alternative_needs_no_evidence(self):
        Alternative(alt_id="a", description="artefact", state=AlternativeState.PLAUSIBLE_UNTESTED, ordinary=True)

    def test_conclusion_requires_evidence_hypothesis_does_not(self):
        with self.assertRaises(ContractViolation):
            Statement(kind=StatementKind.CONCLUSION, text="supported by checks")
        Statement(kind=StatementKind.CONCLUSION, text="supported by checks", evidence=EVIDENCE)
        Statement(kind=StatementKind.HYPOTHESIS, text="could be an artefact")


if __name__ == "__main__":
    unittest.main()
