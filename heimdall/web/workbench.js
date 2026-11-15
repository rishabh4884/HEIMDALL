import { SkyMap, deltaRA } from "./sky.js";
import { wavelengths, equatorialToGalactic } from "./hips.js";
import { renderChart } from "./charts.js";
import { ApiClient, describe, isAbort, FAILURE } from "./api.js";
import { DEFAULT_LAYERS, LAYERS, identify, orderItems, parse, parseCoordinates, serialize, targetOfVersion } from "./state.js";
import { JobTracker } from "./jobs.js";
import { allPlanes, fractionalChannel, measurementMarks, planeInfo, CHANNELS, DETECTOR_RANGE_UM } from "./bands.js";
import { buildEpochs, dwell, formatEpoch, gaps, positions } from "./timeline.js";
import { TwinView } from "./twin.js";
import { action, badge, details, empty, facts, finite, h, json, label, list, number, para, stack, svg, table, traceView } from "./records.js";
import { answerPage, compareView, deeperPage, evidencePage, followupView, historyPage, jobView } from "./views.js";

const $ = (id) => document.getElementById(id);
const client = new ApiClient();
const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const state = {
  targets: [], runs: [], library: "targets", tab: "answer", chart: "motion", axis: 0, trace: [], traceIndex: -1, tableView: false,
  filters: {}, layers: Object.fromEntries(LAYERS.map((l) => [l, DEFAULT_LAYERS.has(l)])), inventory: [], plane: 2, wavelengths: [],
  record: null, doc: null, claims: null, patterns: null, versions: null, attempt: null, map: null, coverage: null, panelErrors: {},
  selection: null, region: null, frame: null, spacing: "elapsed", speed: 1, separated: false, twinPair: null, gen: 0, health: null, capabilities: null,
};
const layerOptions = [
  ["survey", "SPHEREx DR1 image (display mosaic)", "coverage"], ["grid", "Coordinate grid", ""], ["planes", "Galactic equator & ecliptic (computed)", "planes"],
  ["reference", "Catalogue or target position", "track"], ["coverage", "Observation footprints", "coverage"], ["detections", "Measured centroids / detections", "measure"],
  ["sources", "Associated sources", ""], ["candidates", "Candidates (review priority)", "candidate"], ["tracks", "Time-ordered linkages (not orbits)", "track"],
  ["associations", "Source membership", ""], ["catalogue", "Catalogue context", ""], ["inventory", "Indexed sky partitions (metadata only)", "coverage"],
];
const ATLAS = [266.40499, -28.93617, 360];
const INITIAL_SEARCH = location.search;
const announce = (text) => { $("announcer").textContent = ""; requestAnimationFrame(() => { $("announcer").textContent = text; }); };

// ---------------------------------------------------------------- sky
const sky = new SkyMap($("sky"), onCamera, pick, $("map-tooltip"), (text) => {
  const status = planeInfo(state.plane).blank && !text.startsWith("Loading") ? `${text} · dichroic plane intentionally blank` : text;
  const offline = state.health?.offline && text.includes("unavailable") ? " · offline: only cached imagery" : "";
  if ($("image-status").textContent !== status + offline) $("image-status").textContent = status + offline;
}, () => {}, onRegion);
const twin = new TwinView($("twin"), client, announce);
twin.onPair = (pair) => { state.twinPair = pair; syncUrl(false); };
const jobs = new JobTracker({ client, onChange: () => { renderJobBadge(); if ($("dialog").open && $("dialog").dataset.kind === "jobs") renderJobsDialog(); } });

let cameraTimer;
function onCamera(view) {
  const digits = view.span < .02 ? 5 : view.span < 1 ? 3 : 2;
  $("coordinates").textContent = `RA ${view.ra.toFixed(digits)}° · Dec ${view.dec >= 0 ? "+" : ""}${view.dec.toFixed(digits)}°`;
  $("field-size").textContent = view.span === 360 ? "Full sky · 360°" : `Zoom ${number(360 / view.span, 1)}×`;
  $("map-mode").textContent = view.span === 360 ? "Galactic · Hammer (all sky)" : "Recentred Hammer · orientation shown by compass";
  $("all-sky").setAttribute("aria-pressed", view.span === 360);
  $("map-stage").dataset.fullSky = view.span === 360;
  requestAnimationFrame(updateCompass);
  clearTimeout(cameraTimer);
  cameraTimer = setTimeout(() => syncUrl(false), 400);
}

function updateCompass() {
  const o = sky.orientation();
  const full = sky.view.span === 360;
  $("map-compass").hidden = full;
  $("scale-bar").hidden = full;
  if (full) return;
  const set = (id, [x, y], len) => { $(id).setAttribute("x2", x * len); $(id).setAttribute("y2", y * len); };
  set("compass-n", o.north, 20); set("compass-e", o.east, 20);
  $("compass-nl").setAttribute("x", o.north[0] * 26); $("compass-nl").setAttribute("y", o.north[1] * 26 + 4);
  $("compass-el").setAttribute("x", o.east[0] * 26); $("compass-el").setAttribute("y", o.east[1] * 26 + 4);
  const target = 90 / o.pxPerDeg; // degrees spanned by ~90 px
  const nice = [1 / 3600, 2 / 3600, 5 / 3600, 10 / 3600, 30 / 3600, 1 / 60, 2 / 60, 5 / 60, 10 / 60, 30 / 60, 1, 2, 5, 10, 20, 45].reverse().find((d) => d <= target) || 1 / 3600;
  $("scale-bar").querySelector("i").style.width = `${Math.round(nice * o.pxPerDeg)}px`;
  $("scale-bar").querySelector("span").textContent = nice >= 1 ? `${nice}°` : nice >= 1 / 60 ? `${Math.round(nice * 60)}′` : `${Math.round(nice * 3600)}″`;
}

// ---------------------------------------------------------------- URL and history
function viewState() {
  return { record: state.record, camera: sky.view, plane: state.plane, layers: state.layers, filters: state.filters, selection: state.selection,
    frame: state.frame, tab: state.tab, compare: state.compare, twin: state.twinPair, separated: state.separated };
}

function syncUrl(push) {
  if (!state.booted) return; // the incoming link is applied first; nothing may overwrite it before then
  const qs = serialize(viewState());
  if (`?${qs}` === location.search) return;
  history[push ? "pushState" : "replaceState"]({ heimdall: 1 }, "", `${location.pathname}?${qs}`);
}

addEventListener("popstate", () => applyUrl(parse(location.search), { fromHistory: true }));

async function applyUrl(parsed, { initial = false, fromHistory = false } = {}) {
  if (parsed.problems.length) notice(parsed.problems.join(" "), "error");
  if (parsed.plane) setPlane(parsed.plane, { silent: true });
  if (parsed.layers) state.layers = { ...state.layers, ...parsed.layers };
  state.filters = { ...parsed.filters };
  state.separated = parsed.separated;
  state.twinPair = parsed.twin || null;
  state.compare = parsed.compare || null;
  const same = sameRecord(parsed.record, state.record);
  if (!same) await loadRecord(parsed.record || null, { camera: parsed.camera, fromHistory });
  else if (parsed.camera) sky.setView(parsed.camera.ra, parsed.camera.dec, parsed.camera.span);
  state.tab = parsed.tab || "answer";
  state.selection = parsed.selection || null;
  state.frame = parsed.frame || null;
  state.trace = []; state.traceIndex = -1;
  if (state.selection) openTrace(state.selection.kind, state.selection.id, { push: false });
  if (state.compare) openTrace("compare", `${state.compare.a}..${state.compare.b}`, { push: false, request: () => client.get("/compare", { params: state.compare }) });
  if (!parsed.record && initial && !parsed.camera) showAllSky();
  render();
  if (state.selection && !isVisible(state.selection)) announce("The linked selection is hidden by the current filters.");
}

const sameRecord = (a, b) => (a?.kind || null) === (b?.kind || null) && (a?.id || JSON.stringify(a?.field || null)) === (b?.id || JSON.stringify(b?.field || null));

// ---------------------------------------------------------------- records
async function loadRecord(record, { camera = null, fromHistory = false, push = false } = {}) {
  const gen = ++state.gen;
  stopPlayback();
  Object.assign(state, { record, doc: null, claims: null, patterns: null, versions: null, attempt: null, map: null, coverage: null, panelErrors: {},
    region: null, trace: [], traceIndex: -1, frame: fromHistory ? state.frame : null, loading: Boolean(record), error: "" });
  if (!fromHistory) { state.selection = null; state.tab = "answer"; }
  if (push) syncUrl(true);
  render();
  if (!record) { state.loading = false; return; }
  const live = () => gen === state.gen;
  try {
    if (record.kind === "run") {
      const [doc, map] = await Promise.allSettled([client.get(`/runs/${record.id}`), client.get(`/runs/${record.id}/skymap`)]);
      if (!live()) return;
      if (!doc.value) throw doc.reason;
      state.doc = doc.value;
      state.map = map.value || null;
      if (map.reason) state.panelErrors.map = describe(map.reason).message;
      state.chart = "observations";
      if (!state.runs.length) client.get("/runs").then((r) => { state.runs = list(r.runs); }).catch(() => {});
    } else if (record.kind === "field") {
      state.chart = "observations";
      try { state.coverage = await client.get("/coverage", { params: record.field }); }
      catch (e) { if (live()) state.panelErrors.coverage = describe(e).message; }
      if (!live()) return;
      if (state.coverage?.job?.job_id) jobs.track(state.coverage.job.job_id, { label: `Index preparation for ${record.field.ra.toFixed(3)}, ${record.field.dec.toFixed(3)}`, origin: record });
    } else {
      const target = record.kind === "target" ? record.id : targetOfVersion(record.id);
      const [result, versions, attempt] = await Promise.allSettled([
        client.get(record.kind === "version" ? `/investigations/${record.id}` : `/targets/${target}`),
        client.get(`/targets/${target}/versions`), client.get(`/targets/${target}/attempt`)]);
      if (!live()) return;
      state.versions = versions.value || null;
      state.attempt = attempt.value || null;
      if (!result.value) {
        if (result.reason?.kind === FAILURE.NOT_FOUND && record.kind === "target" && state.attempt) state.doc = state.attempt;
        else throw result.reason;
      } else state.doc = result.value;
      const id = state.doc.investigation_id;
      const [claims, patterns] = await Promise.allSettled([client.get(`/investigations/${id}/claims`), client.get(`/investigations/${id}/patterns`)]);
      if (!live()) return;
      state.claims = claims.value || null;
      state.patterns = patterns.value || null;
      if (claims.reason) state.panelErrors.claims = describe(claims.reason).message;
      if (patterns.reason) state.panelErrors.patterns = describe(patterns.reason).message;
      state.chart = list(state.doc.analyses?.motion?.track).length ? "motion" : list(state.doc.analyses?.variability?.light_curve).length ? "flux" : "observations";
    }
    if (camera) sky.setView(camera.ra, camera.dec, camera.span); else focusField();
  } catch (e) {
    if (!live() || isAbort(e)) return;
    const d = describe(e);
    state.error = d.kind === FAILURE.NOT_FOUND
      ? `${label(record.kind)} ${record.id || ""} is not on this server. A link only reopens where the same saved data exists. The rest of the view (camera, plane, layers) was kept.`
      : `${d.title}: ${d.message}. ${d.action}`;
    if (camera) sky.setView(camera.ra, camera.dec, camera.span);
  } finally {
    if (live()) { state.loading = false; render(); }
  }
}

