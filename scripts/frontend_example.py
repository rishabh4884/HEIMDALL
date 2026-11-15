#!/usr/bin/env python3
"""Executable frontend integration example for the HEIMDALL HTTP API (stdlib only).

Run a server first (e.g. ``python3 -m heimdall serve`` or ``scripts/demo_submission.sh --bundle B`` style fresh state), then:

    python3 scripts/frontend_example.py --base http://127.0.0.1:8765/api/v1 [--poll-seconds 20] [--alternate]

It walks the flows a frontend needs and asserts the contract at every step (exit 1 on a violation):
health/capabilities -> coverage browse -> submit a target -> claims (quick answer -> headline claim -> evidence -> measurements ->
products) -> reverse lookup from a product -> versions + version comparison -> an invalid request (stable error envelope) -> a new-data
request (202 + job, reconnect by polling, never resubmit) -> optional alternate field run and a run comparison.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


class Client:
    def __init__(self, base: str, token: str | None = None) -> None:
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765/api/v1")
    ap.add_argument("--token")
    ap.add_argument("--poll-seconds", type=float, default=10.0, help="how long to poll a new-data job before reporting its state")
    ap.add_argument("--alternate", action="store_true", help="also submit an alternate field run and compare it with the stored one")
    a = ap.parse_args()
    c = Client(a.base, a.token)

    print("1. health and capabilities")
    code, h = c.call("GET", "/health")
    check(code == 200 and h["schema"] == "heimdall.http/1.0", f"health {code}; worker running={h['worker']['running']}, offline={h['offline']}")
    code, caps = c.call("GET", "/capabilities")
    check(code == 200 and caps["schema"] == "heimdall.capabilities/1.0", f"capabilities: {[x['id'] for x in caps['analyses']]}")

    print("2. browse coverage (local index only; never starts an analysis)")
    code, cov = c.call("GET", "/coverage?ra=269.4485&dec=4.7394&radius=0.02&detectors=2&limit=5")
    check(code == 200 and cov["readiness"] in ("READY", "PREPARING", "NO_COVERAGE"), f"coverage readiness={cov['readiness']} n={cov.get('n_observations')}")
    if cov.get("observations"):
        check(all("footprint" in o for o in cov["observations"]), "footprints present for the map")

    print("3. submit a named target (stored result or durable job)")
    code, r = c.call("POST", "/investigations", {"target_id": "NASA-02"})
    check(code in (200, 202), f"submit -> {code}")
    if code == 202:
        print(f"     job {r['job']['job_id']} {r['readiness']}; must_resubmit={r['must_resubmit']} (poll /jobs/<id>)")
    else:
        check("investigation_id" in r and "data_status" in r, f"result {r['investigation_id']} ({r['data_status'].get('result_kind')})")

    print("4. claims: quick answer -> headline claim -> evidence -> measurements -> products")
    code, cl = c.call("GET", "/targets/NASA-02/claims")
    check(code == 200 and cl["schema"] == "heimdall.claims/1.0", f"quick answer: {cl['quick_answer'][:110]}...")
    head = next(x for x in cl["claims"] if x["claim_id"] == cl["headline_claim"])
    check(bool(head["gate"]["checks"]), f"headline {head['analysis']} -> {head['claim_type']} ({head.get('strength')}); gate passed={head['gate']['passed']}")
    code, doc = c.call("GET", f"/investigations/{cl['investigation_id']}")
    used = set(head.get("observations_used") or [])
    prods = [p for p in doc.get("products", []) if p.get("obs_id") in used]
    check(code == 200 and (not used or prods), f"{len(used)} observations used -> {len(prods)} products with sha256")

    print("5. reverse lookup from a product to every dependent conclusion")
    if prods:
        code, lk = c.call("GET", f"/evidence/lookup?sha256={prods[0]['sha256']}")
        check(code == 200 and lk["versions"], f"product {prods[0]['sha256'][:12]} -> versions {[v['investigation_id'] for v in lk['versions']]}, {len(lk['claims'])} claims")

    print("6. versions and comparison")
    code, vs = c.call("GET", "/targets/NASA-02/versions")
    check(code == 200, f"{len(vs['versions'])} versions; latest usable {vs['latest_usable']}")
    sup = [v for v in vs["versions"] if v.get("supersedes")]
    if sup:
        code, cmpv = c.call("GET", f"/compare?a={sup[0]['supersedes']}&b={sup[0]['investigation_id']}")
        check(code == 200, f"compare {sup[0]['supersedes']} -> {sup[0]['investigation_id']}: {cmpv['comparability']['status']}; verdict changes {cmpv['changes']['verdicts'] if cmpv['changes'] else None}")
    code, bad = c.call("GET", "/compare?a=tinv:NASA-01:fd9680e5&b=tinv:NASA-02:0c714738")
    if code == 200:
        check(bad["comparability"]["status"] == "NOT_COMPARABLE" and bad["changes"] is None, "cross-target comparison refused: NOT_COMPARABLE, no change statement")

    print("7. failure envelope")
    code, err = c.call("POST", "/investigations", {"ra": 10})
    check(code == 400 and err["error"]["code"] == "INPUT_INVALID", f"invalid request -> {code} {err['error']['message']}")

    print("8. new-data request: accepted once, reconnect by polling, never resubmitted")
    code, nd = c.call("POST", "/investigations", {"ra": 210.0, "dec": 54.3, "category": "VARIABLE_STAR", "options": {"max_observations": 4}})
    check(code in (200, 202), f"submit -> {code}")
    if code == 202:
        jid, t0 = nd["job"]["job_id"], time.time()
        while time.time() - t0 < a.poll_seconds:
            code, st = c.call("GET", f"/jobs/{jid}")
            if "investigation_id" in st or st.get("readiness") not in ("PREPARING",):
                break
            time.sleep(2)
        state = "COMPLETE" if "investigation_id" in st else st["job"]["state"]
        check(code == 200, f"job {jid}: {state}; next attempt {st.get('job', {}).get('next_attempt_at')}; must_resubmit={st.get('must_resubmit')}")

    if a.alternate:
        print("9. alternate field run (material parameter change) and comparison with the stored run")
        base = {"ra": 150.1, "dec": 2.2, "radius": 0.05, "detectors": [2], "cutout_size_deg": 0.1}
        code, r1 = c.call("POST", "/runs", base | {"max_observations": 4})
        code2, r2 = c.call("POST", "/runs", base | {"max_observations": 6})
        print(f"     run(4 obs) -> {code} {r1.get('run_id') or r1.get('job', {}).get('job_id')}; run(6 obs) -> {code2} {r2.get('run_id') or r2.get('job', {}).get('job_id')}")
        if r1.get("run_id") and r2.get("run_id"):
            code, cr = c.call("GET", f"/compare?a={r1['run_id']}&b={r2['run_id']}")
            check(code == 200, f"run comparison: {cr['comparability']['status']}; parameters {cr['parameters']}; changes {cr['changes']}")
        else:
            print("     alternate run accepted as a durable job; compare once it completes (GET /jobs/<id>)")
    print("contract walk-through complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
