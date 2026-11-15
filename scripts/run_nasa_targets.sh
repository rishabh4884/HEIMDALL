#!/bin/zsh
# Bounded IRSA health check, then the NASA target investigations (default: all five; or pass target ids).
#
#   scripts/run_nasa_targets.sh                 # NASA-03 NASA-01 NASA-04 NASA-02 NASA-05
#   scripts/run_nasa_targets.sh NASA-05         # just one
#
# Health = either discovery route (SIA2 or TAP) answers. The four components - TAP, SIA2, direct product, calibration - are logged
# separately by `python3 -m heimdall irsa-status`. The wait is finite: IRSA_PROBE_ATTEMPTS probes (default 8) IRSA_PROBE_SLEEP_S apart
# (default 90); if no discovery route ever answers the script exits 1 without starting any run. Completed retrievals are cached, so a
# later invocation resumes cheaply. Exit 0 means the bounded workflow ran; it is not a scientific verdict (read the matrix).
cd "$(cd "$(dirname "$0")/.." && pwd)"
LOG=${HEIMDALL_VERIFICATION_LOG_DIR:-.heimdall/verification/$(date -u +%F)-targets}
mkdir -p "$LOG"
MAX_PROBES=${IRSA_PROBE_ATTEMPTS:-8}
SLEEP_S=${IRSA_PROBE_SLEEP_S:-90}
if ! [[ "$MAX_PROBES" =~ ^[1-9][0-9]*$ && "$SLEEP_S" =~ ^[0-9]+$ ]]; then
  echo "IRSA_PROBE_ATTEMPTS must be a positive integer and IRSA_PROBE_SLEEP_S a non-negative integer" >&2; exit 2
fi
export HEIMDALL_REQUEST_TIMEOUT_S=${HEIMDALL_REQUEST_TIMEOUT_S:-45}
export HEIMDALL_MAX_RETRIES=${HEIMDALL_MAX_RETRIES:-2}
TARGETS=("$@"); [[ ${#TARGETS[@]} -eq 0 ]] && TARGETS=(NASA-03 NASA-01 NASA-04 NASA-02 NASA-05)
echo "$(date -u +%FT%TZ) health check (max $MAX_PROBES probes, ${SLEEP_S}s apart)" >> "$LOG/runner.log"
healthy=0
for probe in $(seq 1 "$MAX_PROBES"); do
  python3 -m heimdall irsa-status > "$LOG/irsa-status.last" 2>&1; rc=$?
  sed "s/^/$(date -u +%FT%TZ) probe $probe\/$MAX_PROBES /" "$LOG/irsa-status.last" >> "$LOG/runner.log"
  if [[ $rc -eq 0 ]]; then healthy=1; break; fi
  [[ "$probe" != "$MAX_PROBES" ]] && sleep "$SLEEP_S"
done
if [[ $healthy -ne 1 ]]; then
  echo "$(date -u +%FT%TZ) no IRSA discovery route (TAP or SIA2) answered within the bounded health check; no runs started" >> "$LOG/runner.log"
  exit 1
fi
echo "$(date -u +%FT%TZ) a discovery route is available; starting: ${TARGETS[*]}" >> "$LOG/runner.log"
for t in $TARGETS; do
  echo "$(date -u +%FT%TZ) start $t" >> "$LOG/runner.log"
  python3 -m heimdall target run "$t" --max-observations 16 --max-seconds 2400 > "$LOG/$t.out" 2> "$LOG/$t.err"
  echo "$(date -u +%FT%TZ) end $t exit=$?" >> "$LOG/runner.log"
done
echo "$(date -u +%FT%TZ) all done (exit codes are software status, not scientific results; see docs/NASA_TARGET_MATRIX.md)" >> "$LOG/runner.log"
