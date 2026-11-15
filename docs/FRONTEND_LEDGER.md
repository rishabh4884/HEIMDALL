# Frontend execution ledger (run 3, 2026-11-15)

Starting point: `main` = `origin/main` = `91d4ddd26947ec066fa477bc4c5369bdbd09229e`, working tree clean. The audit snapshot S6
(`…/work/heimdall-check`, no Git metadata) was compared file by file. It is an earlier state of the same workbench: the seven
`heimdall/web` files differ by 5–172 lines, and `docs/WORKBENCH.md` did not exist yet. The current tree supersedes it.

The recovered historical sources (S1–S5) were not re-read in this run. F-IDs and their provenance come from the attached
preliminary ledger. **Historical recovery is not complete.** Exact original choreographies (Separate/Merge, guided demo) remain
unrecovered, and the interactions below that stand in for them are new design decisions.

Status vocabulary:
- **Complete**: implemented, with acceptance evidence.
- **Partial**: implemented with a stated gap.
- **Dependency**: blocked by a missing backend, data or service element, named in the row.
- **Deferred**: not started, for the reason given.

## Verification evidence

| Evidence | Command | Result |
|---|---|---|
| Browser workflows (real UI, real API, real saved data) | `python3 -m heimdall serve --port 8777` then `node scripts/browser_check.mjs --base http://127.0.0.1:8777` | Latest run: 31/31 on the real saved state (online), including keyboard, accessibility tree and constellations. Previous run: 28/28 on the real saved state (online). 29/29 with `--writes` on a throwaway offline state holding copies of NASA-01/02, including review-note persistence and the no-worker and offline paths. 0 console errors or uncaught exceptions in either |
| Frontend pure modules | `node --test tests/test_frontend_modules.mjs tests/test_hips.mjs` | 17/17 |
| Twin-view pair endpoint and module serving | `python3 -W error -m unittest tests.test_web_pair` | 5/5. Synthetic fixtures; includes a 25° detector rotation that aligns after resampling |
| Full backend suite | `python3 -W error -m unittest discover -s tests -t .` | 582 passed, 5 opt-in live tests skipped |
| Name resolution and review notes | `python3 -W error -m unittest tests.test_resolve_notes` | 5/5 (Sesame mocked); one live Sesame check resolved Barnard's star to 269.45208, +4.69336 |
| Large dataset | browser check "large dataset" | The real `SkyMap` redraws 20 000 synthetic points in 4.8 ms median, 8.6 ms max (a synthetic load test; it measures rendering, not science) |
| Accessibility automation | browser check "accessibility" | Every visible control has an accessible name; sampled text classes reach at least 4.5:1 contrast, after raising three low-contrast labels |
| Pan responsiveness | browser check "pan responsiveness" | See the measurement below |
| Durable job journey (UI) | `node scripts/browser_check.mjs --base <disposable server> --job-flow`, with the evidence bundle imported into a throwaway state and HTTP(S) routed to a dead proxy | 4/4 through the UI: the *Region & run* submit is queued while no worker runs; a reload reconnects with no resubmission; the script starts the worker; the job completes from **cached genuine DR1 products** (run `R20261115T160841-3904c8ea`, REAL, execution PARTIAL with one epoch) and the result is displayed; a second job needing uncached products is cancelled. A live archive run is still unverified (IRSA discovery outage) |
| Keyboard and screen-reader semantics | browser checks "keyboard" and "screen-reader semantics" | Real key events through Chrome's input pipeline: Tab reaches the skip link (focus visible); Enter moves to the inspector; the Jobs dialog takes focus and Escape returns it to the opener; arrow, Home and End keys on tabs, strip and ribbon; Space plays; search by keyboard. The Chrome accessibility tree (1094 nodes) exposes banner, main, search, navigation, tablist with one selected tab, wavelength listbox and options (plane 51 announced blank), epoch slider with value, the map as a named application, and no unnamed buttons |
| Constellation boundaries | `tests.test_constellations` plus a live build | CDS VI/49 `constbnd` B1875 outlines precessed to J2000 match all **1533** original vertices of `bound_20` (CDS revision 2024-07-23) within **0.00024°**. Fetched from the CfA VizieR mirror; the CDS FTP host timed out |
| Absent worker and empty state | throwaway empty state, no worker | Absent-worker banner shown; a NASA-02 link explains the missing record and keeps the view; library shows "No saved result" |

