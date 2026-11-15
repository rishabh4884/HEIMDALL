# Using the HEIMDALL engine

Everything below works without the future UI. Commands print readable text by
default and JSON with `--json`. Exit codes: 0 success, 1 operation failed,
2 usage, input or configuration error.

## Setup

```bash
cd HEIMDALL
python3 -m pip install numpy scipy astropy certifi   # only if not already installed
cp .env.example .env                                  # optional
python3 -m heimdall check
```

| Variable | Meaning | Default |
|---|---|---|
| `HEIMDALL_DATA_SOURCE` | default adapter (`irsa-spherex` or `local-fits`) | none |
| `HEIMDALL_SOURCE_CREDENTIAL` | credential, if a source needs one (IRSA does not) | none |
| `HEIMDALL_CACHE_DIR` | downloaded products (checksummed) | `.heimdall/cache` |
| `HEIMDALL_STATE_DIR` | runs and validation records | `.heimdall/state` |
| `HEIMDALL_LOCAL_FITS_DIR` | directory for `local-fits` | none |
| `HEIMDALL_REQUEST_TIMEOUT_S`, `HEIMDALL_MAX_RETRIES` | network behaviour | 60, 3 |
| `HEIMDALL_LOG_LEVEL` | logging | INFO |

`.heimdall/` is git-ignored.

## Commands

| Command | Purpose |
|---|---|
| `sources [--json]` | registered sources and their descriptions |
| `access SOURCE [--local-root DIR]` | check reachability and permissions without retrieving data |
| `coverage --source S --ra RA --dec DEC --radius R [--detectors 1,2] [--collections spherex_qr2] [--mjd-start M] [--mjd-end M] [--json]` | observations covering a region, per detector and per epoch (no pixels) |
| `investigate --source S --ra --dec --radius [filters] [--max-observations N] [--cutout-size DEG] [--validate] [--no-calibration] [--calibration-products epsf\|full] [--no-context] [--config FILE] [--out DIR] [--no-svg] [--quiet]` | full pipeline; saves a run directory. `full` adds the CWAVE/CBAND spectral WCS and the solid-angle map (about 50 MB per detector, read once). Without `--no-context`, real-data runs query Gaia DR3 (IRSA) and JPL SB Ident |
| `runs` | stored runs |
| `inspect RUN [--section NAME] [--id ID] [--json]` | sections: summary, stages, observations, epochs, detections, sources, associations, transients, tracks, candidates, planet_x, validation, playback, provenance, unavailable, events, failed, warnings |
| `validate RUN` / `validate --self-check` | re-run with every validation procedure, or run the independent numerical checks |
| `reproduce RUN` | re-run with the recorded request and configuration and compare results hashes |
| `skymap RUN [--svg FILE] [--viewport RA DEC FOV] [--json]` | sky-map data contract (and SVG) |
| `playback RUN [--mjd-start] [--mjd-end]` | time-ordered frames, optionally within a time window |
| `export RUN --what candidates\|detections\|sources\|observations\|validation --format json\|csv [--out FILE]` | machine-readable export |
| `config show\|hash\|template [--config FILE]` | engine configuration and its hash |
| `cache list\|clear [--older-than-days N]` | IRSA product cache |
| `demo [--small] [--no-validate] [--out DIR]` | SYNTHETIC end-to-end demonstration |

`RUN` is a run id (under `<state_dir>/runs`) or a run directory.

## Configuration

```bash
python3 -m heimdall config template > my_config.json   # edit values
python3 -m heimdall config hash --config my_config.json
python3 -m heimdall investigate ... --config my_config.json
```

Sections: `detection`, `quality`, `association`, `temporal`, `significance`,
`planetx` (for example the scenario `r_min_au`/`r_max_au`), `validation` (for
example `moving_object_experiments`) and `context` (Gaia magnitude limit, match
gate, JPL magnitude limit, assumed ephemeris error). Unknown keys are rejected.
The full configuration and its hash are stored in every result. Validation
applies only to the exact hash it ran under.

Offline and reproducible reruns: every archive discovery answer, product,
calibration file, Gaia answer and JPL answer is cached. With
`HEIMDALL_OFFLINE=1` no network request is made; anything not cached fails
explicitly (`ARCHIVE_UNREACHABLE`, or a context query `OFFLINE_NOT_CACHED`),
never silently. Each run directory has a `manifest.json` listing every input
with its SHA-256.

