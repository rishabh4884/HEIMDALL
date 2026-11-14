"""Structural invariants of candidates (contract §10.5). No classification is tested here."""

import unittest

from heimdall.contracts import (
    Alternative,
    AlternativeState,
    Applicability,
    Association,
    AttentionLevel,
    AttentionReason,
    AttentionReasonCode,
    Candidate,
    CandidateKind,
    Check,
    CheckState,
    ContractViolation,
    Detection,
    EvidenceKind,
    EvidenceRef,
    UnavailableReason,
    Uncertainty,
    Validity,
    ValidityReason,
    ValidityState,
    ValueClass,
    measured,
    Origin,
    OriginInfo,
)

VALIDATED = Validity(state=ValidityState.VALIDATED_FOR_CONFIG, scope_cell="cell-1", validation_run_ref="run-1")
EVIDENCE = (EvidenceRef(kind=EvidenceKind.CHECK, ref_id="x"),)


def check(state, critical=True, applies=True):
    applicability = Applicability(applies=applies, rule_id=None if applies else "rule-1")
    reason = None if state is CheckState.PASSED else "test reason"
    return Check(check_id=f"check-{state}-{applies}", state=state, critical=critical,
                 applicability=applicability, reason=reason)


def candidate(level, validity=VALIDATED, checks=(), alternatives=(), reason=None, cap=None):
    return Candidate(candidate_id="cand-1", kind=CandidateKind.MOTION, attention_level=level, validity=validity,
                     checks=checks, alternatives=alternatives, attention_reason=reason, cap_reason=cap)


class AttendTests(unittest.TestCase):
    def test_attend_requires_validated_for_config(self):
        with self.assertRaises(ContractViolation):
            candidate(AttentionLevel.ATTEND, validity=Validity.unvalidated(ValidityReason.NO_CONTROL_RUN))
        candidate(AttentionLevel.ATTEND, checks=(check(CheckState.PASSED),))

    def test_attend_not_allowed_with_supported_ordinary_explanation(self):
        alt = Alternative(alt_id="a", description="known object", state=AlternativeState.SUPPORTED, ordinary=True,
                          evidence=EVIDENCE)
        with self.assertRaises(ContractViolation):
            candidate(AttentionLevel.ATTEND, alternatives=(alt,))

    def test_inapplicable_critical_check_does_not_block_attend(self):
        candidate(AttentionLevel.ATTEND, checks=(check(CheckState.NOT_APPLICABLE, applies=False),))


class FailedAndUnresolvedTests(unittest.TestCase):
    def test_failed_critical_check_never_attend_or_watch(self):
        for level in (AttentionLevel.ATTEND, AttentionLevel.WATCH):
            with self.assertRaises(ContractViolation):
                candidate(level, checks=(check(CheckState.FAILED),))

    def test_failed_critical_check_inconclusive_needs_matching_reason(self):
        with self.assertRaises(ContractViolation):
            candidate(AttentionLevel.INCONCLUSIVE, checks=(check(CheckState.FAILED),))
        candidate(AttentionLevel.INCONCLUSIVE, checks=(check(CheckState.FAILED),),
                  reason=AttentionReason(code=AttentionReasonCode.ASSUMPTION_CHECK_FAILED))

    def test_unresolved_critical_check_never_attend_or_watch(self):
        for level in (AttentionLevel.ATTEND, AttentionLevel.WATCH):
            with self.assertRaises(ContractViolation):
                candidate(level, checks=(check(CheckState.NOT_ASSESSABLE),))
        candidate(AttentionLevel.INCONCLUSIVE, checks=(check(CheckState.NOT_ASSESSABLE),),
                  reason=AttentionReason(code=AttentionReasonCode.ASSUMPTION_CHECK_UNRESOLVED))

    def test_failed_takes_precedence_over_unresolved(self):
        checks = (check(CheckState.FAILED), check(CheckState.NOT_ASSESSABLE))
        with self.assertRaises(ContractViolation):
            candidate(AttentionLevel.INCONCLUSIVE, checks=checks,
                      reason=AttentionReason(code=AttentionReasonCode.ASSUMPTION_CHECK_UNRESOLVED))

    def test_explained_allowed_despite_failed_check(self):
        alt = Alternative(alt_id="a", description="artefact", state=AlternativeState.SUPPORTED, ordinary=True,
                          evidence=EVIDENCE)
        candidate(AttentionLevel.EXPLAINED, checks=(check(CheckState.FAILED),), alternatives=(alt,))

    def test_explained_requires_supported_ordinary_explanation(self):
        with self.assertRaises(ContractViolation):
            candidate(AttentionLevel.EXPLAINED)

    def test_non_critical_failure_does_not_force_inconclusive(self):
        candidate(AttentionLevel.WATCH, validity=Validity.not_assessed(), checks=(check(CheckState.FAILED, critical=False),))


class CapAndAnomalyTests(unittest.TestCase):
    def test_cap_reason_only_with_watch_and_unvalidated(self):
        unvalidated = Validity.unvalidated(ValidityReason.OUT_OF_SCOPE)
        candidate(AttentionLevel.WATCH, validity=unvalidated, cap=ValidityReason.OUT_OF_SCOPE)
        with self.assertRaises(ContractViolation):
            candidate(AttentionLevel.INCONCLUSIVE, validity=unvalidated, cap=ValidityReason.OUT_OF_SCOPE)
        with self.assertRaises(ContractViolation):
            candidate(AttentionLevel.WATCH, cap=ValidityReason.OUT_OF_SCOPE)

    def test_anomaly_is_not_applicable_in_v1(self):
        c = candidate(AttentionLevel.WATCH, validity=Validity.not_assessed())
        self.assertEqual(c.anomaly.reason, UnavailableReason.NOT_APPLICABLE)


class DetectionAssociationTests(unittest.TestCase):
    def test_association_requires_two_distinct_detections(self):
        ambiguity = measured(0.0, value_class=ValueClass.QUANTITY,
                             origin=OriginInfo(kind=Origin.DIRECT_MEASUREMENT, procedure_id="test"),
                             uncertainty=Uncertainty.unavailable())
        with self.assertRaises(ContractViolation):
            Association(assoc_id="a", det_ids=("d1",), ambiguity=ambiguity)
        with self.assertRaises(ContractViolation):
            Association(assoc_id="a", det_ids=("d1", "d1"), ambiguity=ambiguity)
        Association(assoc_id="a", det_ids=("d1", "d2"), ambiguity=ambiguity, alternative_groupings=(("d1", "d3"),))

    def test_detection_requires_method_identity(self):
        with self.assertRaises(ContractViolation):
            Detection(det_id="d", obs_id="o", method_id="", method_version="1")


if __name__ == "__main__":
    unittest.main()
