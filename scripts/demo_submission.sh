#!/usr/bin/env bash
# HEIMDALL submission demo (backend only). Every number printed is read from saved result JSON or the local data service;
# nothing here simulates or fabricates a result. Steps 2-6 never contact the archive (HEIMDALL_OFFLINE=1).
#   scripts/demo_submission.sh            # offline/cached demo + HTTP API
#   scripts/demo_submission.sh --live     # also probe IRSA route health (bounded, ~1 min)
#   scripts/demo_submission.sh --tests    # also run the full test suite (~70 s)
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${HEIMDALL_DEMO_PORT:-8797}"

for arg in "$@"; do
  case "$arg" in
    --tests) echo "== tests"; python3 -W error -m unittest discover -s tests -t . 2>&1 | grep -v '^\[http\]' | tail -3 ;;
    --live)  echo "== live IRSA route health (each route reported separately; an outage is not a scientific result)"; python3 -m heimdall irsa-status || true ;;
  esac
done

export HEIMDALL_OFFLINE=1
echo "== 1. five NASA targets: saved status (execution / scientific test status)"
python3 -m heimdall target list

echo "== 2. NASA-01 from verified local data (no archive request); provenance summary"
python3 -m heimdall target run NASA-01 --json --quiet | python3 -c '
import json, sys
r = json.load(sys.stdin)
if "investigation_id" not in r:
    print("not available locally:", r.get("readiness"), r.get("message")); sys.exit(0)
m = r.get("input_manifest") or {}
print("investigation:", r["investigation_id"], "| data_origin:", r["data_origin"], "| status:", r["status"])
print("served from:", r["data_status"])
print("manifest:", m.get("manifest_id"), "| products:", len(m.get("products", [])), "| calibrations:", len(m.get("calibrations", [])))
for p in (m.get("products") or [])[:3]:
    print("  ", p.get("obs_id"), p.get("release"), p.get("route"), p.get("retrieved_at"), (p.get("sha256") or "")[:16])
print("quick answer:", r["conclusion"]["quick_answer"])'

echo "== 3. NASA-02 version history (decision-rule correction kept as a linked version)"
python3 -m heimdall target show NASA-02 | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("current:", r["investigation_id"], "| motion:", r["analyses"]["motion"]["verdict"], "| variability:", r["analyses"]["variability"]["verdict"])
s = r.get("supersedes")
print("supersedes:", s and s["investigation_id"], "|", s and s["reason"])'

echo "== 4. data service: queue, routes, store, worker heartbeat"
python3 -m heimdall data status | python3 -c '
import json, sys
d = json.load(sys.stdin)["metrics"]
print("jobs:", d["jobs"]["by_state"], "| store objects:", d["store"].get("objects"), "| worker:", d["worker"].get("running"))
print("routes:", {k: (v["ok"], v["requests"]) for k, v in d["routes"].items()})'

echo "== 5. HTTP API (offline) on 127.0.0.1:$PORT"
python3 -m heimdall serve --port "$PORT" 2>/dev/null &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do curl -s -o /dev/null "http://127.0.0.1:$PORT/api/v1/health" && break; sleep 0.3; done
B="http://127.0.0.1:$PORT/api/v1"
curl -s "$B/health"; echo
curl -s -w '  -> HTTP %{http_code} in %{time_total}s\n' -o /dev/null -X POST -d '{"target_id":"NASA-01"}' "$B/investigations"
JOB=$(curl -s -X POST -d '{"ra":10.0,"dec":-5.0,"category":"VARIABLE_STAR"}' "$B/investigations" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("uncached coordinate ->", d.get("readiness"), d["job"]["job_id"], "must_resubmit:", d["must_resubmit"], file=sys.stderr)
print(d["job"]["job_id"])')
# the demo cancels its own job so a running worker does not spend archive time on it
curl -s -X POST "$B/jobs/$JOB/cancel" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("cancelled demo job ->", d["readiness"], "must_resubmit:", d["must_resubmit"])'
echo "done."