## Walkthrough 1: synthetic demonstration (offline)

```bash
python3 -m heimdall demo
```

This generates 24 SYNTHETIC SPHEREx-layout images (2 detectors, 3 epochs, 4
visits): 140 stars, a variable, a transient, cosmic rays and a body at
1200 AU. It then runs the full pipeline with validation. Expected outcome:

- the variable becomes `ATTEND`, because its synthetic variability cell was
  validated;
- the transient becomes `WATCH`, capped because the false-detection cell has
  too few images;
- the body becomes track `T0000` at 1200.2 AU (2-sigma interval 1198.4-1202.0).
  It is `INCONCLUSIVE` (the known-object check is unresolved) with Planet X
  status `CONSISTENT_REQUIRES_CONFIRMATION`;
- cosmic rays become `EXPLAINED` as image artefacts;
- the motion null test is `MISCALIBRATED`: the registration floor is too
  conservative for synthetic images;
- the photometric error scale is 1.016 +/- 0.024 (1174 series), as expected
  for synthetic noise that matches its stated errors;
- moving-object injection recovers 17 of 31 covered bodies (95% interval
  0.38-0.71) with no false links: 0/4 at 100-271 AU, 3/11 at 271-737 AU and
  14/16 at 737-2000 AU. Closer bodies move too far between visits to form
  tracklets, so the region statement is `COMPLETENESS_LIMITED` and says so;
- context is `NOT_APPLICABLE`: real catalogues do not describe a synthetic sky.

Then:

```bash
python3 -m heimdall inspect <run_id> --section candidates --json | less
python3 -m heimdall inspect <run_id> --section tracks --json
python3 -m heimdall skymap <run_id> --svg demo.svg
python3 -m heimdall reproduce <run_id>      # identical results hash
```

## Walkthrough 2: real public SPHEREx data

```bash
python3 -m heimdall access irsa-spherex
python3 -m heimdall coverage --source irsa-spherex --ra 150.7817877 --dec 3.3502204 --radius 0.05 --detectors 2   # local index, or a durable INDEX job (add --live for a direct archive query)
python3 -m heimdall investigate --source irsa-spherex --ra 150.7817877 --dec 3.3502204 --radius 0.05 \
    --detectors 2 --max-observations 16 --validate
```

This downloads 16 cutouts (about 5 MB each, about 20 s each) and the QR3 ePSF
for detector 2, then runs the full pipeline and validation. Reruns use the
verified cache.

The larger verification run (all 75 images as 0.3-degree cutouts, full
calibration, Gaia and JPL context, all validation procedures):

```bash
python3 -m heimdall investigate --source irsa-spherex --ra 150.7817877 --dec 3.3502204 --radius 0.15 \
    --detectors 2 --calibration-products full --validate
HEIMDALL_OFFLINE=1 python3 -m heimdall investigate ...   # identical rerun from the cache, no network
```

The first run downloads about 400 MB (about 50 minutes, mostly the IRSA
cutout service) and spends about 10 minutes on JPL queries. The offline rerun
took 656 s with a peak memory of 1.04 GB.

## Python API

```python
from heimdall.api import Engine
from heimdall.pipeline.runner import InvestigationRequest

engine = Engine.from_environment()
cov = engine.coverage("irsa-spherex", 150.78, 3.35, 0.05, detectors=[2])
result, run_dir = engine.investigate(InvestigationRequest(ra_deg=150.78, dec_deg=3.35, radius_deg=0.05,
                                                          source_id="irsa-spherex", detectors=[2], max_observations=8))
sky = engine.skymap(run_dir, viewport=(150.78, 3.35, 0.1))
frames = engine.playback(run_dir, mjd_start=60990, mjd_end=61010)
```

## Reading results honestly

- `status.execution` says whether the computation ran (`COMPLETED`,
  `PARTIAL`, `FAILED`). It is never scientific validity.
- `status.outcome`: `OK`, `NO_OBSERVATIONS` (a successful empty query),
  `INSUFFICIENT_OBSERVATIONS` (nothing usable), `PARTIAL`. A failed request is
  `FAILED` with an error, never "empty".
- A candidate's `attention_level` follows the rules in
  `SCIENCE_METHODS.md` section 11. `validity` says whether its significance is
  validated for this scope. `planet_x.status` is a consistency statement only.
- `null` numbers mean "not computed or not available". The adjacent status
  and reason fields say why.
