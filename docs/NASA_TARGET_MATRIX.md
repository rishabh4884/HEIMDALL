# NASA target validation matrix

Generated from saved results by `python3 -m heimdall target matrix`. Every statement below is read from the saved investigation JSON; nothing here is hand-written. Test status: PASSED = analysis completed with a reported verdict (a non-detection is a valid verdict); LIMITED_BY_DATA = the observations could not support the test; FAILED/BLOCKED = the engine could not run it.

## NASA-01 Barnard's Star (MOVING_OBJECT)

- Investigation: `tinv:NASA-01:87c4fe1a`; execution **BLOCKED**, test **FAILED** (discovery failed: RETRIEVAL_FAILED: irsa.ipac.caltech.edu returned HTTP 502)
- Identification: Gaia DR3 4472832130942575872 (Gaia DR3), status RESOLVED
- Position at reference epoch 2016.0: RA 269.448503 Dec 4.739420; PM (-801.6, 10362.4) mas/yr [Gaia DR3 catalogue]
- SPHEREx observations found None, selected None, measured None; MJD n/a-n/a; epochs None; releases None; detectors None
- Products retrieved: 0 (origins []); wavelengths measured (um): None
- Quality flags in footprints: None
- Blind pipeline: None run `None`; detections near expected target None/None
- Detectability: None
- Hypotheses: not run
- Conclusion: No scientific measurement was made: discovery failed: RETRIEVAL_FAILED: irsa.ipac.caltech.edu returned HTTP 502.
- Reproduce: `python3 -m heimdall target run NASA-01 --max-observations 1`

## NASA-02 Epsilon Indi B (Ba/Bb brown-dwarf binary) (MOVING_OBJECT)

- Investigation: `tinv:NASA-02:2fa222e9`; execution **BLOCKED**, test **FAILED** (discovery failed: RETRIEVAL_FAILED: irsa.ipac.caltech.edu returned HTTP 502)
- Identification: Gaia DR3 6412596012146801152 (Gaia DR3), status RESOLVED
- Position at reference epoch 2016.0: RA 331.076453 Dec -56.793812; PM (3982.0, -2466.8) mas/yr [Gaia DR3 catalogue]
- SPHEREx observations found None, selected None, measured None; MJD n/a-n/a; epochs None; releases None; detectors None
- Products retrieved: 0 (origins []); wavelengths measured (um): None
- Quality flags in footprints: None
- Blind pipeline: None run `None`; detections near expected target None/None
- Detectability: None
- Hypotheses: not run
- Conclusion: No scientific measurement was made: discovery failed: RETRIEVAL_FAILED: irsa.ipac.caltech.edu returned HTTP 502.
- Reproduce: `python3 -m heimdall target run NASA-02 --max-observations 1`

## NASA-03 WISE 0855-0714 (MOVING_OBJECT)

- Investigation: `tinv:NASA-03:d3e900bc`; execution **BLOCKED**, test **FAILED** (target not resolved: SERVICE_FAILED: irsa.ipac.caltech.edu returned HTTP 502)
- Identification: None (CatWISE2020), status SERVICE_FAILED; irsa.ipac.caltech.edu returned HTTP 502
- Position at reference epoch n/a: RA n/a Dec n/a; PM (n/a, n/a) mas/yr []
- SPHEREx observations found None, selected None, measured None; MJD n/a-n/a; epochs None; releases None; detectors None
- Products retrieved: 0 (origins []); wavelengths measured (um): None
- Quality flags in footprints: None
- Blind pipeline: None run `None`; detections near expected target None/None
- Detectability: None
- Hypotheses: not run
- Conclusion: No scientific measurement was made: target not resolved: SERVICE_FAILED: irsa.ipac.caltech.edu returned HTTP 502.
- Reproduce: `python3 -m heimdall target run NASA-03 --max-observations 1`

## NASA-04 RW Aurigae (VARIABLE_STAR)

- Investigation: `tinv:NASA-04:ac85654c`; execution **BLOCKED**, test **FAILED** (discovery failed: ARCHIVE_TIMEOUT: request to irsa.ipac.caltech.edu timed out)
- Identification: Gaia DR3 156430822114424576 (Gaia DR3), status RESOLVED
- Position at reference epoch 2016.0: RA 76.956081 Dec 30.401205; PM (2.6, -27.4) mas/yr [Gaia DR3 catalogue]
- SPHEREx observations found None, selected None, measured None; MJD n/a-n/a; epochs None; releases None; detectors None
- Products retrieved: 0 (origins []); wavelengths measured (um): None
- Quality flags in footprints: None
- Blind pipeline: None run `None`; detections near expected target None/None
- Detectability: None
- Hypotheses: not run
- Conclusion: No scientific measurement was made: discovery failed: ARCHIVE_TIMEOUT: request to irsa.ipac.caltech.edu timed out.
- Reproduce: `python3 -m heimdall target run NASA-04 --max-observations 1`

## NASA-05 R Draconis (VARIABLE_STAR)

- Investigation: `tinv:NASA-05:40ab941a`; execution **BLOCKED**, test **FAILED** (discovery failed: ARCHIVE_TIMEOUT: request to irsa.ipac.caltech.edu timed out)
- Identification: Gaia DR3 1648328016354496384 (Gaia DR3), status RESOLVED
- Position at reference epoch 2016.0: RA 248.167378 Dec 66.755007; PM (-20.6, 6.1) mas/yr [Gaia DR3 catalogue]
- SPHEREx observations found None, selected None, measured None; MJD n/a-n/a; epochs None; releases None; detectors None
- Products retrieved: 0 (origins []); wavelengths measured (um): None
- Quality flags in footprints: None
- Blind pipeline: None run `None`; detections near expected target None/None
- Detectability: None
- Hypotheses: not run
- Conclusion: No scientific measurement was made: discovery failed: ARCHIVE_TIMEOUT: request to irsa.ipac.caltech.edu timed out.
- Reproduce: `python3 -m heimdall target run NASA-05 --max-observations 1`
