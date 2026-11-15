// Pure frontend modules under node:test: identity/link model, transport classification, job polling, bands, epochs.
import { test } from "node:test";
import assert from "node:assert/strict";
import { identify, parse, parseCoordinates, serialize, orderItems, targetOfVersion } from "../heimdall/web/state.js";
import { ApiClient, FAILURE, classify, describe } from "../heimdall/web/api.js";
import { JobTracker, summarize, ACTIVE_MS, BACKOFF_MS } from "../heimdall/web/jobs.js";
import { planeInfo, allPlanes, fractionalChannel, measurementMarks } from "../heimdall/web/bands.js";
import { buildEpochs, positions, gaps, dwell, formatEpoch } from "../heimdall/web/timeline.js";
import { insidePolygon, area } from "../heimdall/web/sky.js";

test("view links round-trip every reproducible part and never carry credentials", () => {
  const view = { record: { kind: "version", id: "tinv:NASA-02:0c714738" }, camera: { ra: 331.1, dec: -56.8, span: .12 }, plane: 40,
    layers: { survey: true, grid: true }, filters: { detector: "2", start: 60800, waveEnd: 2.5 }, selection: { kind: "observation", id: "2025W17_4B_0017_1D2" },
    frame: "2025W17_4B_0017_1D2", tab: "deeper", compare: { a: "tinv:NASA-02:0c714738", b: "tinv:NASA-02:c3c6c224" }, twin: { a: "x1D2", b: "y1D2" }, separated: true, token: "secret" };
  const qs = serialize(view);
  assert.ok(!qs.includes("secret"));
  const back = parse(qs);
  assert.deepEqual(back.problems, []);
  assert.deepEqual(back.record, view.record);
  assert.deepEqual(back.camera, view.camera);
  assert.equal(back.plane, 40);
  assert.equal(back.layers.grid, true);
  assert.equal(back.layers.candidates, false);
  assert.deepEqual(back.filters, { detector: "2", start: 60800, waveEnd: 2.5 });
  assert.deepEqual(back.selection, view.selection);
  assert.equal(back.frame, view.frame);
  assert.equal(back.tab, "deeper");
  assert.deepEqual(back.compare, view.compare);
  assert.deepEqual(back.twin, view.twin);
  assert.equal(back.separated, true);
});

test("invalid or obsolete link parts are reported and dropped, valid parts kept", () => {
  const p = parse("version=tinv:bad&plane=200&ra=10&dec=95&span=1&sel=observation:abc&f.det=9&token=x&field=150.1,2.2,0.05");
  assert.equal(p.record.kind, "field");
  assert.equal(p.plane, undefined);
  assert.equal(p.camera, undefined);
  assert.deepEqual(p.selection, { kind: "observation", id: "abc" });
  assert.ok(p.problems.length >= 4);
  assert.ok(p.problems.some((x) => x.includes("Credentials")));
});

test("identifiers and coordinates are recognised", () => {
  assert.deepEqual(identify("tinv:NASA-02:0c714738"), { kind: "version", id: "tinv:NASA-02:0c714738" });
  assert.equal(identify("R20261115T103457-def358a0").kind, "run");
  assert.equal(identify("A".repeat(0) + "566467f7c64213b580e9dadda12bed810fdd036bca2fbbcdba24725e06fea527").kind, "product");
  assert.equal(identify("2025W17_4B_0017_1D2").kind, "observation");
  assert.equal(targetOfVersion("tinv:NASA-02:0c714738"), "NASA-02");
  const s = parseCoordinates("17:57:48.5 +04:41:36");
  assert.ok(Math.abs(s.ra - 269.452083) < 1e-5 && Math.abs(s.dec - 4.693333) < 1e-5);
  assert.deepEqual(parseCoordinates("150.1, 2.2"), { ra: 150.1, dec: 2.2 });
  assert.ok(parseCoordinates("361 0").error);
  assert.ok(parseCoordinates("10 -91").error);
});

test("coincident records are ordered deterministically", () => {
  const items = [{ kind: "source", id: "b" }, { kind: "observation", id: "z" }, { kind: "candidate", id: "c2" }, { kind: "observation", id: "a" }, { kind: "candidate", id: "c1" }];
  assert.deepEqual(orderItems(items).map((i) => i.id), ["c1", "c2", "a", "z", "b"]);
});