**Pan responsiveness measurement**
- Setup: macOS on Apple Silicon, headless Chrome, 1440×1000 viewport, NASA-02 field with the DR1 survey layer on.
- Method: 60 pointer moves at 16 ms intervals; animation-frame intervals recorded while dragging.
- Before: p50 16.8–33.3 ms, p95 33.4–50.0 ms across runs. The survey layer was re-sampled at full 720 px width on every move.
- After (survey sampled at half resolution while the camera moves; full resolution on settle): p50 16.7 ms, p95 16.7–16.8 ms,
  max 16.8–33.3 ms in two consecutive runs. That is a steady ~60 fps.
- The ~60 fps / <100 ms figures are engineering targets, not historical promises.

The browser checks run against real saved records:
- NASA-01..05;
- real field run `R20261115T103457-def358a0`;
- a synthetic field run, which carries the watermark;
- real cached DR1 cutouts of NASA-02 for the twin views.

## Traceability (F01–F32)

| ID | Status | Implementation (files) | Acceptance evidence | Known limits / next action |
|---|---|---|---|---|
| F01 Explore → Discover → Investigate → Understand | Complete | `workbench.js` (one record and selection model), `state.js` | Browser: map → chart → inspector share `obs_id`; the URL `sel=` matches | — |
| F02 Coordinate sky, pan/zoom, grid, overlays | Complete | `sky.js` (`orientation()` measures north/east and scale from the projection), compass and scale bar | `test_hips.mjs` RA-seam round trips; screenshots show the rotated compass at Dec −56.8° | Orientation is clamped at Dec ±89.999° |
| F03 Object search and coordinate entry | Complete | Omnibox: targets, runs, `tinv:`/obs/sha/manifest/job ids, decimal and sexagesimal coordinates | Browser: "barnard" → NASA-01; `17:57:48.5 +04:41:36` → field | General names resolve through `GET /resolve` (CDS Sesame) only when the user picks the option, which says the name is sent to CDS. The result is a catalogue navigation position (no proper motion applied), never a measurement. Offline mode refuses without network access. Live-verified: Vega lands at 279.2 |
| F04 Box/lasso region selection | Complete | `sky.js` (`within`, `hitAll`), `views.regionBlock`; keyboard: *Select visible* and *Region & run → select within radius* | Browser: box → "16 observations, 1 reference"; deterministic order is unit-tested | Selection covers plotted records only, which is stated in the UI |
| F05 102-band strip | Complete | `bands.js` (published layout: band = detector, frame = 17·(band−1)+(channel−1), planes 51/52 blank), strip in `workbench.js` | Browser: 102 cells, blanks 51 and 52, End key → 102, 16 measurement marks | Survey bandwidths are absent from the HiPS metadata, so the planes are labelled "nominal". Measurement marks use their calibrated wavelength and source |
| F06 Separate/Merge | Complete (new design; historical choreography unrecovered) | `toggleSeparate` (FLIP animation; instant under reduced motion); six detector lanes | Browser: reversible; captions say "Illustrative" and "does not coadd" | Reconciliation (2026-11-15): checked the S6 snapshot, both Codex output folders (master prompt and ledger), every repo doc and the full git history. The only project evidence is REQ-021's note that the blind-run sky map carries per-detector band metadata (`layers.bands`); the lanes now show it for field runs. The ChatGPT conversations S1–S5 are not accessible from this environment, so the original choreography stays unrecovered. Decision FD-1 below |
| F07 Twin view, blink, difference | Complete with limits | `GET /evidence/pair` (`heimdall/web_pair.py`), `twin.js` | Real NASA-02 D2 pair: COMPARABLE_WITH_LIMITATIONS with a difference image. D2 vs D5: NOT_COMPARABLE, difference disabled with its reason. Synthetic alignment test | The difference image is marked `psf_matched: false` and `DISPLAY_ONLY_NOT_EVIDENCE` by the server. The UI requires an explicit acknowledgement before showing it and keeps a prominent "Not PSF-matched · display only · not evidence of change" disclosure beside it; the browser check asserts both. Change claims come only from the gated analyses |
| F08 Time ribbon | Complete | `timeline.js`, ribbon in `workbench.js` | Browser: 12 real epochs; elapsed and equal spacing differ; Home key; play and pause. Pauses on hidden page, inspection or record change | Map, chart and twin view follow the epoch (with *Compare images* open, image B becomes the same-detector observation of the current epoch) |
| F09 Wavelength-aware temporal comparability | Complete | `charts.js` (`chartModel('flux')` connects only points within one comparable series) | Browser and screenshot; series listed with their comparability status | — |
| F10 Coverage browsing ≠ analysis | Complete | `views.coveragePage`; runs only from *Region & run* | Browser: `?field=` issues no POST; no POST during any check except explicit actions | — |
| F11 Known-object checkpoints | Complete (decision FD-2) | The tour and library load saved results; rerunning is explicit | Browser: five targets, 0 POSTs | No auto-run: NASA-02 predates frozen manifests, so a POST would queue archive work |
| F12 Quick answer → Evidence → Deeper dive | Complete | `views.js` | Browser NASA-02 quick answer; `status-grid` separates execution, test status, claim and method validity | — |
| F13 Hypotheses with supporting/contradicting evidence | Complete | `views.hypothesesGrid` (side by side: tests run and unavailable, assumptions, groups, next step; open alternatives) | Browser screenshots | — |
| F14 Motion, flux, spectra, tables | Complete | `charts.js`: error bars, bandwidth bars, detector boundaries, uncovered detectors shaded, hollow excluded points with reasons, censored upper limits, a table for every chart, a centred single point, padded equal values, a zero line | Browser and screenshots | Large tables are capped at 400 rows rather than virtualized (see F30) |
| F15 Forward and reverse provenance | Complete | `records.traceView`, lookup | Browser: claim → observation → product → reverse lookup → 2 dependent versions | — |
| F16 History, comparison, refusal | Complete | `views.historyPage`, `compareView` (structured changes; NOT_COMPARABLE shows no changes) | Browser: comparable NASA-02 pair; refused NASA-01 vs NASA-02 | — |
| F17 Context kept across band, time and view changes | Complete | Separate slices for record, camera, plane, filters, selection, frame and compare | Browser: plane and epoch changes keep record, camera and selection | — |
| F18 Share and reopen exact context | Complete | `state.serialize/parse` (record, camera, plane, layers, filters, selection, epoch, tab, compare, twin pair, separate) | Unit round-trip; credential parameters ignored; browser: an invalid link keeps plane and camera and explains the missing record | Reopening needs a server holding the same data, which the UI states |
| F19 Durable jobs | Complete (UI journey on cached real data) / live archive unverified | `jobs.js`: 3 s cadence, 5 s back-off, dormant during auto-recovery, paused while hidden, ids survive reload, never resubmits | Unit tests with fake timers; browser jobs dialog lists server jobs without a POST | Fixed: restored jobs were not polled after a reload (they stayed "Checking"). They now reconnect at once (unit test plus UI journey). *Region & run* exposes detectors, maximum observations and an MJD window, with transfer guidance. **Remaining:** a live-archive job, blocked by the IRSA discovery outage |
| F20 Capability-aware controls | Complete | *Region & run* offers only analyses declared in `/capabilities` (others disabled with the reason); *Deeper dive* lists `not_available` with reasons; worker and offline notes in the run dialog; Data status shows per-detector validation | Browser: capability check on online and offline servers | — |
| F21 Follow-up plans and budgets | Complete | `views.followupView` (distinguishes, predictions, cost, expected value, availability reason, budget, stopping reason; run once via `POST /followup/{id}/run`) | NASA-02 plan rendered with its stopping reason | — |
| F22 Cached and offline operation | Complete | Offline banner from `/health`; offline note in the run dialog; imagery status explains offline; cached records stay readable | Full browser run (24/24) against `HEIMDALL_OFFLINE=1` on the saved state | The offline run used the saved state, not a fresh bundle import (the backend demo already covers that, `scripts/demo_submission.sh --bundle`) |
| F23 Metadata vs downloaded vs used coverage | Complete | Coverage page and inventory banner state that indexed ≠ downloaded ≠ analysed; all-sky completeness is never claimed | Browser: coverage page text | — |
| F24 Guided discovery | Complete (decision FD-3; historical design unrecovered, see F06) | Six-step tour on real records; skippable, replayable, every step optional | Browser: skip closes; "Show me" opens NASA-02 | — |
| F25 Interruptible data-driven demonstration | Complete (new design) | *Autoplay all steps* advances through the real-record tour every 9 s; any key, pointer or wheel input stops it. Real-epoch playback is also available | Browser: autoplay starts and stops on a key press | The original cinematic specification was not recovered; this is decision FD-6 |
| F26 Ranking/ML subordinate | Complete | Ranking is labelled "review priority, never a probability"; no ML or PCA surfaced | — | — |
| F27 Review annotations | Complete | `GET/POST /notes`, `heimdall/resolve_notes.py`: append-only JSON lines under `<state>/review_notes/`, only for records that exist, supersession without deletion. *History* shows the notes labelled "Human note", separate from claims | Unit tests; browser: a note saved, labelled and persisted across reload (throwaway state) | Notes are local to the server's state directory; there are no user accounts, so the author is free text |
| F28 Reference overlays | Complete | Galactic equator and ecliptic (computed). IAU constellation boundaries via `GET /reference/constellations` (`heimdall/constellations.py`): fetched at run time from CDS or its CfA mirror, cached, never committed, attributed beside the map; offline serves the cache | Unit tests (format, constant-RA/Dec edges, RA seam, real Crux vertices within 0.00012°, mirror fallback, offline); live validation 0.00024° over 1533 vertices; browser screenshot | Boundaries only: stick figures have no single authoritative definition and are not drawn |
| F29 Accessibility | Complete (automated) / manual screen reader unverified | Keyboard for strip, ribbon, tabs, overlap chooser, omnibox combobox; focus restore; live region; reduced motion; tab roles; field errors beside inputs | Browser: keyboard steps; 390 px with no horizontal scroll | Keyboard journeys with real key events and the Chrome accessibility tree are verified. **Manually unverified:** speech output and navigation with VoiceOver or NVDA (it needs a person; enabling VoiceOver automation would change system settings) and Windows high-contrast mode |
| F30 Performance | Complete | rAF-coalesced drawing, half-resolution survey sampling while moving, de-duplicated GETs, cache of immutable records, generation guards, bounded caches, paused polling, paged tables (400 rows at a time) | Pan p50 16.7 ms / p95 16.8 ms after the half-resolution change (was p50 up to 33.3 ms / p95 up to 50.0 ms) | 20 000-point redraw in 4.8 ms median; table paging rather than DOM virtualization |
| F31 Origin and uncertainty honesty | Complete | Synthetic watermark on the map canvas, map banner, inspector and twin view; claim strength always shown | Browser: synthetic run watermark | — |
| F32 Reproducible reports | Complete | Export checks the report's `investigation_id` against the viewed version before saving | Report routes covered by `tests/test_workbench.py` | A headless browser download is not file-verified |

