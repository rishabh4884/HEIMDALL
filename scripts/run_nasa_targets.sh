#!/bin/zsh
# Performs a bounded IRSA discovery health check, then runs the five NASA target investigations.
#
# It deliberately does not wait forever: a TAP outage is evidence to record, not
# a reason to leave an unattended process running indefinitely.  Set
# IRSA_PROBE_ATTEMPTS to a positive integer to choose the maximum number of
# probes (default 12, two minutes apart).  A later invocation is safe because
# completed retrievals are reused from the cache.
cd "$(cd "$(dirname "$0")/.." && pwd)"
LOG=${HEIMDALL_VERIFICATION_LOG_DIR:-.heimdall/verification/$(date -u +%F)-targets}
mkdir -p "$LOG"
MAX_PROBES=${IRSA_PROBE_ATTEMPTS:-12}
if ! [[ "$MAX_PROBES" =~ ^[1-9][0-9]*$ ]]; then
  echo "IRSA_PROBE_ATTEMPTS must be a positive integer" >&2; exit 2
fi
export HEIMDALL_REQUEST_TIMEOUT_S=${HEIMDALL_REQUEST_TIMEOUT_S:-45}
export HEIMDALL_MAX_RETRIES=${HEIMDALL_MAX_RETRIES:-1}
echo "$(date -u +%FT%TZ) starting IRSA TAP/SIA2 health check (max $MAX_PROBES probes)" >> "$LOG/runner.log"
for probe in $(seq 1 "$MAX_PROBES"); do
  tap_code=$(curl -s -m 45 -o /dev/null -w "%{http_code}" "https://irsa.ipac.caltech.edu/TAP/sync" --data-urlencode "QUERY=SELECT TOP 1 observationid FROM spherex.observation" -d "FORMAT=csv&LANG=ADQL&REQUEST=doQuery")
  sia_code=$(curl -s -m 45 -o /dev/null -w "%{http_code}" -G "https://irsa.ipac.caltech.edu/SIA" --data-urlencode "COLLECTION=spherex_dr1" --data-urlencode "POS=CIRCLE 0 0 0.01" --data-urlencode "RESPONSEFORMAT=CSV")
  if [[ "$tap_code" == "200" || "$sia_code" == "200" ]]; then
    echo "$(date -u +%FT%TZ) IRSA discovery available (TAP=$tap_code SIA2=$sia_code); starting runs" >> "$LOG/runner.log"
    break
  fi
  echo "$(date -u +%FT%TZ) IRSA probe $probe/$MAX_PROBES returned TAP=${tap_code:-transport-error} SIA2=${sia_code:-transport-error}" >> "$LOG/runner.log"
  if [[ "$probe" == "$MAX_PROBES" ]]; then
    echo "$(date -u +%FT%TZ) IRSA discovery unavailable after bounded health check; no target runs started" >> "$LOG/runner.log"
    exit 1
  fi
  sleep 120
done
for t in NASA-03 NASA-01 NASA-04 NASA-02 NASA-05; do
  echo "$(date -u +%FT%TZ) start $t" >> $LOG/runner.log
  python3 -m heimdall target run "$t" --max-observations 16 --max-seconds 2400 > "$LOG/$t.out" 2> "$LOG/$t.err"
  echo "$(date -u +%FT%TZ) end $t exit=$?" >> "$LOG/runner.log"
done
echo "$(date -u +%FT%TZ) all done" >> "$LOG/runner.log"