test("transport failures are distinct kinds", async () => {
  assert.equal(classify(503, { error: { code: "ARCHIVE_UNREACHABLE", category: "UPSTREAM_OUTAGE" } }), FAILURE.OUTAGE);
  assert.equal(classify(429, { error: { code: "QUEUE_FULL" } }), FAILURE.QUEUE_FULL);
  assert.equal(classify(401, { error: { code: "UNAUTHORIZED" } }), FAILURE.UNAUTHORIZED);
  assert.equal(classify(413, {}), FAILURE.TOO_LARGE);
  assert.equal(classify(500, { error: { code: "INTERNAL" } }), FAILURE.INTERNAL);
  const respond = (status, body) => async () => ({ ok: status < 400, status, json: async () => (typeof body === "function" ? body() : body) });
  const c = (f, online = true) => new ApiClient({ fetchImpl: f, online: () => online });
  await assert.rejects(c(respond(200, () => { throw new SyntaxError("x"); })).get("/health"), (e) => e.kind === FAILURE.MALFORMED);
  await assert.rejects(c(respond(200, [1])).get("/health"), (e) => e.kind === FAILURE.MALFORMED);
  await assert.rejects(c(async () => { throw new TypeError("fail"); }, false).get("/health"), (e) => e.kind === FAILURE.OFFLINE);
  await assert.rejects(c(async () => { throw new TypeError("fail"); }).get("/health"), (e) => e.kind === FAILURE.UNREACHABLE);
  await assert.rejects(c(async () => { throw Object.assign(new Error("t"), { name: "TimeoutError" }); }).get("/health"), (e) => e.kind === FAILURE.TIMEOUT);
  await assert.rejects(c(respond(400, { error: { code: "INPUT_INVALID", message: "radius" } })).post("/runs", {}), (e) => e.kind === FAILURE.INVALID && e.message === "radius");
  assert.ok(describe({ kind: FAILURE.OUTAGE }).action.includes("not a scientific result"));
});

test("GETs are de-duplicated in flight and immutable records are cached; abort never cancels the shared request", async () => {
  let calls = 0;
  const f = async () => { calls++; await new Promise((r) => setTimeout(r, 5)); return { ok: true, status: 200, json: async () => ({ v: calls }) }; };
  const c = new ApiClient({ fetchImpl: f });
  const ctl = new AbortController();
  const a = c.get("/investigations/tinv:NASA-02:0c714738", { signal: ctl.signal });
  const b = c.get("/investigations/tinv:NASA-02:0c714738");
  ctl.abort();
  await assert.rejects(a, (e) => e.kind === FAILURE.ABORTED);
  assert.deepEqual(await b, { v: 1 });
  assert.deepEqual(await c.get("/investigations/tinv:NASA-02:0c714738"), { v: 1 });
  assert.equal(calls, 1);
  await c.get("/health"); await c.get("/health");
  assert.equal(calls, 3, "mutable resources are not cached");
});

function fakeTimers() {
  let now = 0; const q = [];
  return { now: () => now, setTimeout: (fn, ms) => { const t = { fn, at: now + ms }; q.push(t); return t; }, clearTimeout: (t) => { const i = q.indexOf(t); if (i >= 0) q.splice(i, 1); },
    async advance(ms) { now += ms; for (;;) { q.sort((a, b) => a.at - b.at); const t = q[0]; if (!t || t.at > now) break; q.shift(); await t.fn(); await new Promise((r) => setImmediate(r)); } }, pending: () => q.length };
}

test("job tracker: polls at the documented cadence, never resubmits, stops on terminal, survives reload, backs off on errors", async () => {
  const timers = fakeTimers();
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  const replies = [{ readiness: "PREPARING", job: { state: "DOWNLOADING" }, must_resubmit: false }, new Error("down"),
    { investigation_id: "tinv:NASA-02:0c714738", status: { execution: "COMPLETED" } }];
  const seen = [];
  const client = { get: async (path, opts) => { seen.push([path, opts]); const r = replies.shift(); if (r instanceof Error) throw Object.assign(r, { kind: "unreachable" }); return r; }, post: async () => { throw new Error("must not post"); } };
  const doc = { hidden: false, addEventListener() {} };
  const t = new JobTracker({ client, storage, timers, doc, now: timers.now });
  t.track("job:target_investigation:c03487f9482a", { label: "x" });
  await timers.advance(0);
  assert.equal(t.jobs.get("job:target_investigation:c03487f9482a").summary.state, "DOWNLOADING");
  await timers.advance(ACTIVE_MS);
  assert.ok(t.jobs.get("job:target_investigation:c03487f9482a").error);
  await timers.advance(BACKOFF_MS);
  const s = t.jobs.get("job:target_investigation:c03487f9482a").summary;
  assert.equal(s.terminal, true);
  assert.deepEqual(s.result, { kind: "version", id: "tinv:NASA-02:0c714738" });
  assert.equal(timers.pending(), 0, "terminal jobs are not polled");
  assert.ok(seen.every(([p, o]) => p.startsWith("/jobs/") && o.fresh));
  const reloaded = new JobTracker({ client, storage, timers, doc, now: timers.now });
  assert.ok(reloaded.jobs.has("job:target_investigation:c03487f9482a"), "tracked ids survive a reload");
});

