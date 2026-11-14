# SPHEREx product fidelity: verified versus assumed

Verification date: 2026-10-29. Sources: the public IRSA archive (no credentials), IRSA documentation and tutorials,
the JPL SB Identification API documentation, and real files inspected during development:

- QR2 detector-2 cutouts of the neutral IRSA tutorial field (16 small cutouts and 75 larger ones);
- 8 QR2 detector-5 (MWIR) cutouts of the same field;
- one QR3 detector-2 science product (`2026W36_1A_0001_1D2`, pipeline 7.0.5);
- the QR3 ePSF calibration products for D2 and D5, and the QR2 spectral-WCS and solid-angle calibration products for D2.

No credentialed or non-public product was used. `VERIFICATION_REAL_DATA.md` gives the commands and results.

## 1. Archive and access (verified live)

| Item | Verified value |
|---|---|
| TAP endpoint | `https://irsa.ipac.caltech.edu/TAP/sync`, ADQL, CSV output |
| Tables | `spherex.plane`, `spherex.artifact`, `spherex.observation` (columns read from `TAP_SCHEMA.columns`) |
| Collections (observation counts) | `spherex_qr2` 186,985; `spherex_qr2_deep` 46,015; `spherex_qr2_cal` 14; `spherex_qr3` 26,435; `spherex_qr3_deep` 6,478; `spherex_qr3_cal` 20 |
| QR3 coverage | no QR3 plane contained the neutral field at inspection |
| Footprint test | `1 = CONTAINS(POINT('ICRS', ra, dec), p.poly)` |
| `plane.poly` in CSV output | hex-encoded EWKB (PostGIS) polygon of (RA, Dec) doubles with an SRID flag, not text. HEIMDALL takes footprints from each image's WCS instead |
| Band naming | `energy_bandpassname` = `SPHEREx-D1` to `SPHEREx-D6` |
| Wavelength bounds | `energy_bounds_lower/upper` in metres |
| Time bounds | `time_bounds_lower/upper` (MJD) |
| Release / version | `observation.collection` (e.g. `spherex_qr2`); `plane.provenance_version` (6.4, 6.5.6, 6.5.7 in one field) |
| Checksums | `artifact.contentchecksum` = `md5:<hex>` (full product), `contentlength` in bytes |
| Empty metric columns | `quality_flag`, `metrics_fluxdensitylimit`, `metrics_maglimit` were empty for the inspected rows |
| Cutouts | `https://irsa.ipac.caltech.edu/<uri>?center=<ra>,<dec>d&size=<deg>` returns the same MEF layout. The QR2 101x101x121 PSF cube makes each cutout about 5 MB; each took 16 s to 3 min |
| Coverage clustering | neutral field: epochs of a few days about six months apart |
| Gaia DR3 at IRSA | table `gaia_dr3_source`; `ra`, `dec` (deg), `ra_error`, `dec_error`, `parallax`, `pmra`, `pmdec` and their errors (mas, mas/yr), `ref_epoch` (yr), `phot_g_mean_mag` |

## 2. Level 2 file layout (verified on real files)