## Design decisions made in this run

- **FD-1 Separate/Merge.**
  - *Merge*: one wavelength-ordered axis of the 102 survey planes.
  - *Separate*: six detector lanes of 17 channels each, illustrating that the detectors observe simultaneously through linear
    variable filters.
  - Measurement ticks sit at each measurement's calibrated wavelength in its own lane, never snapped to a survey plane.
  - Captions state that the arrangement is illustrative and that merging is display ordering, not coaddition.
- **FD-2 Known-object entry.** Library and tour checkpoints open saved results only. A new run is always an explicit action
  (`Prepare a new run`), because a POST for a target without a frozen manifest queues archive preparation.
- **FD-3 Guided tour.** Six steps over real records: all sky, NASA-02, strip and ribbon, quick answer, NASA-01 as a limited
  result, and the twin view. It is offered once with a notice, never forced, and can be replayed from *Guide*.
- **FD-4 Display vs analysis pixels in twin views.**
  - Display images use every finite pixel.
  - The engine's photometry exclusion mask (for example HALO, TRANSIENT, SUR_ERROR) is an optional overlay.
  - The difference image uses the masked analysis arrays.
- **FD-6 Demonstration autoplay.** It reuses the six tour steps on real records with a 9 s dwell and stops on any input. Nothing is
  animated beyond the existing real-epoch playback.
