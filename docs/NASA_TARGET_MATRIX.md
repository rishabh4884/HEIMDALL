# NASA target validation matrix

Generated from saved results by `python3 -m heimdall target matrix`. Every statement below is read from the saved investigation JSON; nothing here is hand-written. Test status: PASSED = analysis completed with a reported verdict (a non-detection is a valid verdict); LIMITED_BY_DATA = the observations could not support the test; FAILED/BLOCKED = the engine could not run it.

## NASA-01 Barnard's Star (MOVING_OBJECT)

- Investigation: `tinv:NASA-01:fd9680e5`; execution **COMPLETED**, test **LIMITED_BY_DATA** (motion analysis not assessable: FEWER_THAN_3_USABLE_POINTS, FEWER_THAN_2_EPOCHS, BASELINE_BELOW_20_DAYS)
- Identification: Gaia DR3 4472832130942575872 (Gaia DR3), status RESOLVED
- Position at reference epoch 2016.0: RA 269.448503 Dec 4.739420; PM (-801.6, 10362.4) mas/yr [Gaia DR3 catalogue]
- SPHEREx observations found 130, selected 10, measured 7; MJD 60902.9-61305.6; epochs 6; releases {'spherex_dr1': 84, 'spherex_qr3': 46}; detectors {'2': 67, '3': 63}
- Products retrieved: 10 (origins ['CACHED']); wavelengths measured (um): [1.445, 1.511, 1.512, 1.793, 2.106, 2.307]
- Quality flags in footprints: {'BLOOM': 7, 'OVERFLOW': 7, 'SOURCE': 7, 'SUR_ERROR': 7, 'TRANSIENT': 7}
- Motion: **NOT_ASSESSABLE**; usable 0/10, baseline 0.0 d; fitted v None vs catalogue None arcsec/yr; control null: COMPLETED ['FEWER_THAN_3_USABLE_POINTS', 'FEWER_THAN_2_EPOCHS', 'BASELINE_BELOW_20_DAYS']
- Variability: **NOT_ASSESSABLE** (no comparable series has at least the minimum points and epochs); 0 usable points, 6 epochs, 0 comparable series; period: SAMPLING_ADEQUATE_FOR_SEARCH_ONLY; caveats: ['7 of 7 measured images have saturation/non-linearity flags in the PSF footprint (excluded from tests)', 'single detector in the usable measurements (no cross-detector calibration check)', 'field error scale not estimated (0 comparable series with dof >= 2 (minimum 10)); formal flux errors used']
- Blind pipeline: PARTIAL run `R20261115T063200-a11cf992`; detections near expected target 0/7
- Detectability: {'max': 260.4878270189653, 'median': 157.0733153987506, 'n_ge_3': 6, 'n_ge_5': 6}
- Hypotheses: Insufficient evidence [SUPPORTED]; Moving as the catalogue predicts [NOT_ASSESSABLE]; Stationary source [NOT_ASSESSABLE]; Registration or astrometric artefact [NOT_ASSESSABLE]
- Conclusion: The selected observations do not allow a valid test for Barnard's Star. This is a measurement within stated limits, not a new finding.
- Reproduce: `python3 -m heimdall target run NASA-01`

## NASA-02 Epsilon Indi B (Ba/Bb brown-dwarf binary) (MOVING_OBJECT)

