"""Tests for schema presence, input roles and U propagation (SCI-32, §7.1).

The methods registered here are test-only names with no scientific meaning.
"""

import unittest

from heimdall.contracts import (
    Assumption,
    BlockedCause,
    ContractViolation,
    FieldSpec,
    InputRef,
    InputRole,
    InputSpec,
    MethodRegistry,
    MethodSpec,
    NotEstablishedDetail,
    Origin,
    OriginInfo,
    Presence,
    RecordSchema,
    Tag,
    UnavailableReason,
    Uncertainty,
    ValueClass,
    assumed,
    derived,
    measured,
    unavailable,
)

PROC = OriginInfo(kind=Origin.DIRECT_MEASUREMENT, procedure_id="test-procedure")


def q(value=1.0, uncertainty=None):
    return measured(value, value_class=ValueClass.QUANTITY, origin=PROC,
                    uncertainty=uncertainty or Uncertainty.provided(0.1, "1-sigma"))


def make_registry():
    registry = MethodRegistry()
    registry.register(MethodSpec(
        method_id="test.combine",
        version="1",
        inputs=(
            InputSpec(name="a", role=InputRole.REQUIRED, uncertainty_required=True),
            InputSpec(name="b", role=InputRole.OPTIONAL),
            InputSpec(name="c", role=InputRole.REQUIRED, substitution_allowed=True),
        ),
        has_reduced_information_rule=True,
    ))
    return registry


class MethodSpecTests(unittest.TestCase):
    def test_optional_inputs_require_reduced_information_rule(self):
        with self.assertRaises(ContractViolation):
            MethodSpec(method_id="m", version="1", inputs=(InputSpec(name="x", role=InputRole.OPTIONAL),))

    def test_duplicate_registration_rejected(self):
        registry = make_registry()
        with self.assertRaises(ContractViolation):
            registry.register(registry.get("test.combine"))


class ValidateDerivedTests(unittest.TestCase):
    def test_unregistered_method_fails(self):
        value = derived(1.0, value_class=ValueClass.QUANTITY, method_id="unknown", uncertainty=Uncertainty.unavailable(),
                        derived_from=(InputRef(name="a", ref="r", role=InputRole.REQUIRED),))
        with self.assertRaises(ContractViolation):
            make_registry().validate_derived(value)

    def test_undeclared_input_fails(self):
        value = derived(1.0, value_class=ValueClass.QUANTITY, method_id="test.combine", uncertainty=Uncertainty.unavailable(),
                        derived_from=(InputRef(name="zzz", ref="r", role=InputRole.REQUIRED),))
        with self.assertRaises(ContractViolation):
            make_registry().validate_derived(value)

    def test_role_mismatch_fails(self):
        value = derived(1.0, value_class=ValueClass.QUANTITY, method_id="test.combine", uncertainty=Uncertainty.unavailable(),
                        derived_from=(InputRef(name="a", ref="r", role=InputRole.OPTIONAL),))
        with self.assertRaises(ContractViolation):
            make_registry().validate_derived(value)

    def test_valid_derived_passes(self):
        value = derived(1.0, value_class=ValueClass.QUANTITY, method_id="test.combine", uncertainty=Uncertainty.unavailable(),
                        derived_from=(InputRef(name="a", ref="r1", role=InputRole.REQUIRED),
                                      InputRef(name="c", ref="r2", role=InputRole.REQUIRED)))
        make_registry().validate_derived(value)