- **FD-7 Name resolution.** CDS Sesame is called only on an explicit choice whose label says the name leaves the machine. Results
  are cached per server process.
- **FD-5 Transport adapter.** `GET /evidence/pair` is additive and read-only. It works only on exact checksum-verified local
  products and never fetches anything. The comparability rule is the existing temporal tolerance
  (`wavelength_tolerance_fraction` × bandwidth, same detector). A saved series' comparison record is attached when the
  investigation is named.

## Unresolved dependencies

| Missing element | Kind | Effect for the user |
|---|---|---|
| PSF-matched, source-registered difference products | Backend method | The difference view is an inspection aid with caveats, not a measurement |
| Live end-to-end job run in the browser | External access (IRSA discovery outage) | Job reconnect is unit-verified, not live-verified |
| Manual screen-reader session | Human tester | Automated name and contrast checks only |
| Recovery of the original conversations (S1–S5) | Access to the historical chats | Separate/Merge (FD-1), the tour (FD-3) and autoplay (FD-6) are new designs, not recovered specifications |
| Live-archive durable job in the browser | External access (IRSA discovery outage) | Verified on cached genuine products with the network blocked, not against the live archive |

## Test isolation (fixed 2026-11-15)

- **Cause:** `tests/test_investigation.py` and `tests/test_contract_interfaces.py` built `Engine()` from default settings, so each
  suite run saved two synthetic demo runs into the real `.heimdall/state/runs`.