function openRecord(kind, id, extra = {}) {
  state.compare = null;
  if (!kind) return loadRecord(null, { push: true });
  return loadRecord(kind === "field" ? { kind, field: extra.field } : { kind, id }, { push: true });
}

function fieldCenter() {
  const doc = state.doc || {};
  if (state.record?.kind === "field") return state.record.field;
  if (doc.request && finite(doc.request.ra_deg)) return { ra: doc.request.ra_deg, dec: doc.request.dec_deg, radius: doc.request.radius_deg || .05 };
  const center = doc.reference?.position_at_discovery_epoch || doc.reference;
  const pts = list(doc.measurements).map((m) => m.centroid).filter((c) => finite(c?.ra_deg) && finite(c?.dec_deg));
  if (pts.length) {
    const r0 = pts[0].ra_deg;
    const ra = r0 + pts.reduce((s, c) => s + deltaRA(c.ra_deg, r0), 0) / pts.length;
    const dec = pts.reduce((s, c) => s + c.dec_deg, 0) / pts.length;
    const spread = Math.max(...pts.map((c) => Math.max(Math.abs(deltaRA(c.ra_deg, ra)) * Math.cos(dec * Math.PI / 180), Math.abs(c.dec_deg - dec))));
    return { ra, dec, radius: Math.max(.0026, spread * 1.6) };
  }
  if (finite(center?.ra_deg) && finite(center.dec_deg)) return { ra: center.ra_deg, dec: center.dec_deg, radius: .016 };
  const t = state.targets.find((x) => x.target_id === (doc.target?.target_id || state.record?.id));
  return finite(t?.seed?.ra_deg) ? { ra: t.seed.ra_deg, dec: t.seed.dec_deg, radius: .07 } : null;
}

function focusField(tight = false) {
  const c = fieldCenter();
  if (c) sky.setView(c.ra, c.dec, Math.max(tight ? .001 : .12, c.radius * 2.5));
  else sky.setView(...ATLAS);
  drawMap();
}

function showAllSky() {
  sky.setView(...ATLAS);
  drawMap();
  if (state.layers.inventory) loadInventory();
}

// ---------------------------------------------------------------- filtering and metadata
function refreshMetadata() {
  const doc = state.doc || {};
  const index = new Map();
  const add = (id, fields) => { if (id) index.set(id, { ...index.get(id), ...Object.fromEntries(Object.entries(fields).filter(([, v]) => v != null)) }); };
  for (const o of list(state.coverage?.observations)) add(o.obs_id, { detector: o.detector, release: o.release, mjd: o.t_min_mjd, waveMin: o.wavelength_min_um, waveMax: o.wavelength_max_um });
  for (const p of list(doc.products || doc.input_manifest?.products)) add(p.obs_id, { release: p.release, sha256: p.sha256 });
  for (const o of list(doc.observations)) add(o.obs_id, { detector: o.meta?.detector, release: o.meta?.release, mjd: o.meta?.mjd_avg, usable: o.included, sha256: o.product_checksum });
  for (const m of list(doc.measurements)) add(m.obs_id, { detector: m.detector, mjd: m.mjd, wave: m.wavelength_um, usable: m.status === "MEASURED" });
  for (const m of list(doc.analyses?.variability?.light_curve)) add(m.obs_id, { usable: m.usable });
  state.metadata = index;
  const all = [...index.values()];
  for (const id of ["detector", "release"]) {
    const values = [...new Set(all.map((v) => v[id]).filter((v) => v != null))].sort();
    $(id).innerHTML = `<option value="">${id === "detector" ? "All detectors" : "All releases"}</option>` + values.map((v) => `<option value="${h(v)}">${h(id === "detector" ? `Detector ${v}` : v)}</option>`).join("");
    $(id).value = state.filters[id] || "";
  }
  $("quality").value = state.filters.quality || "";
  for (const [id, key] of [["mjd-start", "start"], ["mjd-end", "end"], ["wave-start", "waveStart"], ["wave-end", "waveEnd"]]) if (document.activeElement !== $(id)) $(id).value = state.filters[key] ?? "";
}

function matches(record, applyFrame = false) {
  if (!record) return false;
  const f = state.filters, m = state.metadata?.get(record.obs_id) || {};
  const detector = record.detector ?? record.meta?.detector ?? m.detector;
  const release = record.release || record.meta?.release || m.release;
  const mjd = record.mjd ?? record.meta?.mjd_avg ?? record.t_min_mjd ?? m.mjd;
  const wave = record.wavelength_um ?? m.wave;
  const lo = wave ?? record.wavelength_min_um ?? m.waveMin, hi = wave ?? record.wavelength_max_um ?? m.waveMax;
  const usable = record.usable ?? record.included ?? m.usable;
  if (f.detector && String(detector) !== String(f.detector)) return false;
  if (f.release && release !== f.release) return false;
  if (f.quality && usable !== (f.quality === "usable")) return false;
  if (f.start !== undefined && (!finite(mjd) || mjd < f.start)) return false;
  if (f.end !== undefined && (!finite(mjd) || mjd > f.end)) return false;
  if (f.waveStart !== undefined && (!finite(hi) || hi < f.waveStart)) return false;
  if (f.waveEnd !== undefined && (!finite(lo) || lo > f.waveEnd)) return false;
  return !applyFrame || !state.frame || frameIds().has(record.obs_id);
}

const activeFilters = () => Object.entries(state.filters).filter(([, v]) => v !== undefined && v !== "");
function isVisible(sel) {
  if (!sel || !["observation", "detection"].includes(sel.kind)) return true;
  const rec = sel.kind === "observation" ? { obs_id: sel.id } : list(state.doc?.detections).find((d) => d.det_id === sel.id);
  return !rec || matches(rec);
}

function updateFilter() {
  const num = (id) => $(id).value === "" || !finite($(id).valueAsNumber) ? undefined : $(id).valueAsNumber;
  const start = num("mjd-start"), end = num("mjd-end"), waveStart = num("wave-start"), waveEnd = num("wave-end");
  const mjdBad = finite(start) && finite(end) && start > end, waveBad = finite(waveStart) && finite(waveEnd) && waveStart > waveEnd;
  $("mjd-error").textContent = mjdBad ? "End MJD must be at or after the start." : "";
  $("wave-error").textContent = waveBad ? "Maximum wavelength must be at or above the minimum." : "";
  $("mjd-end").setAttribute("aria-invalid", mjdBad); $("wave-end").setAttribute("aria-invalid", waveBad);
  if (mjdBad || waveBad) return; // keep the entered values; the previous valid filter stays applied
  state.filters = Object.fromEntries(Object.entries({ detector: $("detector").value, release: $("release").value, quality: $("quality").value, start, end, waveStart, waveEnd })
    .filter(([, v]) => v !== undefined && v !== ""));
  if (state.frame && !epochs().some((e) => e.id === state.frame)) state.frame = null;
  syncUrl(true);
  render();
}

// ---------------------------------------------------------------- map layers
function polygon(p) {
  if (typeof p === "string" && /^POLYGON\s/i.test(p)) {
    const v = p.trim().replace(/^POLYGON\s+(?:[A-Z]+\s+)?/i, "").split(/[\s,]+/).map(Number);
    if (v.length >= 6 && v.length % 2 === 0 && v.every(finite)) return v.flatMap((x, i) => i % 2 ? [] : [[x, v[i + 1]]]);
  }
  if (list(p?.ra).length === list(p?.dec).length && list(p?.ra).length > 2) {
    const pts = p.ra.map((ra, i) => [ra, p.dec[i]]);
    return pts.every(([x, y]) => finite(x) && finite(y)) ? pts : [];
  }
  return [];
}

// Galactic equator (b = 0) and ecliptic (J2000 obliquity 23.4393°): defined reference frames, not catalogue data.
const referencePlanes = (() => {
  const gal = [], ecl = [];
  const RAD = Math.PI / 180, eps = 23.4392911 * RAD;
  for (let l = 0; l <= 360; l += 2) {
    // invert the galactic rotation numerically via the ecliptic-free formula: sample RA/Dec on b=0 by search
    const lam = l * RAD;
    const ra = Math.atan2(Math.sin(lam) * Math.cos(eps), Math.cos(lam)) / RAD, dec = Math.asin(Math.sin(eps) * Math.sin(lam)) / RAD;
    ecl.push([(ra + 360) % 360, dec]);
  }
  for (let ra = 0; ra <= 360; ra += 0.5) {
    // b = 0: find dec where galactic latitude changes sign along this RA
    let lo = -90, hi = 90;
    const b = (d) => equatorialToGalactic(ra, d)[1];
    if (Math.sign(b(lo)) === Math.sign(b(hi))) continue;
    for (let k = 0; k < 40; k++) { const mid = (lo + hi) / 2; if (Math.sign(b(mid)) === Math.sign(b(lo))) lo = mid; else hi = mid; }
    gal.push([ra, (lo + hi) / 2]);
  }
  return [{ coords: gal, label: "Galactic equator" }, { coords: ecl, label: "Ecliptic" }];
})();

