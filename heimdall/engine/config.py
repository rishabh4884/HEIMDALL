"""Versioned engine configuration.

Every numeric default below is PROVISIONAL: an engineering choice made so the
engine can run, not a validated scientific threshold. The full configuration
is hashed into every result, so any change is traceable and invalidates
validation runs made under a different configuration.

Where a default comes from documentation, the source is noted.
"""

from __future__ import annotations

import dataclasses
import math
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..pipeline.interfaces import config_hash

# 1.1.0: context section (Gaia DR3 and JPL SB Ident matching parameters).
# 1.2.0: planetx.link_rate_info_fraction (effective-dof linking criterion).
ENGINE_CONFIG_VERSION = "1.2.0-provisional"

# Pixels with these flags are excluded from fits. Source: IRSA SPHEREx
# Spectrophotometry Tool documentation ("Pixels with any of the following flags
# will be ignored when fitting").
IRSA_FIT_EXCLUDE_FLAGS = (
    "SUR_ERROR",
    "NONFUNC",
    "MISSING_DATA",
    "HOT",
    "COLD",
    "NONLINEAR",
    "PERSIST",
    "BLOOM",
    "SNOWBALL",
    "HALO",
    "SATELLITE_HALO",
)

# Flags propagated as warnings (not excluded). DICHROIC is documented by IRSA
# as calibration-uncertain; TRANSIENT ("Transient detected during SUR", a jump
# within one exposure's ramp) marks cosmic-ray-like events.
WARNING_FLAGS = (
    "TRANSIENT",
    "OVERFLOW",
    "DICHROIC",
    "CROSSTALK",
    "GHOST",
    "GHOST_FPA",
    "GHOST_EXT",
    "STREAK",
    "PHANMISS",
    "PHANTOM",
    "REFERENCE",
)

# Flags that mark saturation or nonlinearity risk.
SATURATION_FLAGS = ("OVERFLOW", "NONLINEAR")

# Flags that mark instrumental single-image artefacts.
ARTEFACT_FLAGS = ("TRANSIENT", "CROSSTALK", "GHOST", "GHOST_FPA", "GHOST_EXT", "STREAK", "PERSIST")

# OUTLIER ("Pixel flagged by Detect Outliers") marks departure from the static sky. A real moving or changing
# source triggers it, so it is recorded as evidence of change,
# never as an artefact, an exclusion or a quality penalty.
CHANGE_FLAGS = ("OUTLIER",)


@dataclass(frozen=True)
class DetectionParams:
    threshold_sigma: float = 5.0
    edge_margin_px: int = 3
    min_separation_px: float = 2.0
    psf_stamp_px: int = 11
    fit_max_iter: int = 10
    max_centroid_shift_px: float = 1.5
    max_masked_fraction: float = 0.25
    footprint_level: float = 0.01  # PSF pixels above this fraction of the peak form the footprint
    background_mesh_px: int = 32
    background_clip_sigma: float = 3.0
    background_max_iter: int = 5
    deblend_passes: int = 2


@dataclass(frozen=True)
class QualityParams:
    exclude_flags: tuple[str, ...] = IRSA_FIT_EXCLUDE_FLAGS
    warning_flags: tuple[str, ...] = WARNING_FLAGS
    background_exclude_flags: tuple[str, ...] = ("SOURCE",)
    require_finast_zero: bool = True
    require_dqa_pass: bool = True
    min_finite_fraction: float = 0.5
    noise_ratio_low: float = 0.8
    noise_ratio_high: float = 1.25
    blend_radius_px: float = 3.0
    grade_min_snr: float = 10.0
    grade_max_chi2_red: float = 3.0
    min_noise_samples: int = 20


@dataclass(frozen=True)
class AssociationParams:
    gate_probability: float = 0.9973
    # Upper end of the documented median fine-astrometry precision for
    # FINAST=0 images (0.1-0.4 arcsec; Akeson et al., arXiv 2511.15823).
    registration_floor_arcsec: float = 0.4
    max_search_arcsec: float = 30.0


@dataclass(frozen=True)
class TemporalParams:
    epoch_gap_days: float = 5.0
    wavelength_tolerance_fraction: float = 0.5  # of the bandwidth at the position
    min_points_variability: int = 3
    min_epochs_variability: int = 2
    min_points_motion: int = 2
    flux_systematic_fraction: float = 0.0


@dataclass(frozen=True)
class SignificanceParams:
    watch_sigma: float = 3.0
    attend_sigma: float = 5.0
    transient_sigma: float = 5.0
    min_quality_grade: str = "B"