- Supersedes `tinv:NASA-02:c3c6c224` (re-evaluated from the same saved measurements, no new data; changed: analyses.variability; VARIABILITY_DETECTED_WITH_CAVEATS -> SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS). Reason: variability decision-rule correction (D-006): the absent control-star comparison (2 completed control series, minimum 5) was counted as passed
- Investigation: `tinv:NASA-02:0c714738`; execution **COMPLETED**, test **PASSED** (analysis completed with a reported verdict; the verdict itself may be a non-detection)
- Identification: Gaia DR3 6412596012146801152 (Gaia DR3), status RESOLVED
- Position at reference epoch 2016.0: RA 331.076453 Dec -56.793812; PM (3982.0, -2466.8) mas/yr [Gaia DR3 catalogue]
- SPHEREx observations found 177, selected 16, measured 16; MJD 60789.9-61315.5; epochs 10; releases {'spherex_dr1': 129, 'spherex_qr3': 48}; detectors {'2': 93, '5': 84}
- Products retrieved: 16 (origins ['LIVE']); wavelengths measured (um): [1.107, 1.11, 1.135, 1.161, 1.47, 1.562, 1.576, 3.829, 3.862, 3.865, 3.897, 3.931, 4.248, 4.32, 4.341]
- Quality flags in footprints: {'HALO': 1, 'SOURCE': 16, 'TRANSIENT': 1}
- Motion: **MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE**; usable 15/16, baseline 521.3 d; fitted v 4.533364551442398 vs catalogue 4.612582776628829 arcsec/yr; control null: COMPLETED 
- Variability: **SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS** (formal significance only: fewer than 5 completed control series (2), so the control-star false-alarm reference could not be computed and field systematics cannot be excluded); 15 usable points, 10 epochs, 12 comparable series; period: SAMPLING_ADEQUATE_FOR_SEARCH_ONLY; caveats: ['1 of 16 measured images have saturation/non-linearity flags in the PSF footprint (excluded from tests)', 'field error scale not estimated (2 comparable series with dof >= 2 (minimum 10)); formal flux errors used']
- Blind pipeline: FAILED run `None`; detections near expected target None/None
- Detectability: {'max': 193.81238645548117, 'median': 104.08317168932494, 'n_ge_3': 16, 'n_ge_5': 16}
- Hypotheses: Known source moving as its catalogue astrometry predicts [SUPPORTED]; Stationary source (no genuine motion) [DISFAVOURED]; Apparent motion produced by registration/astrometric error [CANNOT_EXCLUDE]; Centroid dominated by a blended neighbour or unresolved companion [CANNOT_EXCLUDE]; Possibly moving uncatalogued source [DISFAVOURED]
- Conclusion: Epsilon Indi B (Ba/Bb brown-dwarf binary) moved across the SPHEREx images by about the amount its catalogue motion predicts. This is a measurement within stated limits, not a new finding.
- Reproduce: `python3 -m heimdall target run NASA-02`

## NASA-03 WISE 0855-0714 (MOVING_OBJECT)

- Investigation: `tinv:NASA-03:097ecfcf`; execution **PARTIAL**, test **LIMITED_BY_DATA** (motion analysis not assessable: FEWER_THAN_3_USABLE_POINTS, FEWER_THAN_2_EPOCHS, BASELINE_BELOW_20_DAYS)
- Identification: CatWISE2020 J085507.96-071439.3 (CatWISE2020), status AMBIGUOUS; 4 CatWISE2020 entries satisfy the rule (a fast mover is often split); the one with the smallest proper-motion error was taken; the others are listed
- Position at reference epoch 2015.4: RA 133.783924 Dec -7.244340; PM (-8021.2, 1035.7) mas/yr [CatWISE2020 catalogue (arcsec/yr converted to mas/yr)]
- SPHEREx observations found 93, selected 10, measured 9; MJD 60800.7-61174.3; epochs 5; releases {'spherex_dr1': 93}; detectors {'5': 48, '6': 45}
- Products retrieved: 9 (origins ['LIVE']); wavelengths measured (um): [3.821, 3.835, 4.047, 4.222, 4.295, 4.418, 4.424, 4.633, 4.825]
- Quality flags in footprints: {'HALO': 1, 'SOURCE': 9, 'TRANSIENT': 1}
- Motion: **NOT_ASSESSABLE**; usable 0/10, baseline 0.0 d; fitted v None vs catalogue None arcsec/yr; control null: NOT_ASSESSABLE ['FEWER_THAN_3_USABLE_POINTS', 'FEWER_THAN_2_EPOCHS', 'BASELINE_BELOW_20_DAYS']
- Variability: **NOT_ASSESSABLE** (no comparable series has at least the minimum points and epochs); 8 usable points, 6 epochs, 6 comparable series; period: SAMPLING_ADEQUATE_FOR_SEARCH_ONLY; caveats: ['1 of 9 measured images have saturation/non-linearity flags in the PSF footprint (excluded from tests)', 'field error scale not estimated (0 comparable series with dof >= 2 (minimum 10)); formal flux errors used']
- Blind pipeline: FAILED run `None`; detections near expected target None/None
- Detectability: {'max': 5.3041358377058145, 'median': 3.408898891939401, 'n_ge_3': 8, 'n_ge_5': 2}
- Hypotheses: Insufficient evidence [SUPPORTED]; Moving as the catalogue predicts [NOT_ASSESSABLE]; Stationary source [NOT_ASSESSABLE]; Registration or astrometric artefact [NOT_ASSESSABLE]
- Conclusion: The selected observations do not allow a valid test for WISE 0855-0714. This is a measurement within stated limits, not a new finding.
- Reproduce: `python3 -m heimdall target run NASA-03`

## NASA-04 RW Aurigae (VARIABLE_STAR)

