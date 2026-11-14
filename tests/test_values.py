import unittest

from heimdall.contracts import (
    Assumption,
    BlockedBy,
    BlockedCause,
    ContractViolation,
    InputRef,
    InputRole,
    NotEstablishedDetail,
    Origin,
    OriginInfo,
    Tag,
    UnavailableReason,
    Uncertainty,
    UncertaintyStatus,
    ValueClass,
    ValueObject,
    assumed,
    derived,
    measured,
    unavailable,
)

ARCHIVE = OriginInfo(kind=Origin.SOURCE_REPORTED, source="example-archive", version_or_epoch="v0")
PROCEDURE = OriginInfo(kind=Origin.DIRECT_MEASUREMENT, procedure_id="example-procedure")


class TagScopeTests(unittest.TestCase):
    def test_measured_requires_origin(self):
        with self.assertRaises(ContractViolation):
            ValueObject(
                tag=Tag.MEASURED, value_class=ValueClass.IDENTIFIER, value="x", uncertainty=Uncertainty.not_applicable()
            )

    def test_source_reported_origin_requires_source(self):
        with self.assertRaises(ContractViolation):
            OriginInfo(kind=Origin.SOURCE_REPORTED)

    def test_direct_measurement_origin_requires_procedure(self):
        with self.assertRaises(ContractViolation):
            OriginInfo(kind=Origin.DIRECT_MEASUREMENT)

    def test_accessible_name_for_m(self):
        value = measured("id-1", value_class=ValueClass.IDENTIFIER, origin=ARCHIVE)
        self.assertEqual(value.accessible_name, "Measured or source-reported")

    def test_derived_requires_method_and_inputs(self):
        ref = InputRef(name="a", ref="m-1", role=InputRole.REQUIRED)
        with self.assertRaises(ContractViolation):
            ValueObject(tag=Tag.DERIVED, value_class=ValueClass.COUNT, value=1, uncertainty=Uncertainty.not_applicable(),
                        derived_from=(ref,))
        with self.assertRaises(ContractViolation):
            ValueObject(tag=Tag.DERIVED, value_class=ValueClass.COUNT, value=1, uncertainty=Uncertainty.not_applicable(),
                        method_id="m")

    def test_derived_must_not_carry_origin(self):
        with self.assertRaises(ContractViolation):
            ValueObject(
                tag=Tag.DERIVED, value_class=ValueClass.COUNT, value=1, uncertainty=Uncertainty.not_applicable(),
                method_id="m", derived_from=(InputRef(name="a", ref="r", role=InputRole.REQUIRED),), origin=ARCHIVE,
            )

    def test_assumed_requires_source_or_rationale(self):
        with self.assertRaises(ContractViolation):
            Assumption()
        value = assumed("stationary", value_class=ValueClass.CATEGORICAL, assumption=Assumption(rationale="stated"))
        self.assertEqual(value.tag, Tag.ASSUMED)

    def test_unknown_origin_is_u_not_established(self):
        value = unavailable(
            value_class=ValueClass.QUANTITY,
            reason=UnavailableReason.NOT_ESTABLISHED,
            detail=NotEstablishedDetail.ORIGIN_NOT_ESTABLISHED,
        )
        self.assertEqual(value.tag, Tag.UNAVAILABLE)
        self.assertIsNone(value.value)


