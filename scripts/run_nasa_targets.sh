#!/bin/zsh
# Waits for IRSA TAP, then runs the five NASA target investigations one after another (bounded, resumable via the cache).
cd "$(cd "$(dirname "$0")/.." && pwd)"
LOG=.heimdall/verification/2026-11-14-targets
export HEIMDALL_REQUEST_TIMEOUT_S=120 HEIMDALL_MAX_RETRIES=2
echo "$(date -u +%FT%TZ) waiting for IRSA TAP" >> $LOG/runner.log
until curl -s -m 45 -o /dev/null -w "%{http_code}" "https://irsa.ipac.caltech.edu/TAP/sync" --data-urlencode "QUERY=SELECT TOP 1 observationid FROM spherex.observation" -d "FORMAT=csv&LANG=ADQL&REQUEST=doQuery" | grep -q 200; do
  echo "$(date -u +%FT%TZ) IRSA TAP not answering" >> $LOG/runner.log; sleep 120
done
echo "$(date -u +%FT%TZ) IRSA TAP answering; starting runs" >> $LOG/runner.log
for t in NASA-03 NASA-01 NASA-04 NASA-02 NASA-05; do
  echo "$(date -u +%FT%TZ) start $t" >> $LOG/runner.log
  python3 -m heimdall target run $t --max-observations 16 --max-seconds 2400 > $LOG/$t.out 2> $LOG/$t.err
  echo "$(date -u +%FT%TZ) end $t exit=$?" >> $LOG/runner.log
done
echo "$(date -u +%FT%TZ) all done" >> $LOG/runner.log