- Investigation: `tinv:NASA-04:7f40d3aa`; execution **COMPLETED**, test **LIMITED_BY_DATA** (no comparable series has at least the minimum points and epochs)
- Identification: Gaia DR3 156430822114424576 (Gaia DR3), status RESOLVED
- Position at reference epoch 2016.0: RA 76.956081 Dec 30.401205; PM (2.6, -27.4) mas/yr [Gaia DR3 catalogue]
- SPHEREx observations found 111, selected 12, measured 12; MJD 60926.3-61298.3; epochs 3; releases {'spherex_dr1': 74, 'spherex_qr3': 37}; detectors {'2': 55, '5': 56}
- Products retrieved: 12 (origins ['LIVE']); wavelengths measured (um): [1.26, 1.269, 1.43, 1.441, 1.451, 1.462, 4.015, 4.026, 4.207, 4.219, 4.229, 4.24]
- Quality flags in footprints: {'BLOOM': 6, 'OVERFLOW': 12, 'SOURCE': 12, 'SUR_ERROR': 1, 'TRANSIENT': 6}
- Variability: **NOT_ASSESSABLE** (no comparable series has at least the minimum points and epochs); 0 usable points, 4 epochs, 0 comparable series; period: NOT_ASSESSABLE; caveats: ['12 of 12 measured images have saturation/non-linearity flags in the PSF footprint (excluded from tests)', 'Gaia neighbour(s) with optical flux ratio >= 0.1 inside the PSF stamp (e.g. 156430817820015232 at 1.5 arcsec); not deblended', 'single detector in the usable measurements (no cross-detector calibration check)', 'field error scale not estimated (0 comparable series with dof >= 2 (minimum 10)); formal flux errors used']
- Blind pipeline: FAILED run `None`; detections near expected target None/None
- Detectability: {'max': 536.6883750025798, 'median': 393.85374673607316, 'n_ge_3': 12, 'n_ge_5': 12}
- Hypotheses: Insufficient evidence [SUPPORTED]; Intrinsic variability [NOT_ASSESSABLE]; Constant source [NOT_ASSESSABLE]; Calibration or pipeline artefact [NOT_ASSESSABLE]
- Conclusion: The selected observations do not allow a valid test for RW Aurigae. This is a measurement within stated limits, not a new finding.
- Reproduce: `python3 -m heimdall target run NASA-04`

## NASA-05 R Draconis (VARIABLE_STAR)

- Investigation: `tinv:NASA-05:b840445e`; execution **COMPLETED**, test **LIMITED_BY_DATA** (no comparable series has at least the minimum points and epochs)
- Identification: Gaia DR3 1648328016354496384 (Gaia DR3), status RESOLVED
- Position at reference epoch 2016.0: RA 248.167378 Dec 66.755007; PM (-20.6, 6.1) mas/yr [Gaia DR3 catalogue]
- SPHEREx observations found 189, selected 12, measured 10; MJD 60834.8-61214.5; epochs 6; releases {'spherex_dr1': 151, 'spherex_qr3': 38}; detectors {'2': 95, '5': 94}
- Products retrieved: 12 (origins ['LIVE']); wavelengths measured (um): [1.263, 1.405, 1.406, 1.41, 1.551, 4.019, 4.18, 4.181, 4.185, 4.331]
- Quality flags in footprints: {'BLOOM': 10, 'OVERFLOW': 10, 'SOURCE': 10, 'SUR_ERROR': 10, 'TRANSIENT': 5}
- Variability: **NOT_ASSESSABLE** (no comparable series has at least the minimum points and epochs); 0 usable points, 5 epochs, 0 comparable series; period: NOT_ASSESSABLE; caveats: ['10 of 10 measured images have saturation/non-linearity flags in the PSF footprint (excluded from tests)', 'single detector in the usable measurements (no cross-detector calibration check)', 'field error scale not estimated (0 comparable series with dof >= 2 (minimum 10)); formal flux errors used']
- Blind pipeline: PARTIAL run `R20261115T053002-23b4d41f`; detections near expected target 0/10
- Detectability: {'max': 173.53952637269074, 'median': 173.53952637269074, 'n_ge_3': 1, 'n_ge_5': 1}
- Hypotheses: Insufficient evidence [SUPPORTED]; Intrinsic variability [NOT_ASSESSABLE]; Constant source [NOT_ASSESSABLE]; Calibration or pipeline artefact [NOT_ASSESSABLE]
- Conclusion: The selected observations do not allow a valid test for R Draconis. This is a measurement within stated limits, not a new finding.
- Reproduce: `python3 -m heimdall target run NASA-05`