test("job summary: outage with active auto-recovery is dormant, not final; must_resubmit is literal", () => {
  const d = summarize({ readiness: "UNAVAILABLE_AFTER_DEADLINE", must_resubmit: false, job: { state: "UNAVAILABLE_AFTER_DEADLINE", auto_recovery: { active: true, next_check_at: "2026-11-15T20:16:02+00:00" } } });
  assert.equal(d.dormant, true); assert.equal(d.terminal, false); assert.equal(d.mustResubmit, false); assert.equal(d.nextAttempt, "2026-11-15T20:16:02+00:00");
  const stopped = summarize({ readiness: "UNAVAILABLE_AFTER_DEADLINE", must_resubmit: true, job: { state: "UNAVAILABLE_AFTER_DEADLINE", auto_recovery: { active: false } } });
  assert.equal(stopped.terminal, true); assert.equal(stopped.mustResubmit, true);
  assert.equal(summarize({ readiness: "NO_COVERAGE", job: { state: "NO_COVERAGE" } }).terminal, true);
});

test("102 planes follow the published layout: band = detector, planes 51/52 blank", () => {
  const w = Array.from({ length: 102 }, (_, i) => .75 + i * .042);
  assert.deepEqual([planeInfo(1).detector, planeInfo(17).detector, planeInfo(18).detector, planeInfo(102).detector], [1, 1, 2, 6]);
  assert.deepEqual(allPlanes(w).filter((p) => p.blank).map((p) => p.plane), [51, 52]);
  assert.equal(planeInfo(51).channel, 17); assert.equal(planeInfo(52).channel, 1);
  assert.equal(fractionalChannel(w[20], 2, w), 3);
  assert.equal(fractionalChannel(9, 2, w), null, "outside the detector's planes is not guessed");
  const marks = measurementMarks([{ obs_id: "a", detector: 2, wavelength_um: 1.2, status: "MEASURED" }, { obs_id: "b", detector: 2, wavelength_um: null }]);
  assert.equal(marks.length, 1);
});

test("epochs group simultaneous detectors, keep exclusions, and space by elapsed time or equal steps", () => {
  const rows = [{ obs_id: "a1", mjd: 100, detector: 2 }, { obs_id: "a2", mjd: 100.00001, detector: 5 }, { obs_id: "b", mjd: 101, usable: false, reasons: ["SAT"] },
    { obs_id: "c", mjd: 102 }, { obs_id: "d", mjd: 200 }, { obs_id: "bad", mjd: NaN }];
  const e = buildEpochs(rows);
  assert.equal(e.length, 4);
  assert.equal(e[0].observations.length, 2);
  assert.equal(e[1].excluded, true);
  assert.deepEqual(positions(e, "equal"), [0, 1 / 3, 2 / 3, 1]);
  assert.deepEqual(positions(e, "elapsed").map((x) => +x.toFixed(3)), [0, .01, .02, 1]);
  assert.deepEqual(gaps(e).map((g) => g.after), [2]);
  assert.ok(dwell(e, 2, "elapsed") > dwell(e, 0, "elapsed"));
  assert.equal(dwell(e, 0, "equal", 2), 500);
  assert.deepEqual(positions(buildEpochs([{ obs_id: "x", mjd: 5 }]), "elapsed"), [.5], "a single epoch is centred, not stretched");
  assert.ok(formatEpoch(60789.85618796).includes("2025-04-24 20:32") && formatEpoch(51544.5).includes("2000-01-01 12:00"));
});

test("region geometry: even-odd containment and area", () => {
  const sq = [[0, 0], [10, 0], [10, 10], [0, 10]];
  assert.ok(insidePolygon(5, 5, sq)); assert.ok(!insidePolygon(15, 5, sq));
  assert.equal(area(sq), 100);
});