function frameIds() {
  const e = epochs().find((x) => x.id === state.frame);
  return new Set(e ? e.observations.map((o) => o.obs_id) : []);
}

function canvasLayers() {
  const doc = state.doc || {};
  const d = state.map?.layers || {};
  const c = fieldCenter();
  const atFrame = (r) => matches(r, true);
  const p = doc.reference?.position_at_discovery_epoch || doc.reference;
  const ref = finite(p?.ra_deg) && finite(p.dec_deg) ? [{ ra: p.ra_deg, dec: p.dec_deg, label: "Catalogue position at recorded epoch", id: "reference", kind: "reference" }] : [];
  if (c && !ref.length && state.record?.kind === "target") ref.push({ ...c, id: "target-seed", kind: "reference", label: "Target seed position" });
  if (sky.view.span === 360 || !state.record) for (const t of state.targets) {
    if (finite(t.seed?.ra_deg)) ref.push({ ra: t.seed.ra_deg, dec: t.seed.dec_deg, id: t.target_id, kind: "target", label: `${t.name} · registered target seed` });
  }
  const measurements = list(doc.measurements).filter(atFrame).filter((m) => finite(m.centroid?.ra_deg) && finite(m.centroid?.dec_deg));
  const allowedDet = new Set(list(d.detections).filter(atFrame).map((x) => x.det_id));
  const sourceIds = new Set(list(d.detections).filter((x) => allowedDet.has(x.det_id)).map((x) => x.source_id));
  const narrowed = activeFilters().length > 0 || Boolean(state.frame);
  const footprints = [...list(d.coverage), ...list(state.coverage?.observations)].filter(atFrame).map((o) => ({
    coords: polygon(o.polygon || o.footprint), closed: true, dashed: o.included === false, selected: o.obs_id === state.selection?.id }));
  const byObs = new Map(measurements.map((m) => [m.obs_id, m.centroid]));
  const track = list(doc.analyses?.motion?.track).filter(atFrame).sort((a, b) => a.mjd - b.mjd).map((t) => byObs.get(t.obs_id)).filter(Boolean).map((q) => [q.ra_deg, q.dec_deg]);
  const tracks = list(d.tracks).flatMap((t) => {
    const pts = list(t.points);
    return pts.slice(1).flatMap((q, i) => allowedDet.has(q.det_id) && allowedDet.has(pts[i].det_id) ? [{ coords: [[pts[i].ra, pts[i].dec], [q.ra, q.dec]], dashed: Boolean(t.ambiguous) }] : []);
  });
  if (track.length > 1 && !narrowed) tracks.push({ coords: track, dashed: true });
  const cat = state.map?.external_layers?.star_catalogue;
  return {
    synthetic: doc.data_origin === "SYNTHETIC",
    planes: referencePlanes,
    reference: ref,
    coverage: footprints.filter((o) => o.coords.length),
    detections: [
      ...list(d.detections).filter((x) => allowedDet.has(x.det_id)).map((x) => ({ ...x, id: x.det_id, kind: "detection", ellipse: x.ellipse_1sigma, label: `Pipeline detection · ${x.obs_id}` })),
      ...measurements.map((m) => ({ ra: m.centroid.ra_deg, dec: m.centroid.dec_deg, id: m.obs_id, kind: "observation", label: `Measured centroid · ${m.obs_id} · MJD ${number(m.mjd, 3)} · ${number(m.wavelength_um, 3)} µm` })),
    ],
    sources: list(d.sources).filter((x) => !narrowed || sourceIds.has(x.source_id)).map((x) => ({ ...x, id: x.source_id, kind: "source", ellipse: x.ellipse_1sigma, label: `Source · ${x.source_id}` })),
    candidates: narrowed ? [] : list(d.candidates).map((x) => ({ ...x, id: x.candidate_id, kind: "candidate", ellipse: x.ellipse_1sigma, label: `Candidate · ${label(x.kind)} · review priority` })),
    tracks,
    associations: list(d.associations).flatMap((a) => list(a.segments).filter((s) => allowedDet.has(s.det_id)).map((s) => ({ coords: [s.from, s.to], dashed: true }))),
    catalogue: list(cat?.items).map((s) => ({ ra: s.ra ?? s.ra_deg, dec: s.dec ?? s.dec_deg, label: `${cat.source || "Catalogue"} · ${s.source_id || "source"}` })),
    inventory: sky.view.span === 360 ? state.inventory : [],
  };
}

function drawMap() {
  const data = canvasLayers();
  sky.highlight = new Set([...frameIds(), ...list(state.region?.items).map((i) => i.id)]);
  sky.setData(data, state.layers, state.selection?.id);
  $("focus-field").disabled = !fieldCenter();
  $("layers").innerHTML = layerOptions.map(([id, title, tone]) => {
    const total = Array.isArray(data[id]) ? data[id].length : 0;
    const always = ["survey", "grid", "planes", "inventory"].includes(id);
    return `<label class="layer" data-tone="${tone}"><i aria-hidden="true"></i><span>${title}${always ? "" : ` <small>(${total})</small>`}</span><input data-layer="${id}" type="checkbox" ${state.layers[id] ? "checked" : ""} ${!total && !always ? "disabled" : ""}></label>`;
  }).join("") + `<p class="compact">Positions: measured (gold), sources (blue), candidates (pink diamonds), linkages (teal). Lines are measured-position links, never orbits. No distance, morphology or Planet X location is drawn.</p>`;
  $("map-legend").innerHTML = layerOptions.filter(([id]) => state.layers[id] && !["survey", "grid"].includes(id) && (Array.isArray(data[id]) ? data[id].length : 0))
    .map(([id, title, tone]) => `<span data-tone="${tone || id}"><i></i>${h(title.split(" (")[0])}</span>`).join("");
  const origin = state.doc?.data_origin;
  const banner = origin === "SYNTHETIC" ? "SYNTHETIC · not real observations"
    : state.panelErrors.map ? `Map layer unavailable: ${state.panelErrors.map}`
    : state.coverage?.readiness === "PREPARING" ? "Metadata index for this region is being prepared (no analysis is running)."
    : state.record && !state.loading && !data.detections.length && !data.coverage.length && sky.view.span < 360 ? "No plotted observations in this selection."
    : sky.view.span === 360 && state.layers.inventory ? (state.inventoryLoading ? "Loading recorded partition states…" : "Shaded partitions: searched in the metadata index. Indexed ≠ downloaded ≠ analysed; all-sky completeness is not established.") : "";
  $("map-banner").textContent = banner;
  $("map-banner").hidden = !banner;
  $("map-banner").dataset.tone = origin === "SYNTHETIC" || state.panelErrors.map ? "warning" : "";
}

async function loadInventory() {
  if (state.inventory.length || state.inventoryLoading) return;
  state.inventoryLoading = true;
  drawMap();
  try {
    const data = await client.get("/inventory/domains");
    const pages = await Promise.allSettled(list(data.domains).map(async (dom) => {
      const [tile, domain] = dom.split(":");
      const r = await client.get("/inventory/partitions", { params: { domain, tile_deg: tile.slice(1), limit: 2000 } });
      return list(r.page);
    }));
    state.inventory = pages.flatMap((p) => p.value || []).filter((r) => r.state !== "SUBDIVIDED").map((r) => ({
      coords: [[r.ra_min, r.dec_min], [r.ra_max, r.dec_min], [r.ra_max, r.dec_max], [r.ra_min, r.dec_max]], closed: true,
      certified: ["SEARCHED_HAS_OBS", "SEARCHED_EMPTY"].includes(r.state) }));
  } catch (e) { notice(`Inventory unavailable: ${describe(e).message}`, "error"); }
  finally { state.inventoryLoading = false; drawMap(); }
}

// ---------------------------------------------------------------- selection
function pick(item, overlaps = []) {
  stopPlayback("inspection");
  const distinct = orderItems([...new Map([item, ...overlaps].filter((i) => i.kind).map((i) => [`${i.kind}:${i.id}`, i])).values()]);
  if (distinct.length > 1) return showOverlap(distinct);
  choose(item);
}

function choose(item) {
  $("overlap").hidden = true;
  if (item.kind === "target") return openRecord("target", item.id);
  if (item.kind === "reference") return select("reference", item.id === "target-seed" ? "target" : "reference");
  if (item.kind) select(item.kind, item.id);
}

function showOverlap(items) {
  const box = $("overlap");
  box.innerHTML = `<p class="compact">${items.length} records coincide here. Choose one:</p>${stack(items.map((i) => `<button type="button" class="record-link" data-overlap="${h(i.kind)}|${h(i.id)}"><span>${h(i.label || i.id)}<small>${h(label(i.kind))}</small></span></button>`))}
    <button type="button" class="text-button" data-overlap="close">Cancel</button>`;
  box.hidden = false;
  box.querySelector("button")?.focus();
}

function onRegion(items, skyPolygon, mode) {
  const all = orderItems([...new Map(items.filter((i) => i.kind && i.kind !== "target").map((i) => [`${i.kind}:${i.id}`, i])).values()]);
  const plottedTotal = list(state.doc?.measurements).length + list(state.map?.layers?.detections).length;
  const plottedNow = canvasLayers().detections.length;
  state.region = { items: all, mode, sky: skyPolygon, hidden: activeFilters().length ? Math.max(0, plottedTotal - plottedNow) : 0 };
  state.tab = "evidence";
  state.trace = []; state.traceIndex = -1;
  setPointerMode("pan");
  openPanel("investigations");
  render();
  announce(`${all.length} records selected in the region.`);
}

function selectVisible() {
  const g = sky.geometry();
  onRegion(sky.within([[g.left, g.top], [g.right, g.top], [g.right, g.bottom], [g.left, g.bottom]]), null, "visible area");
}

function select(kind, id) {
  state.selection = { kind, id };
  if (kind === "source") state.chart = "spectrum";
  openTrace(kind, id, { push: true });
  openPanel("investigations");
  renderVisuals();
  if (!isVisible(state.selection)) announce("Selected record is hidden by the current filters; see the filter notice.");
}

