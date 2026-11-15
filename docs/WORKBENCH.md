# Running the HEIMDALL workbench

From the repository root:

```sh
python3 -m heimdall serve
```

Open <http://127.0.0.1:8765/>. There is no separate frontend build. The app reads the state directory used by this server. For a fresh state, import your evidence bundle first using the evidence commands in the main README. For jobs that need live access, start the API with an existing separate worker, or start the API with `--with-worker` when there is no existing worker for that state.

The default view draws genuine [SPHEREx DR1 HiPS](https://caltech-ipac.github.io/irsa-tutorials/spherex-hipscube/) display pixels in a Galactic Hammer projection. The entire sky fits at full extent. Zooming and panning recenter the same spherical projection and request native-resolution tiles when useful. Drag to move between fields once zoomed in. **Focus** centers the current investigation, and **Select region** starts a coordinate query from a point on the map. Open **Layers & filters**, **Investigations** or **Measurements** below the map when needed. Plane numbers are one-based in the interface and correspond to the 102 official central wavelengths. Planes 51 and 52 are intentionally blank dichroic channels.

HiPS requests use IRSA's public imagery URL and do not use TAP, SIA or the investigation worker. The `spherex-dr1-hips` folder in the configured cache holds imagery already opened, with a bounded budget and overview-image preference. Open the desired planes and zoom levels online once before running `HEIMDALL_OFFLINE=1 python3 -m heimdall serve` for an offline demonstration. If detail tiles are unavailable, the viewport uses the cached lower-resolution overview of that same plane. If the selected plane is unavailable, the UI reports that state and keeps the overlays usable. No survey image is synthesized.

Open a saved target or field to compare its overview, analysis and evidence. Select an observation from the plot or table and follow it to source provenance or reverse lookup. History shows the immutable versions and supports backend-guarded comparisons. Copy view preserves the saved record and camera, and requires a server with access to the referenced data.

For a blind field investigation, open a candidate and expand **Pattern and evidence relationships**. Each recorded relationship links both evidence records. Each record links the observations and detections it depends on. Shared inputs and admissibility remain visible throughout.

From source provenance, **View cached FITS pixels** shows a bounded, per-image display stretch if the exact science product is still cached and matches its recorded SHA-256. Nothing is fetched from the archive for a preview. The preview is in image pixel coordinates and should not be used for photometry or cross-image intensity comparisons.

The sky map accepts ICRS RA / Dec for searches and evidence. Global DR1 imagery is a published mosaic, not an analysis or a claim that HEIMDALL has downloaded every source image. Locally indexed partitions are a separate layer. Lines are measured-position links or source membership, not orbits. The plots show saved measurements and their recorded uncertainty, and inherit the underlying data and method limits. The display PNGs cannot be used for photometry. Native single-observation FITS previews have their own stretch and image coordinates. The browser does not yet reproject them as co-registered layers for blink or difference imaging.

The existing HTTP routes and auth still apply. The workbench connects to the same origin. When the API requires a token, the entered token lives only in the open tab's memory. The added reference routes are `GET /api/v1/runs`, `GET /api/v1/runs/{id}/skymap`, `GET /api/v1/inventory/domains`, and immutable `/api/v1/investigations/{id}/patterns` and `/report`. The configured bind address and access control are still the operator's responsibility.

DR1 Spectral All-Sky Cube: [10.26131/IRSA679](https://doi.org/10.26131/IRSA679). This workbench uses data products from SPHEREx, a joint project of JPL and Caltech funded by NASA. The DR1 spectral images are documented separately at [10.26131/IRSA678](https://doi.org/10.26131/IRSA678).