class PropagationTests(unittest.TestCase):
    def resolve(self, inputs, substitutions=None):
        return make_registry().resolve_inputs("test.combine", ValueClass.QUANTITY, inputs, substitutions)

    def test_all_inputs_available(self):
        result = self.resolve({"a": q(), "b": q(), "c": q()})
        self.assertTrue(result.computable)
        self.assertIsNone(result.blocked)

    def test_required_input_u_blocks_with_reason(self):
        u = unavailable(value_class=ValueClass.QUANTITY, reason=UnavailableReason.UNAVAILABLE)
        result = self.resolve({"a": u, "c": q()})
        self.assertFalse(result.computable)
        self.assertEqual(result.blocked.tag, Tag.UNAVAILABLE)
        self.assertEqual(result.blocked.detail, NotEstablishedDetail.REQUIRED_INPUT_UNAVAILABLE)
        self.assertEqual([(b.ref, b.cause) for b in result.blocked.blocked_by], [("a", BlockedCause.UNAVAILABLE)])

    def test_optional_input_missing_still_computable(self):
        result = self.resolve({"a": q(), "c": q()})
        self.assertTrue(result.computable)
        self.assertEqual(result.optional_missing, ("b",))

    def test_unrelated_unavailable_value_has_no_effect(self):
        u = unavailable(value_class=ValueClass.QUANTITY, reason=UnavailableReason.UNAVAILABLE)
        result = self.resolve({"a": q(), "c": q(), "unrelated": u})
        self.assertTrue(result.computable)
        self.assertEqual(result.ignored, ("unrelated",))

    def test_omitted_optional_source_field_required_by_calculation(self):
        # "c" may be OPTIONAL in its source record; omitted there, it blocks this calculation only.
        result = self.resolve({"a": q()})
        self.assertFalse(result.computable)
        self.assertEqual([(b.ref, b.cause) for b in result.blocked.blocked_by],
                         [("c", BlockedCause.OMITTED_OPTIONAL_FIELD)])

    def test_required_uncertainty_unavailable_blocks(self):
        result = self.resolve({"a": q(uncertainty=Uncertainty.unavailable()), "c": q()})
        self.assertFalse(result.computable)
        self.assertEqual(result.blocked.blocked_by[0].ref, "a")

    def test_transitive_propagation(self):
        upstream_u = make_registry().resolve_inputs("test.combine", ValueClass.QUANTITY, {}).blocked
        result = self.resolve({"a": upstream_u, "c": q()})
        self.assertEqual(result.blocked.blocked_by[0].cause, BlockedCause.NOT_ESTABLISHED)

    def test_explicit_assumption_substitution(self):
        sub = assumed(1.0, value_class=ValueClass.QUANTITY, assumption=Assumption(rationale="stated in config"),
                      uncertainty=Uncertainty.unavailable())
        result = self.resolve({"a": q()}, substitutions={"c": sub})
        self.assertTrue(result.computable)
        self.assertEqual(result.substituted, ("c",))

    def test_substitution_not_permitted_or_not_assumed(self):
        sub = assumed(1.0, value_class=ValueClass.QUANTITY, assumption=Assumption(rationale="x"),
                      uncertainty=Uncertainty.unavailable())
        with self.assertRaises(ContractViolation):
            self.resolve({"c": q()}, substitutions={"a": sub})
        with self.assertRaises(ContractViolation):
            self.resolve({"a": q()}, substitutions={"c": q()})


class RecordSchemaTests(unittest.TestCase):
    schema = RecordSchema(schema_id="test.record", version="1", fields=(
        FieldSpec(name="req", presence=Presence.REQUIRED, value_class=ValueClass.IDENTIFIER),
        FieldSpec(name="exp", presence=Presence.EXPECTED, value_class=ValueClass.QUANTITY),
        FieldSpec(name="opt", presence=Presence.OPTIONAL, value_class=ValueClass.QUANTITY),
    ))

    def ident(self):
        return measured("x", value_class=ValueClass.IDENTIFIER, origin=OriginInfo(kind=Origin.SOURCE_REPORTED, source="s"))

    def test_missing_required_makes_record_invalid(self):
        self.assertFalse(self.schema.check({}).valid)

    def test_omitted_optional_is_not_a_defect(self):
        check = self.schema.check({"req": self.ident(), "exp": q()})
        self.assertTrue(check.valid)
        self.assertEqual(check.omitted_optional, ("opt",))
        self.assertEqual(check.not_recorded, ())

    def test_missing_expected_is_not_recorded(self):
        check = self.schema.check({"req": self.ident()})
        self.assertTrue(check.valid)
        self.assertEqual(check.not_recorded, ("exp",))

    def test_presence_and_input_role_are_independent(self):
        # An OPTIONAL field required by a calculation: the record stays valid,
        # while the calculation is blocked.
        record = {"req": self.ident(), "exp": q()}
        self.assertTrue(self.schema.check(record).valid)
        registry = MethodRegistry()
        registry.register(MethodSpec(method_id="test.needs_opt", version="1",
                                     inputs=(InputSpec(name="opt", role=InputRole.REQUIRED),)))
        result = registry.resolve_inputs("test.needs_opt", ValueClass.QUANTITY, record)
        self.assertFalse(result.computable)
        self.assertEqual(result.blocked.blocked_by[0].cause, BlockedCause.OMITTED_OPTIONAL_FIELD)
        self.assertTrue(self.schema.check(record).valid)


if __name__ == "__main__":
    unittest.main()