class UnavailableTests(unittest.TestCase):
    def test_u_requires_null_value_and_reason(self):
        with self.assertRaises(ContractViolation):
            ValueObject(tag=Tag.UNAVAILABLE, value_class=ValueClass.QUANTITY, value=0.0,
                        reason=UnavailableReason.UNAVAILABLE)
        with self.assertRaises(ContractViolation):
            ValueObject(tag=Tag.UNAVAILABLE, value_class=ValueClass.QUANTITY)

    def test_null_value_with_non_u_tag_fails(self):
        with self.assertRaises(ContractViolation):
            ValueObject(tag=Tag.MEASURED, value_class=ValueClass.QUANTITY, value=None,
                        uncertainty=Uncertainty.unavailable(), origin=ARCHIVE)

    def test_not_established_requires_detail(self):
        with self.assertRaises(ContractViolation):
            unavailable(value_class=ValueClass.QUANTITY, reason=UnavailableReason.NOT_ESTABLISHED)

    def test_detail_only_with_not_established(self):
        with self.assertRaises(ContractViolation):
            unavailable(value_class=ValueClass.QUANTITY, reason=UnavailableReason.UNAVAILABLE,
                        detail=NotEstablishedDetail.CALCULATION_FAILED)

    def test_required_input_unavailable_needs_blocked_by(self):
        with self.assertRaises(ContractViolation):
            unavailable(value_class=ValueClass.QUANTITY, reason=UnavailableReason.NOT_ESTABLISHED,
                        detail=NotEstablishedDetail.REQUIRED_INPUT_UNAVAILABLE)
        value = unavailable(
            value_class=ValueClass.QUANTITY,
            reason=UnavailableReason.NOT_ESTABLISHED,
            detail=NotEstablishedDetail.REQUIRED_INPUT_UNAVAILABLE,
            blocked_by=(BlockedBy(ref="x", cause=BlockedCause.OMITTED_OPTIONAL_FIELD),),
        )
        self.assertEqual(len(value.blocked_by), 1)

    def test_distinct_unavailability_reasons(self):
        reasons = {
            unavailable(value_class=ValueClass.CATEGORICAL, reason=r).reason
            for r in (UnavailableReason.UNAVAILABLE, UnavailableReason.NOT_RECORDED,
                      UnavailableReason.NOT_APPLICABLE, UnavailableReason.NOT_ASSESSABLE)
        }
        self.assertEqual(len(reasons), 4)


class UncertaintyTests(unittest.TestCase):
    def test_quantity_must_state_uncertainty(self):
        with self.assertRaises(ContractViolation):
            measured(1.5, value_class=ValueClass.QUANTITY, origin=PROCEDURE)

    def test_measured_quantity_with_unavailable_uncertainty_stays_m(self):
        value = measured(1.5, value_class=ValueClass.QUANTITY, origin=PROCEDURE, uncertainty=Uncertainty.unavailable())
        self.assertEqual(value.tag, Tag.MEASURED)
        self.assertEqual(value.uncertainty.status, UncertaintyStatus.UNAVAILABLE)

    def test_categorical_and_identifier_default_to_not_applicable(self):
        for vc, v in ((ValueClass.CATEGORICAL, "state"), (ValueClass.IDENTIFIER, "id")):
            value = measured(v, value_class=vc, origin=ARCHIVE)
            self.assertEqual(value.uncertainty.status, UncertaintyStatus.NOT_APPLICABLE)
            self.assertEqual(value.tag, Tag.MEASURED)

    def test_quantity_cannot_be_not_applicable(self):
        with self.assertRaises(ContractViolation):
            measured(1.0, value_class=ValueClass.QUANTITY, origin=PROCEDURE, uncertainty=Uncertainty.not_applicable())

    def test_categorical_cannot_have_provided_uncertainty(self):
        with self.assertRaises(ContractViolation):
            measured("s", value_class=ValueClass.CATEGORICAL, origin=ARCHIVE, uncertainty=Uncertainty.provided(1.0, "1-sigma"))

    def test_provided_uncertainty_requires_sigma_and_kind(self):
        with self.assertRaises(ContractViolation):
            Uncertainty(status=UncertaintyStatus.PROVIDED, sigma=1.0)
        with self.assertRaises(ContractViolation):
            Uncertainty(status=UncertaintyStatus.PROVIDED, sigma=-1.0, kind="1-sigma")
        with self.assertRaises(ContractViolation):
            Uncertainty(status=UncertaintyStatus.UNAVAILABLE, sigma=1.0)

    def test_measured_zero_is_a_value_not_missing(self):
        value = measured(0.0, value_class=ValueClass.QUANTITY, origin=PROCEDURE,
                         uncertainty=Uncertainty.provided(0.1, "1-sigma"))
        self.assertTrue(value.is_available)
        self.assertEqual(value.value, 0.0)


class ValueTypeTests(unittest.TestCase):
    def test_quantity_rejects_non_finite_and_bool(self):
        for bad in (float("nan"), float("inf"), True, "1.0"):
            with self.assertRaises(ContractViolation):
                measured(bad, value_class=ValueClass.QUANTITY, origin=PROCEDURE, uncertainty=Uncertainty.unavailable())

    def test_count_must_be_non_negative_int(self):
        with self.assertRaises(ContractViolation):
            derived(-1, value_class=ValueClass.COUNT, method_id="m",
                    derived_from=(InputRef(name="a", ref="r", role=InputRole.REQUIRED),))


if __name__ == "__main__":
    unittest.main()