function openTrace(kind, id, { push = false, request = null } = {}) {
  const item = { kind, id, returnTab: state.tab };
  state.trace.splice(state.traceIndex + 1, Infinity, item);
  state.traceIndex = state.trace.length - 1;
  if (push) syncUrl(true);
  renderInspector();
  $("record").scrollTop = 0;
  if (request) {
    request().then((data) => { item.data = data; }, (e) => { item.data = { error: describe(e).message }; })
      .finally(() => { if (state.trace[state.traceIndex] === item) renderInspector(); });
  }
}

// ---------------------------------------------------------------- wavelength strip and Separate / Merge
function renderStrip() {
  const strip = $("band-strip");
  const planes = allPlanes(state.wavelengths);
  const f = state.filters;
  const inRange = (p) => p.wavelength == null || ((f.waveStart === undefined || p.wavelength >= f.waveStart) && (f.waveEnd === undefined || p.wavelength <= f.waveEnd));
  const marks = measurementMarks(list(state.doc?.measurements).filter((m) => matches(m)));
  const lanes = [1, 2, 3, 4, 5, 6].map((det) => {
    const cells = planes.filter((p) => p.detector === det).map((p) => `<button type="button" role="option" id="plane-${p.plane}" class="plane" data-plane="${p.plane}"
      aria-selected="${p.plane === state.plane}" data-blank="${p.blank}" data-out="${!inRange(p)}" tabindex="-1"
      aria-label="Plane ${p.plane}, detector ${p.detector} channel ${p.channel}${p.wavelength ? `, ${p.wavelength.toFixed(3)} micrometres` : ""}${p.blank ? ", intentionally blank dichroic channel" : ""}"><span>${p.plane}</span></button>`).join("");
    const ticks = marks.filter((m) => m.detector === det).map((m) => {
      const k = fractionalChannel(m.wavelength, det, state.wavelengths);
      return k == null ? "" : `<button type="button" class="mark ${m.usable ? "" : "excluded"}" data-mark="${h(m.obs_id)}" data-pos="${((k + .5) / CHANNELS).toFixed(4)}"
        title="Measurement ${h(m.obs_id)} at ${m.wavelength.toFixed(4)} µm (${h(m.source)})" aria-label="Measurement ${h(m.obs_id)} at ${m.wavelength.toFixed(4)} micrometres"></button>`;
    }).join("");
    const r = DETECTOR_RANGE_UM[det];
    return `<div class="lane" data-detector="${det}"><span class="lane-label">D${det} <small>${r[0]}–${r[1]} µm</small></span><div class="lane-cells">${cells}</div><div class="lane-marks">${ticks}</div></div>`;
  }).join("");
  strip.innerHTML = lanes;
  strip.dataset.separated = state.separated;
  strip.setAttribute("aria-activedescendant", `plane-${state.plane}`);
  for (const m of strip.querySelectorAll(".mark[data-pos]")) m.style.left = `${Number(m.dataset.pos) * 100}%`;
  $("separate").setAttribute("aria-pressed", state.separated);
  $("separate").textContent = state.separated ? "Merge" : "Separate";
  $("separate-note").textContent = state.separated
    ? "Separated: the six detectors image the sky simultaneously, each through its own linear variable filter (17 channels). Illustrative arrangement only: sky positions and identities are unchanged; gold ticks are this record's measurements at their calibrated wavelengths."
    : `Merged: one wavelength-ordered axis of 102 survey planes. Merging is display ordering only: it does not coadd images or improve signal-to-noise.${marks.length ? ` Gold ticks: ${marks.length} measurement(s) of this record at calibrated wavelengths (a survey plane is not a measurement).` : ""}`;
}

function toggleSeparate() {
  const strip = $("band-strip");
  const before = new Map([...strip.querySelectorAll(".plane")].map((el) => [el.dataset.plane, el.getBoundingClientRect()]));
  state.separated = !state.separated;
  renderStrip();
  syncUrl(true);
  announce(state.separated ? "Separated into six detector lanes." : "Merged into one wavelength axis.");
  if (reducedMotion()) return;
  for (const el of strip.querySelectorAll(".plane")) {
    const a = before.get(el.dataset.plane), b = el.getBoundingClientRect();
    if (!a) continue;
    el.animate([{ transform: `translate(${a.left - b.left}px, ${a.top - b.top}px) scale(${a.width / b.width}, ${a.height / b.height})` }, { transform: "none" }],
      { duration: 420, easing: "cubic-bezier(.2,.7,.2,1)" });
  }
}

function setPlane(value, { silent = false } = {}) {
  if (!Number.isInteger(value)) return;
  state.plane = Math.max(1, Math.min(102, value));
  const p = planeInfo(state.plane, state.wavelengths);
  $("plane-prev").disabled = state.plane === 1;
  $("plane-next").disabled = state.plane === 102;
  $("wavelength-label").textContent = `Plane ${state.plane}/102 · D${p.detector} ch ${p.channel}${p.wavelength ? ` · ${p.wavelength.toFixed(3)} µm nominal` : ""}${p.blank ? " · blank (dichroic)" : ""}`;
  sky.survey.settings(state.plane - 1, state.layers.survey, Number($("image-brightness").value));
  for (const el of $("band-strip").querySelectorAll(".plane")) el.setAttribute("aria-selected", Number(el.dataset.plane) === state.plane);
  $("band-strip").setAttribute("aria-activedescendant", `plane-${state.plane}`);
  if (!silent) syncUrl(false);
}

// ---------------------------------------------------------------- time ribbon
function epochRows() {
  const doc = state.doc || {};
  const lc = new Map(list(doc.analyses?.variability?.light_curve).map((p) => [p.obs_id, p]));
  if (list(doc.measurements).length) return doc.measurements.map((m) => ({ obs_id: m.obs_id, mjd: m.mjd, detector: m.detector, wavelength_um: m.wavelength_um,
    usable: lc.get(m.obs_id)?.usable ?? m.status === "MEASURED", reasons: [...list(m.reasons), ...list(lc.get(m.obs_id)?.reasons)] }));
  if (list(doc.observations).length) return doc.observations.map((o) => ({ obs_id: o.obs_id, mjd: o.meta?.mjd_avg, detector: o.meta?.detector, usable: o.included, reasons: list(o.exclusion_reasons) }));
  return list(state.coverage?.observations).map((o) => ({ obs_id: o.obs_id, mjd: o.t_min_mjd, detector: o.detector }));
}

let epochCache = { key: null, value: [] };
function epochs() {
  const key = `${state.gen}|${JSON.stringify(state.filters)}|${state.loading}`;
  if (epochCache.key !== key) epochCache = { key, value: buildEpochs(epochRows().filter((r) => matches(r))) };
  return epochCache.value;
}

function renderRibbon() {
  const eps = epochs();
  $("ribbon").hidden = !eps.length;
  if (!eps.length) return;
  const pos = positions(eps, state.spacing);
  const i = eps.findIndex((e) => e.id === state.frame);
  const track = $("ribbon-track");
  const gapMarks = state.spacing === "elapsed" ? gaps(eps).map((g) => `<span class="gap" data-a="${pos[g.after]}" data-b="${pos[g.after + 1]}" title="${number(g.days, 1)} days without observations"><small>${number(g.days, 0)} d gap</small></span>`).join("") : "";
  track.innerHTML = `<div class="ribbon-line"></div>${gapMarks}${eps.map((e, k) => `<button type="button" class="epoch" data-epoch="${h(e.id)}" data-pos="${pos[k]}" aria-current="${k === i}" data-excluded="${e.excluded}" tabindex="-1"
    title="${h(formatEpoch(e.mjd))} · ${e.observations.length} observation(s)${e.excluded ? " · all excluded" : ""}" aria-label="Epoch ${k + 1}: ${h(formatEpoch(e.mjd))}, ${e.observations.length} observations${e.excluded ? ", excluded" : ""}"><i></i></button>`).join("")}`;
  for (const el of track.querySelectorAll("[data-pos]")) el.style.left = `${Number(el.dataset.pos) * 100}%`;
  for (const el of track.querySelectorAll(".gap")) { el.style.left = `${Number(el.dataset.a) * 100}%`; el.style.width = `${(Number(el.dataset.b) - Number(el.dataset.a)) * 100}%`; }
  track.setAttribute("aria-valuemax", eps.length);
  track.setAttribute("aria-valuenow", i + 1 || 1);
  track.setAttribute("aria-valuetext", i >= 0 ? `Epoch ${i + 1} of ${eps.length}: ${formatEpoch(eps[i].mjd)}` : `All ${eps.length} epochs shown`);
  const cur = eps[i];
  const excl = cur?.observations.filter((o) => o.usable === false) || [];
  $("frame-label").textContent = cur ? `${i + 1}/${eps.length} · ${formatEpoch(cur.mjd)} · ${cur.observations.length} obs (${[...new Set(cur.observations.map((o) => `D${o.detector}`))].join(" ")})${excl.length ? ` · ${excl.length} excluded: ${[...new Set(excl.flatMap((o) => o.reasons))].slice(0, 2).join(", ")}` : ""}`
    : `${eps.length} epochs · ${formatEpoch(eps[0].mjd).split(" · ")[0]} → ${formatEpoch(eps[eps.length - 1].mjd).split(" · ")[0]}`;
  $("epoch-prev").disabled = i <= 0;
  $("epoch-next").disabled = i >= eps.length - 1;
}

function setEpoch(i, { push = false } = {}) {
  const eps = epochs();
  state.frame = eps[Math.max(0, Math.min(eps.length - 1, i))]?.id || null;
  state.frameIds = [...frameIds()];
  followEpochInTwin();
  if (push) syncUrl(true); else syncUrl(false);
  renderVisuals();
}

// With Compare images open, image B follows the ribbon: the observation of the current epoch on image A's detector.
function followEpochInTwin() {
  if (document.querySelector(".workspace").dataset.panel !== "twin" || !twin.pair || !twin.ctx) return;
  const a = twin.ctx.candidates.find((c) => c.obs_id === twin.pair.a);
  const ids = frameIds();
  const b = twin.ctx.candidates.find((c) => ids.has(c.obs_id) && c.detector === a?.detector && c.obs_id !== twin.pair.a);
  if (!b || b.obs_id === twin.pair.b) return;
  twin.pair = { ...twin.pair, b: b.obs_id };
  state.twinPair = twin.pair;
  twin.data = null;
  twin.load();
}

