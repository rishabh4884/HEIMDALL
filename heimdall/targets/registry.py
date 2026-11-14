"""Named investigation targets (NASA Space Apps 2026 "Planet X and SPHEREx" test targets) and ad-hoc coordinates.

A ``TargetSpec`` is *configuration*, not a result: it carries a seed position good to arcminutes, the selection
rule that turns the seed into a catalogue-identified source (see ``astrometry.resolve``) and the investigation
kind. Nothing here is a measurement. The investigation engine treats a named target and an arbitrary
coordinate identically once resolved (``TargetSpec.from_coordinates``).

Seed coordinates are approximate positions for catalogue look-up only. They are not used as truth:
the resolver replaces them with the Gaia DR3 / CatWISE2020 entry that satisfies the selection rule, and
records which one was chosen. If the rule matches nothing the target is ``UNRESOLVED`` and the
investigation reports that instead of falling back to the seed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

MOVING = "MOVING_OBJECT"
VARIABLE = "VARIABLE_STAR"


@dataclass(frozen=True)
class TargetSpec:
    target_id: str
    name: str
    category: str  # MOVING_OBJECT | VARIABLE_STAR
    seed_ra_deg: float
    seed_dec_deg: float
    seed_epoch_jyear: float
    # Resolution rule. ``catalogue`` is gaia_dr3 or catwise2020. ``min_parallax_mas`` selects nearby stars
    # (the brightest source is NOT assumed to be the target); ``max_g_mag`` bounds the search for variables.
    catalogue: str = "gaia_dr3"
    search_radius_deg: float = 0.05
    min_parallax_mas: float | None = None
    max_parallax_mas: float | None = None
    min_g_mag: float | None = None  # excludes much brighter neighbours that satisfy the other rules (Epsilon Indi A)
    max_g_mag: float = 21.5
    min_pm_mas_yr: float | None = None  # total proper motion floor (CatWISE look-up of fast movers)
    max_w2_mag: float | None = None
    prefer: str = "brightest"  # brightest | nearest_seed: which of several rule-satisfying sources is the target
    # Optional literature proper motion (mas/yr) used only when the catalogue lacks one. Provenance is mandatory.
    fallback_pm_mas_yr: tuple[float, float] | None = None
    fallback_pm_source: str = ""
    # Cutout side in degrees. Must exceed the expected motion over the baseline plus a registration margin.
    cutout_size_deg: float = 0.10
    context_radius_deg: float = 0.05
    description: str = ""
    expectations: tuple[str, ...] = ()
    nasa_reference_position: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_coordinates(cls, ra_deg: float, dec_deg: float, *, name: str | None = None, category: str = MOVING,
                         radius_deg: float = 0.02) -> TargetSpec:
        """An arbitrary coordinate-addressable target (no catalogue identification is assumed)."""
        ident = f"COORD-{ra_deg:.5f}{dec_deg:+.5f}"
        return cls(target_id=ident, name=name or ident, category=category, seed_ra_deg=ra_deg % 360.0, seed_dec_deg=dec_deg,
                   seed_epoch_jyear=2000.0, search_radius_deg=radius_deg, description="user-supplied coordinates")


NASA_TARGETS: dict[str, TargetSpec] = {
    "NASA-01": TargetSpec(
        target_id="NASA-01", name="Barnard's Star", category=MOVING, seed_ra_deg=269.452, seed_dec_deg=4.693,
        seed_epoch_jyear=2000.0, search_radius_deg=0.40, min_parallax_mas=500.0, max_g_mag=12.0, cutout_size_deg=0.10,
        description="Nearby high-proper-motion star (about 10.3 arcsec/yr).",
        expectations=("catalogue-propagated position at each epoch", "measured displacement versus catalogue prediction",
                      "registration control from stationary Gaia stars")),
    "NASA-02": TargetSpec(
        target_id="NASA-02", name="Epsilon Indi B (Ba/Bb brown-dwarf binary)", category=MOVING, seed_ra_deg=331.044,
        seed_dec_deg=-56.783, seed_epoch_jyear=2000.0, search_radius_deg=0.40, min_parallax_mas=200.0, max_parallax_mas=350.0, min_g_mag=12.0, max_g_mag=21.0,
        prefer="nearest_seed",
        cutout_size_deg=0.10,
        description="Brown-dwarf binary companion of Epsilon Indi A (about 4.7 arcsec/yr system motion). "
                    "Epsilon Indi A is far brighter and lies several arcmin away.",
        expectations=("system identification, not component identification", "resolved-versus-blended assessment at SPHEREx resolution",
                      "contamination by Epsilon Indi A halo")),
    "NASA-03": TargetSpec(
        target_id="NASA-03", name="WISE 0855-0714", category=MOVING, seed_ra_deg=133.795129, seed_dec_deg=-7.245147,
        seed_epoch_jyear=2000.0, catalogue="catwise2020", search_radius_deg=0.10, min_pm_mas_yr=3000.0, max_w2_mag=16.0, cutout_size_deg=0.10,
        fallback_pm_mas_yr=(-8100.0, 700.0),
        fallback_pm_source="approximate literature value (about 8.1 arcsec/yr); used only if CatWISE2020 has no proper motion",
        nasa_reference_position="08h55m10.831s -07d14m42.53s (NASA challenge reference; epoch not stated, so it is NOT assumed to be current)",
        description="Nearby cold brown dwarf moving about 8 arcsec/yr; extremely red (undetected at W1 in most surveys).",
        expectations=("epoch-adjusted search position", "propagated WISE reference comparison", "infrared detectability and SNR",
                      "report whether motion is measurable")),
    "NASA-04": TargetSpec(
        target_id="NASA-04", name="RW Aurigae", category=VARIABLE, seed_ra_deg=76.9565, seed_dec_deg=30.4014,
        seed_epoch_jyear=2000.0, search_radius_deg=0.02, max_g_mag=16.0, cutout_size_deg=0.10,
        description="Young irregular variable (UXOR/T Tauri) with a disc and known deep dimming events.",
        expectations=("uncertainty-aware light curve", "variability significance", "wavelength-dependent behaviour",
                      "calibration/contamination/bandpass checks")),
    "NASA-05": TargetSpec(
        target_id="NASA-05", name="R Draconis", category=VARIABLE, seed_ra_deg=248.175, seed_dec_deg=66.755,
        seed_epoch_jyear=2000.0, search_radius_deg=0.03, max_g_mag=14.0, cutout_size_deg=0.10,
        description="Mira-type long-period variable.",
        expectations=("light curve", "wavelength-dependent variability", "cadence limits", "no period without sampling")),
}


def get_target(ref: str) -> TargetSpec:
    key = ref.strip().upper()
    if key in NASA_TARGETS:
        return NASA_TARGETS[key]
    for t in NASA_TARGETS.values():
        if ref.strip().lower() in t.name.lower():
            return t
    raise KeyError(f"unknown target {ref!r}; known: {', '.join(NASA_TARGETS)}")