| Extension | Verified content |
|---|---|
| PRIMARY | `VERSION` = pipeline version (e.g. `6.4+psffix1`, `7.0.5`) |
| IMAGE | float32 big-endian, `BUNIT = 'MJy / sr'`, `RA---TAN-SIP`/`DEC--TAN-SIP` (SIP order 3), `RADESYS = ICRS`; `DETECTOR`, `OBSID`, `TIMESYS = UTC`, `MJD-OBS`/`MJD-BEG` (start), `MJD-AVG` (midpoint), `XPOSURE`, `FINAST` (0 = good), `L2DQAFLG` (`Pass`), `PSF_FWHM`, `OMEGA_MEDIAN`/`OMEGA_STDDEV` (arcsec^2), `X_SC/Y_SC/Z_SC` (km) and `VX_SC/VY_SC/VZ_SC` (km/s), `HISTORY [CALIB] <uuid>` |
| Alternate WCS `A` | `LINEAR`, "0-based active pixel coordinates": parent = CRVAL + (x0 + 1 - CRPIX) |
| Alternate WCS `W` | `WAVE-TAB` lookup in `WCS-WAVE`; 11x11 control points over 1-2040; values = (wavelength, bandwidth) in um. SIP must be disabled when evaluating it |
| FLAGS, QR2 (6.x) | int32; bits defined by `MP_<NAME>` keywords: TRANSIENT 0, OVERFLOW 1, SUR_ERROR 2, PHANTOM 4, REFERENCE 5, NONFUNC 6, DICHROIC 7, MISSING_DATA 9, HOT 10, COLD 11, FULLSAMPLE 12, PHANMISS 14, NONLINEAR 15, PERSIST 17, OUTLIER 19, SOURCE 21 |
| FLAGS, QR3 (7.0.5) | no `MP_` keywords. Planes defined by `MSKNnnnn` (name), `MSKMnnnn` (= 1 << nnnn) and `MSKDnnnn` (description): 22 planes. All 14 names shared with QR2 have the same bit; QR3 adds CROSSTALK 20, GHOST 22, GHOST_FPA 23, GHOST_EXT 24, STREAK 25, BLOOM 26, SNOWBALL 27, HALO 28 and does not define PHANTOM or REFERENCE |
| Flag meanings used by the reasoning | TRANSIENT = "Transient detected during SUR" (a jump within one exposure's up-the-ramp samples); OUTLIER = "Pixel flagged by Detect Outliers" |
| VARIANCE | float32, `BUNIT = 'MJy2 / sr2'` |
| ZODI | float32, MJy/sr (model; not subtracted from IMAGE) |
| PSF (QR1/QR2) | 101x101x121 float32, `OVERSAMP = 10`, zone centres `XCTR_i/YCTR_i`; each plane sums to 1 |
| EPSF (QR3 science product) | embedded table replacing the PSF cube: 441 rows, columns `BINX, BINY, XCENTER, YCENTER, XWIDTH, YWIDTH, NSTAR, NEFF_MEAN, CWAVE, CBAND, EPSF (33x33)`, `OVSMPX = OVSMPY = 5`, `DETCOORD = 'sky'` |
| ePSF calibration (QR3) | same table layout; `DETCOORD = 'sky'` ("MWIR pixels flipped on X to match SWIR view") |
| Spectral WCS calibration (QR2, `cal-wcs-v4-2025-254`) | `CWAVE` and `CBAND` images, 2040x2040, `BUNIT = 'um'`, `DETECTOR = 2`; D2 CWAVE 1.0853-1.6678 um (median 1.3429), CBAND 0.0237-0.0408 um; plus a `WCS-WAVE` table |
| Solid-angle map (QR2, `cal-sapm-v2-2025-164`) | `IMAGE`, 2040x2040, `BUNIT = 'arcsec2'`; D2 37.413-38.04 (median 37.695, matching `OMEGA_MEDIAN` 37.70) |

Measured on real cutouts:
- median background 0.28 MJy/sr and per-pixel sigma about 0.019 MJy/sr (D2);
- WCS pixel area 37.67 arcsec^2 against `OMEGA_MEDIAN` 37.70 (0.06% apart);
- pixel-scale noise ratio 1.06-1.07.

## 3. Engine choices that rest on these facts

- Units, flag vocabularies and the time keyword are read from each product, never assumed. Products without them
  are excluded or marked unsuitable. Both flag conventions are parsed; a QR3 plane whose mask is not `1 << n` is
  ignored and reported.
- OUTLIER is treated as evidence of a change on the sky, not as an artefact: the two OUTLIER-flagged "transients"
  of the first real-data run are asteroid 368 Haidea at its JPL-predicted positions (`VERIFICATION_REAL_DATA.md`).
- Wavelengths come from `WCS-WAVE` (visualisation accuracy, about 1 nm, per IRSA) unless the spectral-WCS
  calibration is fetched (`--calibration-products full`). Every point records which was used.
- Pixel solid angles come from the solid-angle map with `--calibration-products full`, otherwise `OMEGA_MEDIAN`.
- Calibration arrays are read once per process and shared read-only by every image.
- The ePSF (embedded or calibration product) is preferred over the legacy PSF cube, as IRSA advises.
- MWIR ePSF orientation: used as delivered. On 8 real D5 cutouts the median reduced chi-square of converged
  SNR >= 20 fits was 1.16 as delivered (14 stars), 1.25 with the arrays mirrored in X, 1.17 with the zone centres
  mirrored and 1.22 with both. The ePSF's left-right asymmetry is 10% (L1). Weak evidence: one field, one MWIR
  detector, 14 stars.
- `X_SC/Y_SC/Z_SC` and `VX_SC/VY_SC/VZ_SC` are passed to JPL SB Ident as a geocentric ICRF state vector, as that
  API documents. The header does not state the axes; equatorial axes are assumed.

## 4. Not verified (open)

| Item | Why it matters | Status |
|---|---|---|
| ePSF orientation for D4 and D6 | PSF fits on those detectors could be mirrored | only D5 checked (above) |
| Spectral-WCS and solid-angle products for QR3 | QR3 images with QR2 calibration | only the QR2 versions are configured; a warning is recorded when they are applied to QR3 images |
| Legacy PSF cube: pre- or post-pixel convolution | the integration onto pixels could double-count the pixel response | treated as an approximation (`approximation: true`) |
| `X_SC` frame axes | parallax of nearby bodies; known-object predictions | assumed equatorial (geocentric ICRF) |
| Flag planes absent from a product (QR2: CROSSTALK, GHOST*, STREAK, BLOOM, SNOWBALL, HALO; QR3: PHANTOM, REFERENCE; both: SATELLITE_HALO) | artefact policy | reported as undefined per product; never assumed |
| Absolute flux calibration accuracy | photometric systematics between releases or versions | not modelled (`flux_systematic_fraction` = 0 by default); version differences are flagged |
| QR3 deep fields, D1, D3, D4, D6 science products | generality | not inspected |
