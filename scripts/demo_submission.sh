#!/usr/bin/env bash
# HEIMDALL submission demo (backend only). Every number printed is read from saved result JSON or the local data service;
# nothing here simulates or fabricates a result. Steps 2-6 never contact the archive (HEIMDALL_OFFLINE=1).
#   scripts/demo_submission.sh            # offline/cached demo + HTTP API
#   scripts/demo_submission.sh --live     # also probe IRSA route health (bounded, ~1 min)
#   scripts/demo_submission.sh --tests    # also run the full test suite (~70 s)
#   scripts/demo_submission.sh --bundle B # fresh, isolated state: verify + import evidence bundle B (dir or .tar.gz), network disabled,
#                                         # then replay and run one fresh measurement on the imported genuine products
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${HEIMDALL_DEMO_PORT:-8797}"
BUNDLE=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    --tests) echo "== tests"; python3 -W error -m unittest discover -s tests -t . 2>&1 | grep -v '^\[http\]' | tail -3 ;;
    --live)  echo "== live IRSA route health (each route reported separately; an outage is not a scientific result)"; python3 -m heimdall irsa-status || true ;;
    --bundle) i=$((i + 1)); BUNDLE="${args[$i]:-}" ;;
  esac
done

if [ -n "$BUNDLE" ]; then
  FRESH="$(mktemp -d)"
  export HEIMDALL_STATE_DIR="$FRESH/state" HEIMDALL_CACHE_DIR="$FRESH/cache"
  # network disabled: offline mode plus an unroutable proxy, so any accidental request fails instead of reaching the internet
  export HEIMDALL_OFFLINE=1 https_proxy="http://127.0.0.1:9" http_proxy="http://127.0.0.1:9" HTTPS_PROXY="http://127.0.0.1:9" HTTP_PROXY="http://127.0.0.1:9" \
         NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"  # only the local demo API is reachable
  echo "== 0. fresh state $FRESH: verify and import the evidence bundle"
  python3 -m heimdall evidence verify "$BUNDLE" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("verify:", "OK" if d["ok"] else "FAILED", d["bundle_id"], d.get("files"), "files", d["problems"][:3])'
  python3 -m heimdall evidence import "$BUNDLE" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("imported", d["bundle_id"], "| products", d["products"], "| open jobs cancelled", len(d["open_jobs_cancelled"]))'
fi

# Prerequisites: the demo replays saved evidence; say exactly what is missing instead of failing mid-way.
STATE="${HEIMDALL_STATE_DIR:-.heimdall/state}"
missing=()
for f in targets/NASA-01/latest.json targets/NASA-02/latest.json dataservice/service.sqlite3; do
  [ -f "$STATE/$f" ] || missing+=("$STATE/$f")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "demo prerequisites missing: ${missing[*]}" >&2
  echo "  -> import an evidence bundle: scripts/demo_submission.sh --bundle <bundle dir or .tar.gz>" >&2
  echo "  -> or export one from a machine that has the evidence: python3 -m heimdall evidence export <dir> --archive" >&2
  exit 3
fi

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

echo "== 3b. auditable claims (claim types + 8-check defensibility gate), NASA-02"
python3 -m heimdall target claims NASA-02 | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("headline:", d["headline_claim"])
for c in d["claims"]:
    print(" ", c["analysis"], "->", c["claim_type"], "| gate passed:", c["gate"]["passed"], "| does not establish:", c["does_not_establish"][0])'

echo "== 3c. coverage from the local index (no archive request)"
python3 -m heimdall coverage --source irsa-spherex --ra 269.4485 --dec 4.7394 --radius 0.02 --detectors 2 | head -2

echo "== 4. data service: queue, routes, store, worker heartbeat"
python3 -m heimdall data status | python3 -c '
import json, sys
d = json.load(sys.stdin)["metrics"]
print("jobs:", d["jobs"]["by_state"], "| store objects:", d["store"].get("objects"), "| worker:", d["worker"].get("running"))
print("routes:", {k: (v["ok"], v["requests"]) for k, v in d["routes"].items()})'

if [ -n "$BUNDLE" ]; then
  echo "== 4b. fresh measurement on the imported genuine products (blind pipeline, offline), compared with the bundled run"
  python3 -m heimdall investigate --source irsa-spherex --ra 150.1 --dec 2.2 --radius 0.05 --detectors 2 --cutout-size 0.1 --max-observations 4 \
    --label "fresh-state measurement" --quiet | head -3
  python3 - <<'PY'
import json, os, pathlib
runs = sorted(pathlib.Path(os.environ["HEIMDALL_STATE_DIR"], "runs").glob("R*/investigation.json"))
docs = [json.loads(p.read_text()) for p in runs]
new = [d for d in docs if d.get("request", {}).get("label") == "fresh-state measurement"][-1]
old = [d for d in docs if d["run_id"] == "R20261115T085933-1cb9d31a"]
print("fresh run", new["run_id"], "| origin", new["data_origin"], "| manifest", (new.get("input_manifest") or {}).get("manifest_id"),
      "| detections", len(new.get("detections") or []))
if old:
    print("results_hash identical to the bundled run:", new["results_hash"] == old[0]["results_hash"])
PY
fi

echo "== 5. HTTP API (offline) on 127.0.0.1:$PORT"
python3 -m heimdall serve --port "$PORT" 2>/dev/null &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do curl -s -o /dev/null "http://127.0.0.1:$PORT/api/v1/health" && break; sleep 0.3; done
B="http://127.0.0.1:$PORT/api/v1"
curl -s "$B/health"; echo
curl -s -w '  -> HTTP %{http_code} in %{time_total}s\n' -o /dev/null -X POST -d '{"target_id":"NASA-01"}' "$B/investigations"
curl -s -X POST -d '{"ra":150.1,"dec":2.2,"radius":0.05,"detectors":[2],"max_observations":4,"cutout_size_deg":0.1}' "$B/runs" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("field run ->", "stored run " + d["run_id"] + " (reused)" if d.get("run_id") else "job " + d["job"]["job_id"] + " (" + d["readiness"] + ")")'
# Uncached request: sent to a second server on a throwaway, empty state directory with no worker attached, so the demo can never create
# real archive work (a running worker on the real state would otherwise pick the job up).
TMPSTATE="$(mktemp -d)"
HEIMDALL_STATE_DIR="$TMPSTATE/state" HEIMDALL_CACHE_DIR="$TMPSTATE/cache" python3 -m heimdall serve --port "$((PORT + 1))" 2>/dev/null &
SRV2=$!
trap 'kill $SRV $SRV2 2>/dev/null || true; rm -rf "$TMPSTATE"' EXIT
for _ in $(seq 1 20); do curl -s -o /dev/null "http://127.0.0.1:$((PORT + 1))/api/v1/health" && break; sleep 0.3; done
curl -s -X POST -d '{"ra":10.0,"dec":-5.0,"category":"VARIABLE_STAR"}' "http://127.0.0.1:$((PORT + 1))/api/v1/investigations" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("uncached coordinate (throwaway state, no worker) ->", d.get("readiness"), d["job"]["job_id"], "must_resubmit:", d["must_resubmit"])'
echo "done."
