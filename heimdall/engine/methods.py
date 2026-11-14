"""Registered derivation methods (SCI-32) and helpers for tagged values.

Every ``D`` value the engine emits names a method registered here, with its
declared inputs and roles, so ``MethodRegistry.validate_derived`` accepts it.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..contracts import (
    InputRef,
    InputRole,
    InputSpec,
    MethodRegistry,
    MethodSpec,
    Origin,
    OriginInfo,
    Uncertainty,
    ValueClass,
    ValueObject,
    derived,
    measured,
)

R = InputRole.REQUIRED
O = InputRole.OPTIONAL

_SPECS = (
    MethodSpec(method_id="wcs_wave_lookup", version="1", description="Bilinear lookup in the image WCS-WAVE table (visualisation fidelity, about 1 nm)",
               inputs=(InputSpec(name="pixel_position", role=R), InputSpec(name="wcs_wave_table", role=R))),
    MethodSpec(method_id="cwave_lookup", version="1", description="Per-pixel CWAVE/CBAND from the spectral WCS calibration product",
               inputs=(InputSpec(name="pixel_position", role=R), InputSpec(name="spectral_wcs_calibration", role=R))),
    MethodSpec(method_id="sky_position_from_pixel", version="1", description="Celestial WCS (TAN-SIP) transform of a fitted pixel position",
               inputs=(InputSpec(name="pixel_position", role=R, uncertainty_required=True), InputSpec(name="celestial_wcs", role=R))),
    MethodSpec(method_id="flux_density_from_surface_brightness", version="1",
               description="PSF-fit amplitude (MJy/sr x pixel) times pixel solid angle, converted to microjansky",
               inputs=(InputSpec(name="amplitude", role=R, uncertainty_required=True), InputSpec(name="pixel_solid_angle", role=R))),
    MethodSpec(method_id="weighted_mean_position", version="1", description="Inverse-covariance weighted mean of associated positions",
               inputs=(InputSpec(name="positions", role=R, uncertainty_required=True),)),
    MethodSpec(method_id="variability_chi2", version="1", description="Chi-square of a constant-flux model within one comparable series",
               inputs=(InputSpec(name="series_fluxes", role=R, uncertainty_required=True),)),
    MethodSpec(method_id="linear_motion_delta_chi2", version="1", description="Delta chi-square, stationary versus linear motion",
               inputs=(InputSpec(name="positions", role=R, uncertainty_required=True), InputSpec(name="times", role=R))),
    MethodSpec(method_id="stationarity_chi2", version="1", description="Chi-square of a fixed-position model for one source",
               inputs=(InputSpec(name="positions", role=R, uncertainty_required=True),)),
    MethodSpec(method_id="parallax_motion_fit", version="1", description="Five-parameter position, proper motion and parallax fit",
               inputs=(InputSpec(name="positions", role=R, uncertainty_required=True), InputSpec(name="times", role=R),
                       InputSpec(name="observer_positions", role=R))),
    MethodSpec(method_id="chi2_two_sided_sigma", version="1", description="p-value from chi-square and two-sided Gaussian-equivalent conversion",
               inputs=(InputSpec(name="statistic", role=R), InputSpec(name="dof", role=R))),
    MethodSpec(method_id="band_weighted_mean", version="1", description="Weighted mean flux of spectrum points in one detector band",
               inputs=(InputSpec(name="points", role=R, uncertainty_required=True),)),
    MethodSpec(method_id="spectral_slope_fit", version="1", description="Weighted linear fit of log flux against log wavelength (descriptive only)",
               inputs=(InputSpec(name="points", role=R, uncertainty_required=True),)),
    MethodSpec(method_id="flux_consistency_z", version="1", description="Difference of two flux measurements divided by their combined uncertainty",
               inputs=(InputSpec(name="flux_a", role=R, uncertainty_required=True), InputSpec(name="flux_b", role=R, uncertainty_required=True))),
    MethodSpec(method_id="count", version="1", description="Count of recorded items",
               inputs=(InputSpec(name="items", role=R),)),
)

ENGINE_METHODS = MethodRegistry()
for _spec in _SPECS:
    ENGINE_METHODS.register(_spec)


def source_reported(value, value_class: ValueClass, source: str, *, unit: str | None = None,
                    version: str | None = None, uncertainty: Uncertainty | None = None) -> ValueObject:
    return measured(value, value_class=value_class, unit=unit, uncertainty=uncertainty,
                    origin=OriginInfo(kind=Origin.SOURCE_REPORTED, source=source, version_or_epoch=version))


def direct_measurement(value: float, sigma: float | None, *, procedure_id: str, unit: str | None = None,
                       uncertainty_kind: str = "1-sigma (Fisher information)") -> ValueObject:
    unc = Uncertainty.provided(sigma, uncertainty_kind) if sigma is not None and sigma == sigma else Uncertainty.unavailable()
    return measured(float(value), value_class=ValueClass.QUANTITY, unit=unit, uncertainty=unc,
                    origin=OriginInfo(kind=Origin.DIRECT_MEASUREMENT, procedure_id=procedure_id))


def derived_quantity(value, method_id: str, inputs: Mapping[str, str], *, sigma: float | None = None,
                     unit: str | None = None, value_class: ValueClass = ValueClass.QUANTITY,
                     uncertainty: Uncertainty | None = None, uncertainty_kind: str = "1-sigma (propagated)") -> ValueObject:
    """Build a D value whose inputs and roles come from the registered method."""
    spec = ENGINE_METHODS.get(method_id)
    refs = []
    for name, ref in inputs.items():
        declared = spec.input(name)
        if declared is None:
            raise ValueError(f"input {name} is not declared by method {method_id}")
        refs.append(InputRef(name=name, ref=ref, role=declared.role))
    if uncertainty is None and value_class is ValueClass.QUANTITY:
        uncertainty = Uncertainty.provided(float(sigma), uncertainty_kind) if sigma is not None and sigma == sigma else Uncertainty.unavailable()
    value_out = float(value) if value_class is ValueClass.QUANTITY else value
    result = derived(value_out, value_class=value_class, method_id=method_id, derived_from=tuple(refs), unit=unit,
                     uncertainty=uncertainty)
    ENGINE_METHODS.validate_derived(result)
    return result