function stopPlayback(reason) {
  if (!state.timer && !state.playing) return;
  clearTimeout(state.timer);
  state.timer = null;
  state.playing = false;
  $("play").innerHTML = svg("play");
  $("play").setAttribute("aria-label", "Play real epochs");
  if (reason) announce(`Playback paused (${reason}).`);
}

function play() {
  if (state.playing) return stopPlayback("by you");
  const eps = epochs();
  if (eps.length < 2) return;
  state.playing = true;
  $("play").innerHTML = svg("pause");
  $("play").setAttribute("aria-label", "Pause playback");
  let i = Math.max(0, eps.findIndex((e) => e.id === state.frame));
  if (i >= eps.length - 1) i = 0;
  const step = () => {
    if (!state.playing) return;
    setEpoch(i);
    if (i >= eps.length - 1) return stopPlayback("end of the recorded epochs");
    state.timer = setTimeout(() => { i++; step(); }, dwell(eps, i, state.spacing, state.speed));
  };
  step();
}
document.addEventListener("visibilitychange", () => { if (document.hidden) stopPlayback("page hidden"); });

// ---------------------------------------------------------------- rendering
function renderHeader() {
  const doc = state.doc;
  const r = state.record;
  const target = doc?.target || state.targets.find((t) => t.target_id === (r?.kind === "target" ? r.id : targetOfVersion(r?.id)));
  $("title").textContent = target?.name || (doc?.run_id ? `Field run ${doc.run_id}` : r?.kind === "field" ? `Region ${r.field.ra.toFixed(4)}, ${r.field.dec >= 0 ? "+" : ""}${r.field.dec.toFixed(4)}` : "Explore the sky");
  $("subtitle").textContent = doc?.investigation_id ? `${doc.investigation_id} · ${label(target?.category)}` : doc?.run_id ? "Saved SPHEREx blind field run" :
    r?.kind === "field" ? "Coverage browsing (metadata only; no analysis)" : "Explore SPHEREx observations and trace findings to their evidence.";
  $("heading-chips").innerHTML = doc ? [badge(doc.data_origin === "REAL" ? "Real data (stored)" : doc.data_origin, doc.data_origin === "SYNTHETIC" ? "warn" : "good"),
    badge(doc.status?.execution ? `Run: ${label(doc.status.execution)}` : ""), badge(doc.status?.test_status ? `Test: ${label(doc.status.test_status)}` : "", "warn"),
    badge(doc.created_at ? `Saved ${doc.created_at.slice(0, 10)}` : "")].join("") : "";
  $("record-header").innerHTML = `<h2 id="record-title">${h(doc?.investigation_id || doc?.run_id || (r?.kind === "field" ? "Coverage" : "Investigation"))}</h2>`;
  document.title = `${$("title").textContent} · HEIMDALL`;
}

function renderInspector() {
  const root = $("record");
  const active = state.trace[state.traceIndex];
  $("trace-nav").hidden = !state.trace.length;
  $("trace-back").disabled = !active;
  $("trace-forward").disabled = state.traceIndex + 1 >= state.trace.length;
  $("trace-label").textContent = active ? `${label(active.kind)} · ${active.id}` : "";
  for (const b of $("inspector-tabs").querySelectorAll("[data-tab]")) {
    b.setAttribute("aria-selected", b.dataset.tab === state.tab && !active);
    b.tabIndex = b.dataset.tab === state.tab ? 0 : -1;
  }
  root.setAttribute("aria-labelledby", `tab-${state.tab}`);
  root.setAttribute("aria-busy", Boolean(state.loading));
  const hidden = state.selection && !isVisible(state.selection)
    ? `<p class="notice compact">The selected ${h(state.selection.kind)} ${h(state.selection.id)} is hidden by the current filters; it stays selected. <button type="button" class="text-button" data-action="clear-filters">Clear filters</button></p>` : "";
  if (state.loading) root.innerHTML = empty("Loading investigation…");
  else if (state.error) root.innerHTML = empty(state.error, true) + stack([action("Search the sky instead", "region-dialog")]);
  else if (active) {
    root.innerHTML = hidden + (active.kind === "compare" ? (active.data ? (active.data.error ? empty(active.data.error, true) : compareView(active.data)) : empty("Comparing…"))
      : active.kind === "followup" ? (active.data ? (active.data.error ? empty(active.data.error, true) : followupView(active.data)) : empty("Loading follow-up plan…"))
      : traceView(state, active));
  } else if (!state.record) root.innerHTML = `${empty("Choose a target or saved field in the library, search above, or select a region of the sky.")}${stack([action("Take the guided tour", "guide")])}`;
  else {
    const pages = { answer: answerPage, evidence: (s) => evidencePage(s, (r) => matches(r)), deeper: deeperPage, history: historyPage };
    root.innerHTML = hidden + pages[state.tab](state);
  }
}

function renderLibrary() {
  for (const b of $("library-tabs").querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.library === state.library);
  const targets = state.library === "targets";
  const rows = targets ? state.targets : state.runs;
  $("library").setAttribute("aria-busy", "false");
  const cur = state.record?.kind === "run" ? state.record.id : state.doc?.target?.target_id || (state.record?.kind === "target" ? state.record.id : null);
  $("library").innerHTML = rows.map((r) => {
    const id = targets ? r.target_id : r.run_id;
    const sub = targets ? `${id} · ${r.saved?.status?.test_status ? label(r.saved.status.test_status) : "No saved result"}` : `${label(r.data_origin)} · ${label(r.status?.execution)} · ${r.n_candidates ?? 0} candidates`;
    return `<button type="button" class="library-item" data-action="${targets ? "target" : "run"}" data-id="${h(id)}" aria-current="${id === cur}"><strong>${h(targets ? r.name : r.run_id)}</strong><small>${h(sub)}</small></button>`;
  }).join("") || empty(targets ? "No targets." : "No saved field runs.");
}

function renderFilterSummary() {
  const n = activeFilters().length;
  $("filter-count").hidden = !n;
  $("filter-count").textContent = n;
  const rows = epochRows();
  const shown = rows.filter((r) => matches(r)).length;
  $("filter-summary").textContent = n ? `${n} active filter(s): ${activeFilters().map(([k, v]) => `${k} ${v}`).join(", ")} · ${shown} of ${rows.length} observations shown.` : rows.length ? `No filters · ${rows.length} observations.` : "";
  const region = state.region ? ` · region: ${state.region.items.length} selected` : "";
  $("selection-summary").innerHTML = `${state.selection ? `Selected ${h(label(state.selection.kind))} ${h(state.selection.id)}` : ""}${h(region)}${n ? ` · <button type="button" class="text-button light" data-action="clear-filters">Clear ${n} filter(s)</button>` : ""}${state.selection || state.region ? ` · <button type="button" class="text-button light" data-action="clear-selection">Clear selection</button>` : ""} · <button type="button" class="text-button light" data-action="select-visible">Select visible</button>`;
}

function twinContext() {
  const doc = state.doc || {};
  const seriesOf = new Map();
  for (const s of list(doc.analyses?.variability?.series)) for (const p of list(s.points)) seriesOf.set(p.obs_id, s.series_id);
  const meas = new Map(list(doc.measurements).map((m) => [m.obs_id, m]));
  const candidates = [...state.metadata?.entries() || []].filter(([, m]) => m.sha256).map(([obs, m]) => ({ obs_id: obs, sha256: m.sha256, detector: m.detector, mjd: m.mjd,
    wavelength_um: meas.get(obs)?.wavelength_um ?? m.wave, series: seriesOf.get(obs) })).sort((a, b) => (a.detector - b.detector) || (a.mjd - b.mjd));
  const c = list(doc.measurements).find((m) => finite(m.centroid?.ra_deg))?.centroid;
  const ref = doc.reference?.position_at_discovery_epoch || doc.reference;
  const centre = finite(ref?.ra_deg) ? { ra: ref.ra_deg, dec: ref.dec_deg } : c ? { ra: c.ra_deg, dec: c.dec_deg } : doc.request ? { ra: doc.request.ra_deg, dec: doc.request.dec_deg } : null;
  return { candidates, centre, investigation: doc.investigation_id || null, synthetic: doc.data_origin === "SYNTHETIC", pair: state.twinPair };
}

let twinKey = null;
function renderVisuals() {
  state.frameIds = [...frameIds()];
  drawMap();
  renderStrip();
  renderRibbon();
  $("chart-kind").value = state.chart;
  $("chart-table").setAttribute("aria-pressed", state.tableView);
  renderChart($("chart"), $("chart-subtitle"), state, (r) => matches(r));
  const key = `${state.gen}|${state.doc?.investigation_id || state.doc?.run_id || ""}`;
  if (key !== twinKey && !state.loading) { twinKey = key; twin.setContext(twinContext()); }
  renderFilterSummary();
}

function render() {
  refreshMetadata();
  renderHeader();
  renderInspector();
  renderVisuals();
  renderLibrary();
}

function setTab(tab) {
  state.tab = tab;
  state.trace = []; state.traceIndex = -1;
  state.compare = null;
  syncUrl(true);
  renderInspector();
  $("record").scrollTop = 0;
}

function openPanel(panel) {
  const ws = document.querySelector(".workspace");
  ws.dataset.panel = panel;
  for (const b of document.querySelectorAll(".map-tools [data-panel]")) b.setAttribute("aria-expanded", b.dataset.panel === panel);
  twin.show(panel === "twin");
  if (panel === "charts") renderChart($("chart"), $("chart-subtitle"), state, (r) => matches(r));
}

function setPointerMode(mode) {
  sky.setMode(mode);
  for (const b of document.querySelectorAll("[data-mode]")) b.setAttribute("aria-pressed", b.dataset.mode === mode);
  $("map-stage").dataset.pick = mode !== "pan";
  if (mode !== "pan") notice(`${mode === "box" ? "Drag a box" : "Draw a lasso"} on the map to select plotted records. Esc cancels. Keyboard: use "Select visible" or Region & run → select within radius.`);
  else notice("");
}

