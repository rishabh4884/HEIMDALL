# Thermal / spectral reference feasibility (2026-10-30)

Question: can Gaia DR3 astrophysical parameters and AllWISE provide a defensible reference for the thermal/red-spectrum hypothesis?
**Conclusion: not implementable and testable now. The hypothesis stays `NOT_ASSESSABLE`; no code path was added.** Facts below were checked against
IRSA TAP (`TAP_SCHEMA` and live counts in the real field RA 150.7817877, Dec 3.3502204, r = 0.15 deg) on 2026-10-30.

## What the catalogues actually provide

| Table (IRSA) | Verified content | Wavelength / units | Quality information |
|---|---|---|---|
| `gaia_dr3_astrophys_param` | `teff_gspphot`, `logg_gspphot`, `ag_gspphot`, `distance_gspphot`, `radius_gspphot` with 16/84% bounds; also `teff_espucd`, FLAME `lum/radius`, MSC, class probabilities (`classprob_dsc_*`) | Teff K, Ag mag; derived from Gaia BP/RP spectrophotometry (0.33-1.05 um), not infrared | `flags_esphs`, `flags_flame`, `flags_msc`, `flags_oa`, `flags_gspspec`; **no `flags_gspphot` column exists in this IRSA table** |
| `allwise_p3as_psd` | `w1..w4mpro`, `w?sigmpro`, `w?snr`, `ph_qual`, `cc_flags`, `ext_flg`, `var_flg`, `w?sat`, `nb`, `na` | W1 3.4, W2 4.6, W3 12, W4 22 um (Vega mag) | per-band quality, artefact, extension and variability flags |
| `gaia_dr3_allwise_best_neighbour` | `source_id`, `allwise_oid`, `angular_distance` [arcsec], `xm_flag`, `number_of_neighbours`, `number_of_mates` | cross-match | match ambiguity counts |

## Measured coverage in the real field (G < 21 and all Gaia sources)

- Gaia sources by G magnitude: `teff_gspphot` exists for most sources brighter than G = 19 (for G 16-18: 25/29, 29/30, 35/39) and for **none at G >= 19** (44 + 80 + 16 sources have no GSP-Phot).
- AllWISE: 994 sources; 166 with W1 and W2 S/N >= 10; 76 with W3 S/N >= 3; 9 with W4 S/N >= 3. Gaia-AllWISE best neighbours: 203.
- Among the 33 HEIMDALL candidate subjects matched to a Gaia star in the saved real run, 7 have G < 19 and 26 have G >= 19: **about 79% would have no GSP-Phot Teff**.
- Spectra: 682 of 682 source spectra in the saved run use calibrated CWAVE/CBAND wavelengths, but **0 span two detectors** (only detector 2 was loaded; a separate 8-cutout detector-5 set exists but was not part of this run).

## What the data could support, and what blocks a comparison

1. A Teff estimate for a *matched, bright* Gaia star is a model-dependent catalogue value (it assumes a stellar atmosphere and extinction). It is a reference quantity, not a SPHEREx measurement.
2. Turning it into an expected SPHEREx spectrum needs a photospheric model grid (not in the repository, no dependency allowed without approval), the per-channel SPHEREx spectral response, and a SPHEREx absolute flux calibration mapping. HEIMDALL records the calibration provenance but does not model absolute calibration, so a flux-level comparison cannot be made defensibly.
3. A thermal/dust excess is normally identified at W3/W4 (12 and 22 um), which lie outside SPHEREx's range (0.75-5 um). Only W1/W2 (3.4 and 4.6 um) fall inside SPHEREx's range, on whichever detectors cover those wavelengths (to be read from the products' wavelength metadata; not verified here). The saved run loaded detector 2 only; its spectra were not compared with WISE W1/W2 and no overlap has been established.
4. Cross-survey comparability is limited by epoch differences (WISE 2010-2011, Gaia 2014-2017, SPHEREx 2025-), variability, blending (SPHEREx pixels are 6.2 arcsec; WISE beam is 6 arcsec at W1), Vega vs AB vs flux-density conventions, and the Gaia-WISE match flags.
5. Selection effects: GSP-Phot is absent for faint and many non-stellar sources, so "no reference" must remain `NOT_ASSESSABLE`, never "no excess".

## Possible future method (not implemented)

For matched sources with a clean GSP-Phot record and AllWISE W1/W2 S/N >= 10 with clean `cc_flags`/`ph_qual`: obtain a SPHEREx spectrum on the detectors covering 3.4 and 4.6 um, convert catalogue magnitudes to flux density with documented zero points, and test whether SPHEREx and WISE agree within the combined uncertainties **before** any photospheric comparison. That first step needs only data, not a model, but needs real multi-detector SPHEREx products (arriving 14 Nov) and a zero-point reference that must be verified, not assumed.

## Decision

No change to behaviour. The `photospheric_reference` prediction remains `NOT_ASSESSABLE` and now names the exact blocking dependency (model grid + band-response integration + absolute-calibration mapping). No temperature, dust composition or spectral class is inferred from band labels or colours.
