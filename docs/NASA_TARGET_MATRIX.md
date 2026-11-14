# NASA target validation matrix

Generated from saved results by `python3 -m heimdall target matrix`. Every statement below is read from the saved investigation JSON; nothing here is hand-written. Test status: PASSED = analysis completed with a reported verdict (a non-detection is a valid verdict); LIMITED_BY_DATA = the observations could not support the test; FAILED/BLOCKED = the engine could not run it.

> **Status at time of writing (2026-11-14 ~17:05Z):** the IRSA TAP service (`https://irsa.ipac.caltech.edu/TAP/sync`) answered `502 Proxy Error`
> to every query, including `SELECT TOP 1 observationid FROM spherex.observation`, from 16:38Z onward (log: `.heimdall/verification/2026-11-14-targets/runner.log`).
> SPHEREx observation discovery and cutout retrieval go through that service, so **no SPHEREx image of any target has been retrieved or measured in this sprint**.
> Catalogue identification via the ESA Gaia archive did succeed (see `python3 -m heimdall target resolve NASA-0x`). A background runner
> (`scripts/run_nasa_targets.sh`, see the log) waits for IRSA and executes the five investigations; regenerate this file afterwards with
> `python3 -m heimdall target matrix --write docs/NASA_TARGET_MATRIX.md`. Rows below say "Not yet run" until that happens.
> Earlier in the same session (before the outage) live TAP coverage counts were obtained for six seed positions (see `docs/REQUIREMENTS_LEDGER.md` REQ-001).


## NASA-01 Barnard's Star (MOVING_OBJECT)

Not yet run.

## NASA-02 Epsilon Indi B (Ba/Bb brown-dwarf binary) (MOVING_OBJECT)

Not yet run.

## NASA-03 WISE 0855-0714 (MOVING_OBJECT)

Not yet run.

## NASA-04 RW Aurigae (VARIABLE_STAR)

Not yet run.

## NASA-05 R Draconis (VARIABLE_STAR)

Not yet run.