// ---------------------------------------------------------------- dialogs
let dialogTask = 0;
function openDialog(title, content, kind = "") {
  const task = ++dialogTask;
  $("dialog").dataset.kind = kind;
  $("dialog-title").textContent = title;
  $("dialog-content").innerHTML = content;
  if (!$("dialog").open) { state.returnFocus = document.activeElement; $("dialog").showModal(); }
  return task;
}
const dialogContent = (task, html) => { if (task === dialogTask && $("dialog").open) $("dialog-content").innerHTML = html; };

function notice(text, tone = "") {
  $("notice").textContent = text;
  $("notice").dataset.tone = tone;
  $("notice").hidden = !text;
}

function renderJobBadge() {
  const n = jobs.active().length;
  $("job-count").hidden = !n;
  $("job-count").textContent = n;
}

function renderJobsDialog() {
  const tracked = jobs.list();
  dialogContent(dialogTask, `<p class="compact">Jobs are durable on the server; this list reconnects after a reload and never resubmits. Polling every 3 s while preparing, paused while this page is hidden.</p>
    ${state.health && !state.health.worker?.running ? `<p class="notice compact">No worker is running for this server: queued jobs will not progress until one starts (python3 -m heimdall data worker).</p>` : ""}
    ${tracked.map(jobView).join("") || empty("No jobs tracked in this browser.")}
    ${details("All jobs recorded on the server", `<div id="server-jobs">${empty("Loading…")}</div>`)}`);
  client.get("/jobs", { params: { limit: 50 } }).then((d) => {
    const el = $("server-jobs");
    if (el) el.innerHTML = table(["Job", "Kind", "State", "Updated"], list(d.jobs).slice().reverse().map((j) => [`<button type="button" class="text-button" data-action="track-job" data-id="${h(j.job_id)}">${h(j.job_id)}</button>`, h(label(j.kind)), h(label(j.state)), h(j.updated_at || "")]));
  }).catch((e) => { const el = $("server-jobs"); if (el) el.innerHTML = empty(describe(e).message, true); });
}

function regionDialog(category = "field") {
  const v = sky.view;
  const f = state.record?.kind === "field" ? state.record.field : fieldCenter() || { ra: v.ra, dec: v.dec, radius: .05 };
  const caps = list(state.capabilities?.analyses);
  const fieldCap = caps.find((a) => a.id === "field_run");
  const workerWarn = (state.health?.offline ? `<p class="notice compact">Offline mode: nothing is fetched. Coverage reads only the existing index; a new run is queued and waits for network access.</p>` : "")
    + (state.health && !state.health.worker?.running ? `<p class="notice compact">No worker is running: a queued run will wait until one starts. Saved results stay available.</p>` : "");
  // Offer only what the backend declares (GET /capabilities); an absent capability is shown disabled with the reason.
  const has = (id) => !state.capabilities || caps.some((a) => a.id === id);
  const opt = (value, text, capId) => `<label class="inline"><input type="radio" name="intent" value="${value}" ${category === value && has(capId) ? "checked" : ""} ${has(capId) ? "" : "disabled"}> ${text}${has(capId) ? "" : " <small>(not offered by this server)</small>"}</label>`;
  openDialog("Region & run", `<form id="region-form" novalidate>
    <p class="compact">ICRS decimal degrees, or paste "h:m:s d:m:s" into the position box.</p>
    <label>Position <input name="pos" value="${number(f.ra, 5)} ${number(f.dec, 5)}" aria-describedby="pos-error" required></label><p id="pos-error" class="field-error" role="alert"></p>
    <label>Radius (°) <input name="radius" type="number" min="0.005" max="0.5" step="any" value="${number(Math.min(.5, Math.max(.005, f.radius || .05)), 4)}" aria-describedby="radius-error" required></label><p id="radius-error" class="field-error" role="alert"></p>
    <fieldset><legend>Action</legend>
      ${opt("coverage", "Browse coverage (metadata only; never analysis)", "coverage")}
      <label class="inline"><input type="radio" name="intent" value="select" ${category === "select" ? "checked" : ""}> Select plotted records within the radius</label>
      ${opt("field", "Run a blind field investigation", "field_run")}
      ${opt("MOVING_OBJECT", "Run a motion test at this position", "target_investigation")}
      ${opt("VARIABLE_STAR", "Run a variability test at this position", "target_investigation")}
    </fieldset>
    <p class="compact">Runs are explicit and durable: an identical saved result returns at once (200); otherwise a job is queued (202) and may download cutouts. ${fieldCap?.limits ? `Field limits: ${h(JSON.stringify(fieldCap.limits))}.` : "Field radius ≤ 0.5°, up to 32 observations."} Coordinate tests use the engine's defaults (up to 64 observations).</p>
    ${workerWarn}
    <div class="form-actions"><button class="primary" type="submit">Continue</button></div></form>`, "region");
}

async function submit(path, body, labelText) {
  const task = openDialog("Request status", empty("Submitting…"), "submit");
  try {
    const data = await client.post(path, body);
    const run = data.run_id || data.run?.run_id, inv = data.investigation_id, job = data.job?.job_id || data.job_id;
    if (run && !job) { $("dialog").close(); client.get("/runs").then((r) => { state.runs = list(r.runs); renderLibrary(); }).catch(() => {}); return openRecord("run", run); }
    if (inv) { $("dialog").close(); notice(`Identical stored result reused: ${inv}.`); return openRecord("version", inv); }
    if (job) {
      jobs.track(job, { label: labelText });
      dialogContent(task, `<p>Queued as a durable job. You can keep working; progress appears under Jobs and survives a reload.</p>${jobView(jobs.jobs.get(job))}`);
      return;
    }
    dialogContent(task, para(data.message || label(data.readiness)));
  } catch (e) {
    const d = describe(e);
    dialogContent(task, `${empty(`${d.title}: ${d.message}`, true)}${para(d.action)}`);
  }
}

async function showSystem() {
  const task = openDialog("Data and method status", empty("Loading service status…"), "system");
  const [health, service, caps, inv] = (await Promise.allSettled([client.get("/health"), client.get("/status"), client.get("/capabilities"), client.get("/inventory/domains")])).map((r) => r.value);
  const per = caps?.data?.per_detector || {};
  dialogContent(task, `<h3>Service</h3>${facts([["Software", health?.software_version], ["Mode", health ? health.offline ? "Offline (cached only)" : "Online" : "Unavailable"],
    ["Worker", health ? health.worker?.running ? `Running (heartbeat ${number(health.worker.heartbeat_age_s, 1)} s ago)` : "Not running: jobs will not progress" : "Unavailable"],
    ["All-sky completeness", service ? service.freshness?.all_sky_complete === true ? "Complete" : "Not established" : "Unavailable"]])}
    ${caps ? `<h3>Detectors</h3>${table(["Detector", "Indexed observations (metadata)", "Genuine products stored", "Scientific validation"], Object.entries(per).map(([d, v]) => [h(d), h(v.indexed_observations), h(v.real_products_stored), h(v.scientific_validation)]))}
      ${para(caps.data?.per_detector_note)}
      <h3>Analyses</h3>${table(["Analysis", "Entry", "Validation / methods"], list(caps.analyses).map((a) => [h(a.id), h(a.entry), h(a.validation || (a.methods ? Object.values(a.methods).join("; ") : ""))]))}
      ${list(caps.not_available).length ? `<h3>Not available</h3>${table(["Analysis", "Reason"], caps.not_available.map((n) => [h(n.id || n.analysis || n.name), h(n.reason || "")]))}` : ""}` : empty("Capabilities unavailable.", true)}
    ${inv ? `<h3>Inventory domains</h3>${list(inv.domains).map(para).join("") || para("No all-sky partitions planned.")}` : ""}
    ${service ? details("Service metrics and freshness", json(service)) : ""}`);
}

const TOUR = [
  { title: "1 · Explore", text: "The full-width sky is the genuine SPHEREx DR1 display mosaic in a Galactic Hammer projection. Drag, scroll or use the keyboard to move; the compass shows true north and east once zoomed.", run: () => { openRecord(null); showAllSky(); } },
  { title: "2 · Discover: a known moving object", text: "Epsilon Indi B (NASA-02). Its saved investigation loads from verified local data; nothing is re-run. Gold marks are measured centroids; the dashed teal path links them in time order (not an orbit).", run: () => openRecord("target", "NASA-02") },
  { title: "3 · Wavelength and time", text: "Step through the 102 wavelength planes, or press Separate to see the six simultaneous detector lanes. The ribbon below the map plays only the real epochs, in elapsed time or equal steps.", run: () => { $("band-strip").scrollIntoView({ block: "nearest" }); } },
  { title: "4 · Investigate", text: "The quick answer is the backend's gated headline claim. Evidence and Deeper dive show the measurements, competing hypotheses side by side, and what is not established.", run: () => { openPanel("investigations"); setTab("answer"); } },
  { title: "5 · Understand: a limited result", text: "Barnard's Star (NASA-01) is limited by data: too few usable epochs. That is a complete scientific outcome, shown with its evidence, not a failure or a non-detection.", run: () => openRecord("target", "NASA-01") },
  { title: "6 · Compare images", text: "Compare images aligns two exact cached products on one north-up grid. Difference mode is offered only when the server marks the pair comparable (same detector, wavelength within tolerance).", run: () => { openRecord("target", "NASA-02").then(() => openPanel("twin")); } },
];

function guide(step = 0) {
  const s = TOUR[step];
  try { localStorage.setItem("heimdall.tour.seen", "1"); } catch { /* optional */ }
  openDialog(`Guided tour · ${s.title}`, `<p>${h(s.text)}</p><p class="compact">Each step opens real saved records; results are shown with their actual limits. You can leave at any time.</p>
    <div class="form-actions"><button type="button" class="quiet" data-tour="${step - 1}" ${step === 0 ? "disabled" : ""}>Back</button>
    <button type="button" class="primary" data-tour-go="${step}">Show me</button>
    <button type="button" class="quiet" data-tour="${step + 1}" ${step === TOUR.length - 1 ? "disabled" : ""}>Next</button>
    <button type="button" class="text-button" data-tour="end">Skip tour</button></div>`, "tour");
}