@dataclass(frozen=True)
class PlanetXParams:
    # Broad exploratory range for the distant-body hypothesis. Not a
    # literature estimate; set it explicitly for a specific scenario.
    r_min_au: float = 100.0
    r_max_au: float = 2000.0
    link_gof_min_p: float = 0.01
    # Safety bound only (a full search of 42 inputs / 683 pairs needed about 6,000 evaluations, under 1 s). Reaching it
    # makes the search incomplete: LinkingResult.truncated is set and consumers must not treat it as complete.
    max_link_evaluations: int = 200_000
    min_epoch_clusters_for_parallax: int = 2
    min_tracklet_points: int = 2
    min_link_dof: int = 3
    # A tracklet constrains the chain's rate only if its own apparent rate is measured to within this fraction of the
    # weakest annual-parallax rate of the scenario (parallax(r_max) x 2 pi / yr, about 1.8 arcsec/day for 2000 AU).
    # Tracklets that fail this test contribute their position only to the effective degrees of freedom.
    link_rate_info_fraction: float = 1.0

    def __post_init__(self):
        if not (math.isfinite(self.link_rate_info_fraction) and self.link_rate_info_fraction > 0):
            raise ValueError("planetx.link_rate_info_fraction must be a finite number > 0")


@dataclass(frozen=True)
class ValidationParams:
    min_null_trials: int = 100
    ks_min_p: float = 0.01
    completeness_target: float = 0.9
    injection_snr_grid: tuple[float, ...] = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
    injections_per_image: int = 20
    match_radius_px: float = 1.5
    robustness_threshold_delta: float = 0.5
    max_false_detections_per_image: float = 0.1
    seed: int = 20261029
    # Moving-object injection: each experiment re-runs detection on every image a body crosses.
    moving_object_experiments: int = 4
    moving_object_bodies_per_experiment: int = 8
    # Truth attribution in moving-object injection: ambiguous band as a multiple of the association-gate radius.
    attribution_ambiguous_factor: float = 2.0


@dataclass(frozen=True)
class ContextParams:
    gaia_g_max: float = 21.0
    gate_probability: float = 0.9973
    # Used only when JPL SB Ident gives no ephemeris error for a body; reported as assumed.
    sb_assumed_ephemeris_error_arcsec: float = 1.0
    sb_min_interval_s: float = 1.0
    # SPHEREx single-image 5-sigma depth is about AB 18.4 at 1.4 um (real D2 cutouts); asteroids are not more than
    # about 1.5 mag brighter in the near-infrared than in V, so bodies fainter than V = 21 cannot be detected.
    sb_vmag_limit: float = 21.0
    # Gaia stars moving less than this are used as stationary controls for the motion null test.
    quasi_stationary_pm_mas_yr: float = 10.0


@dataclass(frozen=True)
class EngineConfig:
    version: str = ENGINE_CONFIG_VERSION
    detection: DetectionParams = field(default_factory=DetectionParams)
    quality: QualityParams = field(default_factory=QualityParams)
    association: AssociationParams = field(default_factory=AssociationParams)
    temporal: TemporalParams = field(default_factory=TemporalParams)
    significance: SignificanceParams = field(default_factory=SignificanceParams)
    planetx: PlanetXParams = field(default_factory=PlanetXParams)
    validation: ValidationParams = field(default_factory=ValidationParams)
    context: ContextParams = field(default_factory=ContextParams)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def flat(self) -> dict[str, str | int | float | bool]:
        out: dict[str, str | int | float | bool] = {}
        for section, values in self.to_dict().items():
            if not isinstance(values, dict):
                out[section] = values
                continue
            for key, value in values.items():
                out[f"{section}.{key}"] = ",".join(map(str, value)) if isinstance(value, (list, tuple)) else value
        return out

    @property
    def hash(self) -> str:
        return config_hash(self.flat())

    def replace(self, **sections: Any) -> EngineConfig:
        return dataclasses.replace(self, **sections)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EngineConfig:
        kwargs: dict[str, Any] = {}
        types = {f.name: f.type for f in dataclasses.fields(cls)}
        section_classes = {
            "detection": DetectionParams,
            "quality": QualityParams,
            "association": AssociationParams,
            "temporal": TemporalParams,
            "significance": SignificanceParams,
            "planetx": PlanetXParams,
            "validation": ValidationParams,
            "context": ContextParams,
        }
        for key, value in data.items():
            if key not in types:
                raise ValueError(f"unknown configuration section: {key}")
            if key in section_classes:
                section_cls = section_classes[key]
                known = {f.name for f in dataclasses.fields(section_cls)}
                unknown = set(value) - known
                if unknown:
                    raise ValueError(f"unknown parameters in {key}: {sorted(unknown)}")
                cleaned = {k: tuple(v) if isinstance(v, list) else v for k, v in value.items()}
                kwargs[key] = section_cls(**cleaned)
            else:
                kwargs[key] = value
        return cls(**kwargs)

    @classmethod
    def load(cls, path: Path) -> EngineConfig:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