- **Fix:** both now use disposable settings, and `tests/__init__.py` points `HEIMDALL_STATE_DIR` (and, by default, the cache) at a
  per-run temporary directory.
- **Verified:** the real runs directory had 105 entries before and after a full suite run.
- **Existing records:** 95 runs were certainly test-created (SYNTHETIC DEMONSTRATION with products in deleted unittest temp
  dirs). They were moved, not deleted, to `.heimdall/state/quarantine/test-created-runs-2026-11-15/` with a manifest and a restore
  command.
- **Left in place:** 4 synthetic demo runs from an `opencode` session (ownership uncertain), and every real run.

## Commits (branch `main`, pushed to `origin`)

| Commit | Feature IDs | Verification |
|---|---|---|
| `2510f8e` Add co-registered product pair endpoint for twin image views | F07 (transport), FD-5 | `tests.test_web_pair` plus workbench and server tests in an isolated worktree (44 OK); full suite 577 OK |
| `8e3cdb2` Integrate the sky, wavelength, time, image comparison and evidence workbench | F01–F19, F21, F23–F26, F28–F32 | 23/23 browser workflows; 17/17 node; full suite 577 OK (5 skipped) |
| `9921be8` Gate run options on declared capabilities and verify offline and no-worker states | F20, F22 | 24/24 browser online and offline; empty-state check |
| `0e8c55a` Add name resolution, review notes, demonstration autoplay and load checks | F03, F25, F27, F29, F30 | 28/28 browser on the real state; 29/29 with writes on a throwaway offline state; 582 Python OK; 17 node |