// ---------------------------------------------------------------- export and sharing
function download(content, name, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 3000);
}

async function exportReport(format) {
  const id = state.doc?.investigation_id;
  if (!id) return;
  try {
    const rep = await client.get(`/investigations/${id}/report`, { params: { format } });
    const text = format === "md" ? rep.markdown : JSON.stringify(rep, null, 2);
    // Verify the export describes exactly the viewed immutable version before saving it.
    if (format === "json" ? rep.investigation_id !== id : !String(text).includes(id)) throw new Error("the report does not match the viewed version");
    download(text, `${id.replaceAll(":", "-")}.${format}`, format === "md" ? "text/markdown" : "application/json");
    announce(`Report for ${id} downloaded.`);
  } catch (e) { notice(`Report export failed: ${e.message}`, "error"); }
}

async function copyLink() {
  syncUrl(false);
  const url = location.href;
  try {
    await navigator.clipboard.writeText(url);
    notice("Link copied. It restores this record, camera, plane, layers, filters, selection and epoch on a server holding the same saved data. It never contains credentials.");
  } catch {
    openDialog("Copy link", `<label for="share-link">Link to this view</label><input id="share-link" readonly value="${h(url)}"><p class="compact">Reopens on a server with the same saved data. No credentials are included.</p>`, "share");
    $("share-link").select();
  }
}

// ---------------------------------------------------------------- search
function omniResults(text) {
  const t = text.trim().toLowerCase();
  if (!t) return [];
  const out = [];
  const id = identify(text);
  if (id && id.kind !== "job") out.push({ kind: id.kind, id: id.id, label: `Open ${label(id.kind).toLowerCase()} ${id.id}` });
  if (id?.kind === "job") out.push({ kind: "job", id: id.id, label: `Track job ${id.id}` });
  const pos = parseCoordinates(text);
  if (!pos.error) out.push({ kind: "position", id: `${pos.ra},${pos.dec}`, label: `Go to RA ${pos.ra.toFixed(5)}, Dec ${pos.dec.toFixed(5)}` });
  for (const tg of state.targets) if (`${tg.name} ${tg.target_id}`.toLowerCase().includes(t)) out.push({ kind: "target", id: tg.target_id, label: `${tg.name} (${tg.target_id})` });
  for (const r of state.runs) if (r.run_id.toLowerCase().includes(t)) out.push({ kind: "run", id: r.run_id, label: `Saved field ${r.run_id}` });
  return out.slice(0, 8);
}

function renderOmni() {
  const res = omniResults($("omni-input").value);
  state.omni = res;
  const ul = $("omni-list");
  ul.innerHTML = res.map((r, i) => `<li role="option" id="omni-${i}" data-i="${i}" aria-selected="${i === state.omniIndex}">${h(r.label)}</li>`).join("");
  ul.hidden = !res.length;
  $("omni-input").setAttribute("aria-expanded", res.length > 0);
  $("omni-input").setAttribute("aria-activedescendant", state.omniIndex >= 0 ? `omni-${state.omniIndex}` : "");
}

function runOmni(r) {
  $("omni-list").hidden = true;
  $("omni-error").textContent = "";
  if (!r) {
    const text = $("omni-input").value;
    const pos = parseCoordinates(text);
    $("omni-error").textContent = text.trim() ? `No match. ${pos.error} General object names are not resolved (no name-resolver service is configured); use a registered target, an identifier or coordinates.` : "";
    return;
  }
  if (r.kind === "position") {
    const [ra, dec] = r.id.split(",").map(Number);
    return openRecord("field", null, { field: { ra, dec, radius: .05 } });
  }
  if (r.kind === "job") { jobs.track(r.id, { label: "Opened from search" }); return showJobsDialog(); }
  if (["observation", "product"].includes(r.kind)) {
    return openTrace("lookup", r.id, { push: false, request: () => client.get("/evidence/lookup", { params: r.kind === "product" ? { sha256: r.id } : { obs_id: r.id } }) });
  }
  if (r.kind === "manifest") return openTrace("manifest", r.id, { request: () => client.get(`/manifests/${r.id}`) });
  openRecord(r.kind, r.id);
}

function showJobsDialog() {
  openDialog("Jobs", "", "jobs");
  renderJobsDialog();
}

// ---------------------------------------------------------------- actions
function runAction(el) {
  const { action: kind, id } = el.dataset;
  const close = () => { if ($("dialog").open) $("dialog").close(); };
  switch (kind) {
    case "target": close(); return openRecord("target", id);
    case "version": close(); return openRecord("version", id);
    case "run": close(); return openRecord("run", id);
    case "tab": return setTab(id);
    case "chart": state.chart = id; openPanel("charts"); return renderVisuals();
    case "axis": state.axis = Number(id); return renderVisuals();
    case "fit-evidence": focusField(true); return $("map-heading").scrollIntoView({ block: "nearest" });
    case "observation": case "candidate": case "source": case "detection": close(); return select(kind, id);
    case "lookup": return openTrace(kind, id, { request: () => client.get("/evidence/lookup", { params: id.length === 64 ? { sha256: id } : { obs_id: id } }) });
    case "manifest": return openTrace(kind, id, { request: () => client.get(`/manifests/${id}`) });
    case "preview": state.twinPair = null; return showPreview(id);
    case "followup": return openTrace(kind, id, { request: () => client.get(`/targets/${id}/followup`) });
    case "followup-run": return submit(`/followup/${id}/run`, {}, `Follow-up ${id}`);
    case "compare": {
      const b = $("compare-version")?.value;
      if (!b) return;
      state.compare = { a: id, b };
      return openTrace("compare", `${id}..${b}`, { push: true, request: () => client.get("/compare", { params: { a: id, b } }) });
    }
    case "submit-target": return submit("/investigations", { target_id: id }, `New run of ${id}`);
    case "job": case "track-job": jobs.track(id); return showJobsDialog();
    case "cancel-job": if (confirm("Cancel this job? Shared prerequisite jobs keep running for other requests.")) jobs.cancel(id).catch((e) => notice(describe(e).message, "error")); return;
    case "forget-job": jobs.forget(id); return renderJobsDialog();
    case "export": return exportReport(id);
    case "export-run": if (state.doc?.run_id) download(JSON.stringify(state.doc, null, 2), `${state.doc.run_id}.json`, "application/json"); return;
    case "clear-filters": state.filters = {}; $("mjd-error").textContent = $("wave-error").textContent = ""; syncUrl(true); return render();
    case "clear-selection": state.selection = null; state.region = null; state.trace = []; state.traceIndex = -1; syncUrl(true); return render();
    case "clear-region": state.region = null; return render();
    case "select-visible": return selectVisible();
    case "region-dialog": return regionDialog("coverage");
    case "run-dialog": return regionDialog(id);
    case "guide": return guide(0);
    default: return openTrace(kind, id, { push: kind !== "reference" });
  }
}

async function showPreview(sha) {
  const task = openDialog("Cached FITS preview", empty("Checking the stored product…"), "preview");
  try {
    const p = await client.get("/evidence/preview", { params: { sha256: sha } });
    const bytes = atob(p.pixels);
    if (p.sha256 !== sha || bytes.length !== p.width * p.height) throw new Error("Invalid or mismatched local preview.");
    dialogContent(task, `${state.doc?.data_origin === "SYNTHETIC" ? `<p class="synthetic-flag">SYNTHETIC · NOT REAL DATA</p>` : ""}
      <p class="compact">Checksum-matched FITS pixels in native image coordinates (not north-up), with this image's own stretch. For aligned comparison use Compare images.</p>
      <figure class="fits-preview"><canvas id="fits-canvas" width="${p.width}" height="${p.height}" role="img" aria-label="Grayscale preview of the cached FITS science image"></canvas></figure>
      ${facts([["HDU", p.hdu], ["Native image", `${p.source_width} × ${p.source_height} px`], ["Stretch", `${p.stretch_percentiles.join(" to ")} percentile`]])}<p class="mono compact">${h(sha)}</p>`);
    const ctx = $("fits-canvas").getContext("2d"), img = ctx.createImageData(p.width, p.height);
    for (let i = 0; i < bytes.length; i++) { const g = bytes.charCodeAt(i); img.data.set([g, g, g, 255], i * 4); }
    ctx.putImageData(img, 0, 0);
  } catch (e) { dialogContent(task, empty(e.message, true)); }
}

