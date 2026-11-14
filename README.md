# HEIMDALL

An evidence-first investigation platform for SPHEREx observations.
*Find the signal. Test the evidence. Follow the truth.*

HEIMDALL reports what was measured and what the evidence supports. It never
claims to have detected Planet X or identified a new object. Its best possible
statement about a distant-body candidate is "consistent with the stated
scenario; independent confirmation required".

## Status

The scientific engine is implemented and integrated (no user interface yet):

- **Data access.** The public NASA/IPAC IRSA SPHEREx archive (TAP discovery,
  cutout retrieval, the ePSF, spectral-WCS and solid-angle calibration
  products) and local FITS directories, with typed failures, retries,
  `Retry-After` handling, release de-duplication, a verified cache, offline
  reruns from cached answers and a per-run input manifest.
- **Ingestion and quality control.** SPHEREx Level 2 files (QR2 and QR3 layouts):
  units, WCS, per-pixel wavelengths, flags (both
  header conventions), time, PSF and per-analysis suitability.
- **External context.** Gaia DR3 stars (IRSA TAP) and known small bodies (JPL
  SB Identification, with the spacecraft's own position), with explicit query
  states so that a failed query never looks like "no match".
- **Measurement.** Matched-filter detection, PSF-fit photometry and
  astrometry with deblending, and forced photometry.
- **Analysis.** Association with explicit ambiguity, spectra, epochs,
  comparable series, variability, stationarity and parallax-motion fits,
  transient tests and moving-object linking.
- **Interpretation.** Versioned reasoning rules (checks, competing
  explanations, known-object and catalogue evidence, single-image
  independence, attention levels, follow-up ranking) and the Planet X
  hypothesis evaluation.
- **Validation.** Injection-recovery, null tests (with Gaia stationary
  controls), moving-object injection and linking completeness, PSF-mismatch
  sensitivity, noise calibration, robustness, reproducibility, performance
  bounds and independent numerical checks, plus a validation registry that
  controls validity states.
- **Outputs.** A sky-map data contract with a spatial index and a minimal SVG
  view, time-ordered playback sequences, and a CLI and Python API.

What has and has not been checked against real data is recorded in
`docs/VERIFICATION_REAL_DATA.md` and `docs/COMPLETENESS_MATRIX.md`. Passing
software tests are not scientific validation.

## Requirements

- Python 3.11 or later
- numpy, scipy and astropy
- certifi (recommended) for TLS certificate verification

## Quick start

```bash
cd HEIMDALL
python3 -m heimdall version
python3 -m heimdall validate --self-check          # independent numerical checks
python3 -m heimdall demo                           # SYNTHETIC end-to-end demonstration (~90 s)
python3 -m heimdall coverage --source irsa-spherex --ra 150.7817877 --dec 3.3502204 --radius 0.05 --detectors 2
python3 -m heimdall investigate --source irsa-spherex --ra 150.7817877 --dec 3.3502204 --radius 0.05 \
    --detectors 2 --max-observations 16 --validate
```

The demo uses synthetic products that are labelled synthetic everywhere
(files, records, sky map watermark). The `investigate` example downloads real
public cutouts (about 5 MB each) into the git-ignored `.heimdall/` cache.

See `docs/USAGE.md` for every command, configuration and a walkthrough.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

249 tests (the 5 live tests against IRSA and JPL are skipped unless
`HEIMDALL_REAL_DATA_TESTS=1`). They cover:
- numerical reference checks (including Gaia epoch propagation against astropy);
- ingestion and QC (both flag conventions), and detection;
- association, temporal and spectral analysis;
- reasoning, Planet X, context and astrometric-error rules, and validation logic;
- adapters and external services (fake transports, no network), offline mode;
- end-to-end pipeline runs on synthetic products, reproducibility and the CLI;
- performance and memory bounds.

## Documentation

| File | Contents |
|---|---|
| `docs/ARCHITECTURE.md` | modules, dependency rules, pipeline, design decisions |
| `docs/SCIENCE_METHODS.md` | every method explained, with formulas, assumptions and failure conditions |
| `docs/USAGE.md` | setup, CLI reference, configuration, walkthrough |
| `docs/OUTPUT_SCHEMA.md` | investigation, sky-map, playback and validation record schemas (the UI contract) |

## Secrets

- Credentials live only in environment variables or a local `.env`, which is
  git-ignored. `.env.example` holds placeholders only. The public IRSA archive
  needs no credential.
- Secrets are wrapped in `Secret`, which never prints its value, cannot be
  serialized or pickled, and is redacted from logs.
- Downloaded products, runs and validation records live in `.heimdall/`
  (git-ignored). Never commit data without checking licensing.