function bind() {
  document.addEventListener("click", (e) => {
    const ov = e.target.closest("[data-overlap]");
    if (ov) {
      if (ov.dataset.overlap === "close") { $("overlap").hidden = true; return; }
      const [kind, ...rest] = ov.dataset.overlap.split("|");
      return choose({ kind, id: rest.join("|") });
    }
    const tour = e.target.closest("[data-tour], [data-tour-go]");
    if (tour) {
      if (tour.dataset.tourGo) { const s = TOUR[Number(tour.dataset.tourGo)]; $("dialog").close(); s.run(); return; }
      if (tour.dataset.tour === "end") return $("dialog").close();
      return guide(Number(tour.dataset.tour));
    }
    const mark = e.target.closest("[data-mark]");
    if (mark) return select("observation", mark.dataset.mark);
    const plane = e.target.closest(".plane");
    if (plane) return setPlane(Number(plane.dataset.plane));
    const epoch = e.target.closest("[data-epoch]");
    if (epoch) { stopPlayback(); return setEpoch(epochs().findIndex((x) => x.id === epoch.dataset.epoch), { push: true }); }
    const b = e.target.closest("button[data-action], g[data-action]");
    if (b) runAction(b);
  });
  $("chart").addEventListener("keydown", (e) => { if (e.target.matches("g[data-action]") && ["Enter", " "].includes(e.key)) { e.preventDefault(); runAction(e.target); } });
  $("inspector-tabs").addEventListener("keydown", (e) => {
    const tabs = ["answer", "evidence", "deeper", "history"];
    const i = tabs.indexOf(state.tab);
    const next = { ArrowRight: tabs[(i + 1) % 4], ArrowLeft: tabs[(i + 3) % 4], Home: tabs[0], End: tabs[3] }[e.key];
    if (next) { e.preventDefault(); setTab(next); $(`tab-${next}`).focus(); }
  });
  $("inspector-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) setTab(b.dataset.tab); });
  $("library-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-library]");
    if (!b) return;
    state.library = b.dataset.library;
    renderLibrary();
    if (state.library === "runs") client.get("/runs").then((r) => { state.runs = list(r.runs); renderLibrary(); }).catch((err) => { $("library").innerHTML = empty(describe(err).message, true); });
  });
  $("trace-back").onclick = () => { const a = state.trace[state.traceIndex]; if (state.traceIndex >= 0) state.traceIndex--; if (state.traceIndex < 0 && a) state.tab = a.returnTab; renderInspector(); };
  $("trace-forward").onclick = () => { if (state.traceIndex < state.trace.length - 1) state.traceIndex++; renderInspector(); };
  $("layers").addEventListener("change", (e) => {
    const l = e.target.dataset.layer;
    if (!l) return;
    state.layers[l] = e.target.checked;
    if (l === "survey") setPlane(state.plane);
    if (l === "inventory" && e.target.checked) loadInventory();
    syncUrl(true);
    drawMap();
  });
  for (const id of ["detector", "release", "quality", "mjd-start", "mjd-end", "wave-start", "wave-end"]) $(id).addEventListener("change", updateFilter);
  $("clear-filters").onclick = () => runAction({ dataset: { action: "clear-filters" } });
  $("chart-kind").onchange = () => { state.chart = $("chart-kind").value; renderVisuals(); };
  $("chart-table").onclick = () => { state.tableView = !state.tableView; renderVisuals(); };
  $("zoom-in").onclick = () => sky.zoom(.55);
  $("zoom-out").onclick = () => sky.zoom(1.8);
  $("focus-field").onclick = () => focusField();
  $("all-sky").onclick = showAllSky;
  $("plane-prev").onclick = () => setPlane(state.plane - 1);
  $("plane-next").onclick = () => setPlane(state.plane + 1);
  $("separate").onclick = toggleSeparate;
  $("band-strip").addEventListener("keydown", (e) => {
    const d = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: state.separated ? CHANNELS : 0, ArrowUp: state.separated ? -CHANNELS : 0 }[e.key];
    if (d) { e.preventDefault(); setPlane(state.plane + d); }
    else if (e.key === "Home") { e.preventDefault(); setPlane(1); }
    else if (e.key === "End") { e.preventDefault(); setPlane(102); }
  });
  $("image-brightness").oninput = () => setPlane(state.plane, { silent: true });
  for (const b of document.querySelectorAll("[data-mode]")) b.onclick = () => setPointerMode(b.dataset.mode);
  document.querySelector(".map-tools").addEventListener("click", (e) => { const b = e.target.closest("[data-panel]"); if (b) openPanel(b.dataset.panel); });
  $("skip-inspector").onclick = (e) => { e.preventDefault(); openPanel("investigations"); $("inspector").focus(); };
  $("play").onclick = play;
  $("epoch-prev").onclick = () => { stopPlayback(); setEpoch(Math.max(0, epochs().findIndex((x) => x.id === state.frame) - 1), { push: true }); };
  $("epoch-next").onclick = () => { stopPlayback(); setEpoch(epochs().findIndex((x) => x.id === state.frame) + 1, { push: true }); };
  $("show-all").onclick = () => { stopPlayback(); state.frame = null; syncUrl(true); renderVisuals(); };
  $("spacing").onchange = () => { state.spacing = $("spacing").value; renderRibbon(); };
  $("speed").onchange = () => { state.speed = Number($("speed").value); };
  $("ribbon-track").addEventListener("keydown", (e) => {
    const i = epochs().findIndex((x) => x.id === state.frame);
    const n = { ArrowRight: i + 1, ArrowLeft: Math.max(0, i - 1), Home: 0, End: epochs().length - 1 }[e.key];
    if (n !== undefined) { e.preventDefault(); stopPlayback(); setEpoch(n, { push: true }); }
    else if (e.key === " ") { e.preventDefault(); play(); }
  });
  $("open-field").onclick = () => regionDialog("coverage");
  $("show-jobs").onclick = showJobsDialog;
  $("show-system").onclick = showSystem;
  $("show-guide").onclick = () => guide(0);
  $("share").onclick = copyLink;
  $("close-dialog").onclick = () => $("dialog").close();
  $("dialog").addEventListener("close", () => {
    if ($("dialog").open) return; // reopened before the close event was delivered: keep the new content
    dialogTask++;
    state.returnFocus?.focus?.();
  });
  $("dialog").addEventListener("submit", (e) => {
    e.preventDefault();
    if (e.target.id === "api-auth") { client.token = new FormData(e.target).get("token"); $("dialog").close(); return boot(); }
    if (e.target.id !== "region-form") return;
    const fd = new FormData(e.target);
    const pos = parseCoordinates(fd.get("pos"));
    const radius = Number(fd.get("radius"));
    $("pos-error").textContent = pos.error || "";
    $("radius-error").textContent = finite(radius) && radius >= .005 && radius <= .5 ? "" : "Radius must be between 0.005° and 0.5°.";
    if (pos.error || $("radius-error").textContent) return; // values stay as entered
    const intent = fd.get("intent") || "coverage";
    const field = { ra: pos.ra, dec: pos.dec, radius };
    if (intent === "coverage") { $("dialog").close(); return openRecord("field", null, { field }); }
    if (intent === "select") {
      $("dialog").close();
      const cosd = Math.cos(pos.dec * Math.PI / 180);
      const data = canvasLayers();
      const items = ["detections", "sources", "candidates", "reference"].flatMap((k) => list(data[k]).filter((i) => finite(i.ra) && Math.hypot(deltaRA(i.ra, pos.ra) * cosd, i.dec - pos.dec) <= radius));
      return onRegion(items, null, `within ${radius}° of ${pos.ra.toFixed(4)}, ${pos.dec.toFixed(4)}`);
    }
    if (intent === "field") return submit("/runs", field, `Field run at ${pos.ra.toFixed(4)}, ${pos.dec.toFixed(4)} r=${radius}`);
    return submit("/investigations", { ra: pos.ra, dec: pos.dec, category: intent }, `${label(intent)} test at ${pos.ra.toFixed(4)}, ${pos.dec.toFixed(4)}`);
  });
  $("omnibox").addEventListener("submit", (e) => { e.preventDefault(); runOmni(state.omni?.[Math.max(0, state.omniIndex ?? 0)]); });
  $("omni-input").addEventListener("input", () => { state.omniIndex = -1; renderOmni(); $("omni-error").textContent = ""; });
  $("omni-input").addEventListener("keydown", (e) => {
    const n = state.omni?.length || 0;
    if (e.key === "ArrowDown" && n) { e.preventDefault(); state.omniIndex = ((state.omniIndex ?? -1) + 1) % n; renderOmni(); }
    else if (e.key === "ArrowUp" && n) { e.preventDefault(); state.omniIndex = ((state.omniIndex ?? 0) - 1 + n) % n; renderOmni(); }
    else if (e.key === "Escape") { $("omni-list").hidden = true; }
  });
  $("omni-list").addEventListener("mousedown", (e) => { const li = e.target.closest("[data-i]"); if (li) { e.preventDefault(); runOmni(state.omni[Number(li.dataset.i)]); } });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("overlap").hidden) { $("overlap").hidden = true; $("sky").focus(); }
    if (e.key === "Escape" && sky.mode !== "pan" && !$("dialog").open) setPointerMode("pan");
  });
  new ResizeObserver(() => renderChart($("chart"), $("chart-subtitle"), state, (r) => matches(r))).observe($("chart-panel"));
}

async function refreshHealth() {
  try {
    state.health = await client.get("/health");
    $("connection").dataset.state = "connected";
    $("connection").innerHTML = `<i></i>${state.health.offline ? "Offline mode" : "API connected"}`;
    $("software-version").textContent = state.health.software_version ?? "";
    const w = state.health.worker;
    const msg = state.health.offline ? "Offline mode: nothing touches the network. Saved records and cached imagery stay available; uncached imagery and new data wait." :
      !w?.running ? "No data worker is running for this server: saved results are fully usable, but new jobs will wait until a worker starts." : "";
    $("worker-banner").textContent = msg;
    $("worker-banner").hidden = !msg;
  } catch (e) {
    $("connection").dataset.state = "error";
    $("connection").innerHTML = `<i></i>${describe(e).title}`;
    throw e;
  }
}

async function boot() {
  try {
    await refreshHealth();
    const [targets, caps, runs] = await Promise.allSettled([client.get("/targets"), client.get("/capabilities"), client.get("/runs")]);
    state.targets = list(targets.value?.targets);
    state.capabilities = caps.value || null;
    state.runs = list(runs.value?.runs);
    if (targets.reason) throw targets.reason;
    await applyUrl(parse(INITIAL_SEARCH), { initial: true });
    state.booted = true;
    syncUrl(false);
    document.body.dataset.booted = "1";
    let seen = false;
    try { seen = localStorage.getItem("heimdall.tour.seen") === "1"; } catch { seen = true; }
    if (!seen && !location.search) notice("New here? Press Guide for a short tour through real saved results (skippable).");
    setInterval(() => { if (!document.hidden) refreshHealth().catch(() => {}); }, 30000);
  } catch (e) {
    const d = describe(e);
    if (d.kind === FAILURE.UNAUTHORIZED) {
      openDialog("Connect to HEIMDALL", `<form id="api-auth"><label for="api-token">API access token</label><input id="api-token" name="token" type="password" required autocomplete="off"><p class="compact">Kept only in this tab's memory; never written to links or storage.</p><div class="form-actions"><button class="primary" type="submit">Connect</button></div></form>`, "auth");
    }
    state.error = `${d.title}. ${d.action}`;
    document.body.dataset.booted = "error";
    render();
  }
}

bind();
onCamera(sky.view);
setPlane(2, { silent: true });
render();
wavelengths().then((v) => { state.wavelengths = v; setPlane(state.plane, { silent: true }); renderStrip(); }).catch(() => {});
boot();
