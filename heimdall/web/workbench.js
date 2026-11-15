import { SkyMap, deltaRA } from "./sky.js";
import { wavelengths } from "./hips.js";
import { renderChart } from "./charts.js";
import { action, apiPath, badge, claimLinks, details, empty, facts, finite, first, group, h, json,
  key, label, list, number, obsAction, para, stack, svg, traceView } from "./records.js";

const $ = (id) => document.getElementById(id);
const state = {
  targets: [], runs: [], library: "targets", tab: "overview", chart: "motion", axis: 0, trace: [], traceIndex: -1,
  filters: {}, layers: {}, inventory: [], allSky: false, token: "", plane: 2, wavelengths: [],
};
const layerOptions = [
  ["survey", "SPHEREx DR1 image", "coverage", true],
  ["grid", "Coordinate grid", "", false],
  ["reference", "Catalogue or target position", "track", true],
  ["coverage", "Observation footprints", "coverage", true],
  ["detections", "Centroids / sources of light", "measure", true],
  ["sources", "Associated sources", "", true],
  ["candidates", "Investigation candidates", "candidate", true],
  ["tracks", "Time-ordered linkages", "track", true],
  ["associations", "Source membership", "", false],
  ["catalogue", "Catalogue context", "", false],
  ["inventory", "Indexed sky partitions", "coverage", false],
];
for (const [id, , , enabled] of layerOptions) state.layers[id] = enabled;
const atlasCenter = [266.40499, -28.93617, 360];
const sky = new SkyMap($("sky"), updateCoordinates, pick, $("map-tooltip"), (text) => {
  const blank = state.plane === 51 || state.plane === 52;
  const status = blank && !text.startsWith("Loading") ? `${text} · dichroic plane intentionally blank` : text;
  if ($("image-status").textContent !== status) $("image-status").textContent = status;
}, pickPosition);
const workspace = document.querySelector(".workspace");

function openPanel(panel, reveal = false) {
  workspace.dataset.panel = panel;
  for (const button of document.querySelectorAll(".map-tools [data-panel]")) {
    button.setAttribute("aria-expanded", button.dataset.panel === panel);
  }
  if (reveal && panel !== "none") {
    const container = { layers: "workbench-controls", investigations: "inspector", charts: "chart-panel" }[panel];
    requestAnimationFrame(() => $(container)?.scrollIntoView({ block: "nearest" }));
  }
}

async function api(path, { params, body, signal } = {}) {
  const headers = { Accept: "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(apiPath(path, params), {
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(12000)]) : AbortSignal.timeout(12000), method: body === undefined ? "GET" : "POST",
    headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw Object.assign(new Error(payload.error?.message || `Request failed (${response.status})`), { status: response.status });
  return payload;
}

function notice(text, tone = "") {
  $("notice").textContent = text;
  $("notice").dataset.tone = tone;
  $("notice").hidden = !text;
}

function updateCoordinates({ ra, dec, span }) {
  const digits = span < .02 ? 5 : span < 1 ? 3 : 2;
  $("coordinates").textContent = `RA ${ra.toFixed(digits)}° · Dec ${dec >= 0 ? "+" : ""}${dec.toFixed(digits)}°`;
  $("field-size").textContent = span === 360 ? "Full sky · 360°" : `Zoom ${number(360 / span, 1)}×`;
  $("map-mode").textContent = span === 360 ? "Galactic · Hammer" : "Recentered sky projection";
  $("all-sky").setAttribute("aria-pressed", span === 360);
  $("map-stage").dataset.fullSky = span === 360;
  $("map-compass").hidden = true;
}

function library() {
  for (const b of $("library-tabs").querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.library === state.library);
  const targets = state.library === "targets";
  const records = targets ? state.targets : state.runs;
  $("library").setAttribute("aria-busy", "false");
  $("library").innerHTML = records.map((r) => {
    const id = targets ? r.target_id : r.run_id;
    const title = targets ? r.name : r.run_id;
    const status = targets ? `${id} · ${r.saved?.status?.test_status === "PASSED" ? "Test passed" : r.saved?.status?.test_status ? label(r.saved.status.test_status) : "No saved result"}`
      : `${label(r.data_origin)} · ${label(r.status?.execution)} · ${r.n_candidates ?? 0} candidates`;
    const current = id === (targets ? state.targetId : state.doc?.run_id);
    return `<button type="button" class="library-item" data-action="${targets ? "target" : "run"}" data-id="${h(id)}" aria-current="${current}"><strong>${h(title)}</strong><small>${h(status)}</small></button>`;
  }).join("") || empty(targets ? "No targets loaded." : "No saved field runs.");
}

async function loadRuns() {
  const response = await api("/runs");
  state.runs = list(response.runs);
  if (state.library === "runs") library();
}

function beginSelection(targetId) {
  state.loading?.abort();
  const signal = new AbortController();
  const previous = state.targetId;
  stopPlayback();
  Object.assign(state, { loading: signal, targetId, doc: null, claims: null, versions: null, attempt: null, patterns: null, map: null, coverage: null,
    error: "", mapError: "", selection: null, tab: "overview", trace: [], traceIndex: -1, frame: null, filters: {}, allSky: false, field: null });
  if (targetId !== previous) state.chart = "motion";
  library();
  render();
  return signal;
}

async function loadTarget(id, version, preserveView = false) {
  const camera = preserveView ? { ...sky.view } : null;
  state.library = "targets";
  const load = beginSelection(id);
  const options = { signal: load.signal };
  try {
    const [result, versions, attempt] = await Promise.allSettled([
      api(version ? `/investigations/${key(version)}` : `/targets/${key(id)}`, options),
      api(`/targets/${key(id)}/versions`, options),
      api(`/targets/${key(id)}/attempt`, options),
    ]);
    if (load !== state.loading) return;
    state.versions = versions.value;
    state.attempt = attempt.value;
    if (!result.value && result.reason?.status !== 404) throw result.reason;
    state.doc = result.value || (!version ? attempt.value : null);
    if (!state.doc) {
      state.error = version ? "This saved version could not be loaded." : "This target has no saved investigation or attempt.";
      focusField();
      return;
    }
    const versionID = state.doc.investigation_id;
    const calls = await Promise.allSettled([
      api(`/investigations/${key(versionID)}/claims`, options),
      api(`/investigations/${key(versionID)}/patterns`, options),
    ]);
    if (load !== state.loading) return;
    [state.claims, state.patterns] = calls.map((r) => r.value || { error: r.reason?.message || "Unavailable" });
    state.chart = list(state.doc.analyses?.motion?.track).length ? "motion" : list(state.doc.analyses?.variability?.light_curve).some((p) => p.usable) ? "flux" : "observations";
    if (camera) sky.setView(camera.ra, camera.dec, camera.span); else focusField();
  } catch (e) {
    if (load === state.loading) state.error = e.message;
  } finally {
    if (load === state.loading) {
      state.loading = null;
      render();
    }
  }
}

async function loadRun(id) {
  state.library = "runs";
  const load = beginSelection();
  try {
    const [doc, map] = await Promise.allSettled([api(`/runs/${key(id)}`, { signal: load.signal }), api(`/runs/${key(id)}/skymap`, { signal: load.signal })]);
    if (load !== state.loading) return;
    if (!doc.value) throw doc.reason;
    state.doc = doc.value;
    state.map = map.value || null;
    state.mapError = map.reason?.message;
    state.chart = "observations";
    focusField();
  } catch (e) {
    if (load === state.loading) state.error = e.message;
  } finally {
    if (load === state.loading) {
      state.loading = null;
      render();
    }
  }
}

function fieldCenter() {
  const doc = state.doc || {};
  if (state.field) return state.field;
  if (doc.request && finite(doc.request.ra_deg)) return { ra: doc.request.ra_deg, dec: doc.request.dec_deg, radius: doc.request.radius_deg || .05 };
  const center = doc.reference?.position_at_discovery_epoch || doc.reference;
  const measurements = list(doc.measurements).map((m) => m.centroid).filter((c) => finite(c?.ra_deg) && finite(c?.dec_deg));
  if (measurements.length) {
    const firstRA = measurements[0].ra_deg;
    const ra = firstRA + measurements.reduce((sum, c) => sum + deltaRA(c.ra_deg, firstRA), 0) / measurements.length;
    const dec = measurements.reduce((sum, c) => sum + c.dec_deg, 0) / measurements.length;
    const spread = Math.max(...measurements.map((c) => Math.max(Math.abs(deltaRA(c.ra_deg, ra)), Math.abs(c.dec_deg - dec) * 1.8)));
    return { ra, dec, radius: Math.max(.0026, spread * 1.45) };
  }
  if (finite(center?.ra_deg) && finite(center.dec_deg)) return { ra: center.ra_deg, dec: center.dec_deg, radius: .016 };
  const spec = doc.target || state.targets.find((t) => t.target_id === state.targetId)?.seed;
  const ra = spec?.seed_ra_deg ?? spec?.ra_deg, dec = spec?.seed_dec_deg ?? spec?.dec_deg;
  return finite(ra) && finite(dec) ? { ra, dec, radius: .07 } : null;
}

function focusField(fitEvidence = false) {
  const c = fieldCenter();
  state.allSky = false;
  if (c) sky.setView(c.ra, c.dec, Math.max(fitEvidence ? .001 : .12, c.radius * 2.5));
  else {
    state.allSky = true;
    sky.setView(...atlasCenter);
  }
  drawMap();
}

function metaFor(id) {
  return state.metadata?.get(id) || {};
}

function frameRows() {
  const rows = state.map?.layers?.time?.frames || state.doc?.playback?.frames;
  const selected = list(rows).length ? list(rows) : list(state.doc?.measurements).map((m) => ({ obs_id: m.obs_id, mjd: m.mjd }));
  return selected.filter((f) => finite(f.mjd) && matches(f, false)).sort((a, b) => a.mjd - b.mjd);
}

function matches(record, applyFrame = true) {
  const f = state.filters;
  const m = metaFor(record.obs_id);
  const detector = record.detector ?? record.meta?.detector ?? m.detector;
  const release = record.release || record.meta?.release || m.release;
  const mjd = record.mjd ?? record.meta?.mjd_avg ?? record.t_min_mjd ?? m.mjd;
  const wave = record.wavelength_um ?? m.wave;
  const waveMin = wave ?? record.wavelength_min_um ?? m.waveMin;
  const waveMax = wave ?? record.wavelength_max_um ?? m.waveMax;
  const usable = record.usable ?? record.included ?? m.usable;
  if (f.detector && String(detector) !== f.detector) return false;
  if (f.release && release !== f.release) return false;
  if (f.quality && usable !== (f.quality === "usable")) return false;
  if (f.start !== undefined && (!finite(mjd) || mjd < f.start)) return false;
  if (f.end !== undefined && (!finite(mjd) || mjd > f.end)) return false;
  if (f.waveStart !== undefined && (!finite(waveMax) || waveMax < f.waveStart)) return false;
  if (f.waveEnd !== undefined && (!finite(waveMin) || waveMin > f.waveEnd)) return false;
  return !applyFrame || !state.frame || record.obs_id === state.frame;
}

function refreshMetadata() {
  const doc = state.doc || {};
  const index = new Map();
  const add = (id, fields) => {
    if (!id) return;
    index.set(id, { ...index.get(id), ...Object.fromEntries(Object.entries(fields).filter(([, v]) => v != null)) });
  };
  for (const o of list(state.coverage?.observations)) add(o.obs_id, { detector: o.detector, release: o.release, mjd: o.t_min_mjd,
    waveMin: o.wavelength_min_um, waveMax: o.wavelength_max_um });
  for (const p of list(doc.products || doc.input_manifest?.products)) add(p.obs_id, { release: p.release });
  for (const o of list(doc.observations)) add(o.obs_id, { detector: o.meta?.detector, release: o.meta?.release, mjd: o.meta?.mjd_avg, usable: o.included });
  for (const m of list(doc.measurements)) add(m.obs_id, { detector: m.detector, mjd: m.mjd, wave: m.wavelength_um, usable: m.status === "MEASURED" });
  for (const f of list(state.map?.layers?.coverage)) add(f.obs_id, { wave: f.wavelength_um });
  for (const m of list(doc.analyses?.variability?.light_curve)) add(m.obs_id, { usable: m.usable });
  state.metadata = index;
  const counts = [...index.values()];
  for (const id of ["detector", "release"]) {
    const base = id === "detector" ? "All detectors" : "All releases";
    const values = [...new Set(counts.map((v) => v[id]).filter((v) => v != null))].sort();
    $(id).innerHTML = `<option value="">${base}</option>` + values.map((v) => `<option value="${h(v)}">${h(id === "detector" ? `Detector ${v}` : v)}</option>`).join("");
    $(id).value = state.filters[id] || "";
  }
  $("quality").value = state.filters.quality || "";
  $("mjd-start").value = state.filters.start ?? "";
  $("mjd-end").value = state.filters.end ?? "";
  $("wave-start").value = state.filters.waveStart ?? "";
  $("wave-end").value = state.filters.waveEnd ?? "";
}

function polygon(p) {
  if (typeof p === "string" && /^POLYGON\s/i.test(p)) {
    const values = p.trim().replace(/^POLYGON\s+(?:[A-Z]+\s+)?/i, "").split(/[\s,]+/).map(Number);
    if (values.length >= 6 && values.length % 2 === 0 && values.every(finite)) {
      return values.flatMap((v, i) => i % 2 ? [] : [[v, values[i + 1]]]);
    }
  }
  if (list(p?.ra).length === list(p?.dec).length && list(p?.ra).length > 2) {
    const points = p.ra.map((ra, i) => [ra, p.dec[i]]);
    return points.every(([x, y]) => finite(x) && finite(y)) ? points : [];
  }
  return [];
}

function canvasLayers() {
  const doc = state.doc || {};
  const d = state.map?.layers || {};
  const c = fieldCenter();
  const p = doc.reference?.position_at_discovery_epoch || doc.reference;
  const ref = finite(p?.ra_deg) && finite(p.dec_deg) ? [{ ra: p.ra_deg, dec: p.dec_deg, label: "Catalogue position at recorded epoch", id: "reference", kind: "reference" }] : [];
  if (c && !ref.length && state.targetId) ref.push({ ...c, id: "target-seed", kind: "reference", label: "Target seed position" });
  if (state.allSky) {
    for (const t of state.targets) {
      if (finite(t.seed?.ra_deg) && finite(t.seed?.dec_deg)) ref.push({ ra: t.seed.ra_deg, dec: t.seed.dec_deg, id: t.target_id, kind: "target", label: `${t.name} · seed` });
    }
  }
  if (state.picked) ref.push({ ...state.picked, id: "picked", label: "Selected sky position" });
  const measurements = list(doc.measurements).filter(matchesAtFrame).filter((m) => finite(m.centroid?.ra_deg) && finite(m.centroid?.dec_deg));
  const allowedDet = new Set(list(d.detections).filter(matchesAtFrame).map((x) => x.det_id));
  const sourceIds = new Set(list(d.detections).filter((x) => allowedDet.has(x.det_id)).map((x) => x.source_id));
  const obsFilter = Object.values(state.filters).some((x) => x !== undefined && x !== "") || Boolean(state.frame);
  const fromCoverage = list(state.coverage?.observations);
  const footprints = [...list(d.coverage), ...fromCoverage].filter(matchesAtFrame).map((o) => ({
    coords: polygon(o.polygon || o.footprint), closed: true, dashed: o.included === false, selected: o.obs_id === state.selection?.id,
  }));
  const centroidByObs = new Map(measurements.map((m) => [m.obs_id, m.centroid]));
  const targetTrack = list(doc.analyses?.motion?.track).filter(matchesAtFrame).sort((a, b) => a.mjd - b.mjd)
    .map((p) => centroidByObs.get(p.obs_id)).filter(Boolean).map((p) => [p.ra_deg, p.dec_deg]);
  const tracks = list(d.tracks).flatMap((t) => {
    const points = list(t.points);
    // Break paths across filtered-out observations to preserve the recorded adjacency.
    return points.slice(1).flatMap((p, i) => allowedDet.has(p.det_id) && allowedDet.has(points[i].det_id)
      ? [{ coords: [[points[i].ra, points[i].dec], [p.ra, p.dec]], dashed: Boolean(t.ambiguous) }] : []);
  });
  if (targetTrack.length > 1 && !obsFilter) tracks.push({ coords: targetTrack, dashed: true });
  const catalog = state.map?.external_layers?.star_catalogue;
  return {
    reference: ref,
    coverage: footprints.filter((o) => o.coords.length),
    detections: [
      ...list(d.detections).filter((x) => allowedDet.has(x.det_id)).map((x) => ({ ...x, id: x.det_id, kind: "detection", ellipse: x.ellipse_1sigma, label: `Pipeline detection · ${x.obs_id}` })),
      ...measurements.map((m) => ({ ra: m.centroid.ra_deg, dec: m.centroid.dec_deg, id: m.obs_id, kind: "observation", label: `Measured centroid · ${m.obs_id}` })),
    ],
    sources: list(d.sources).filter((x) => !obsFilter || sourceIds.has(x.source_id)).map((x) => ({ ...x, id: x.source_id, kind: "source", ellipse: x.ellipse_1sigma, label: `Source · ${x.source_id}` })),
    candidates: obsFilter ? [] : list(d.candidates).map((x) => ({ ...x, id: x.candidate_id, kind: "candidate", ellipse: x.ellipse_1sigma, label: `Candidate · ${label(x.kind)}` })),
    tracks,
    associations: list(d.associations).flatMap((a) => list(a.segments).filter((s) => allowedDet.has(s.det_id)).map((s) => ({ coords: [s.from, s.to], dashed: true }))),
    catalogue: list(catalog?.items).map((s) => ({ ra: s.ra ?? s.ra_deg, dec: s.dec ?? s.dec_deg, label: `${catalog.source || "Catalogue"} · ${s.source_id || "source"}` })),
    inventory: state.allSky ? state.inventory : [],
  };
}

function matchesAtFrame(record) { return matches(record, true); }

function drawMap() {
  const data = canvasLayers();
  sky.setData(data, state.layers, state.selection?.id);
  $("all-sky").setAttribute("aria-pressed", sky.view.span === 360);
  $("focus-field").disabled = !fieldCenter();
  $("layers").innerHTML = layerOptions.map(([id, title, tone]) => {
    const total = list(data[id]).length;
    return `<label class="layer" data-tone="${tone}"><i aria-hidden="true"></i><span>${title}</span><input data-layer="${id}" type="checkbox" aria-label="${title}" ${state.layers[id] ? "checked" : ""} ${!total && !["survey", "grid", "inventory"].includes(id) ? "disabled" : ""}></label>`;
  }).join("");
  const from = state.doc?.data_origin;
  const banner = from === "SYNTHETIC" ? "SYNTHETIC · not real observations"
    : state.allSky && state.layers.inventory ? (state.inventoryLoading ? "Loading recorded partition states…" : state.inventory.length ? "Partition states · shaded tiles certified searched" : "No local inventory geometry available")
    : state.allSky ? "" :
      state.mapError ? `Map layer unavailable: ${state.mapError}`
    : state.coverage?.readiness === "PREPARING" ? "Archive coverage is being prepared."
    : !data.detections.length && !data.coverage.length ? "No plotted observations in this selection."
    : state.doc?.run_id ? "Saved field · pipeline detections are unvalidated" : "Saved investigation · measured centroids";
  $("map-banner").textContent = banner;
  $("map-banner").hidden = !banner;
  $("map-banner").dataset.tone = from === "SYNTHETIC" || Boolean(state.mapError) ? "warning" : "";
}

async function showAllSky() {
  state.allSky = true;
  sky.setView(...atlasCenter);
  drawMap();
  if (state.layers.inventory) await loadInventory();
}

async function loadInventory() {
  if (state.inventory.length || state.inventoryLoading) return;
  state.inventoryLoading = true;
  drawMap();
  try {
    const data = await api("/inventory/domains");
    const domains = await Promise.allSettled(list(data.domains).map(async (d) => {
      const [tile, domain] = d.split(":");
      const params = { domain, tile_deg: tile.slice(1), limit: 2000 };
      const firstPage = await api("/inventory/partitions", { params });
      const pages = [firstPage];
      for (let offset = 2000; offset < firstPage.partitions && offset < 20000; offset += 2000) {
        pages.push(await api("/inventory/partitions", { params: { ...params, offset } }));
      }
      return pages.flatMap((p) => list(p.page));
    }));
    state.inventory = domains.flatMap((p) => p.value || []).filter((r) => r.state !== "SUBDIVIDED").map((r) => ({
      coords: [[r.ra_min, r.dec_min], [r.ra_max, r.dec_min], [r.ra_max, r.dec_max], [r.ra_min, r.dec_max]],
      closed: true, certified: ["SEARCHED_HAS_OBS", "SEARCHED_EMPTY"].includes(r.state),
    }));
    if (domains.some((p) => p.status === "rejected")) notice("Some inventory domains could not be loaded.", "error");
  } catch (e) {
    notice(`Inventory unavailable: ${e.message}`, "error");
  } finally {
    state.inventoryLoading = false;
    if (state.allSky) drawMap();
  }
}

function reportLink(doc) {
  const id = doc.investigation_id;
  if (!id) return action("Export saved run data", "export-run");
  return stack([action("Download readable report", "export", "md"), action("Download report JSON", "export", "json")]);
}

function renderHeader() {
  const doc = state.doc;
  const target = doc?.target || state.targets.find((t) => t.target_id === state.targetId);
  const position = state.field;
  const name = target?.name || (doc?.run_id ? "Field investigation" : position ? "Coordinate search" : "Investigation");
  const id = doc?.investigation_id || doc?.run_id || state.targetId;
  const status = doc?.status;
  $("subtitle").textContent = state.targetId ? `${state.targetId} · ${label(target?.category)}` :
    doc?.run_id ? "Saved SPHEREx field investigation" : "Explore SPHEREx observations and trace findings to their evidence.";
  $("record-header").innerHTML = `<h2 id="record-title">${h(name)}</h2>${id ? `<p class="record-kind">${h(id)}</p>` : ""}
    ${status || doc?.data_origin ? `<div class="record-meta">${badge(doc?.data_origin === "REAL" ? "Stored real data" : doc?.data_origin, doc?.data_origin === "SYNTHETIC" ? "warn" : "")}
      ${badge(status?.execution ? `Run · ${label(status.execution)}` : "", "good")}${badge(status?.test_status ? `Test · ${label(status.test_status)}` : "", "warn")}</div>` : ""}`;
}

function overview(doc) {
  if (state.coverage) {
    const c = state.coverage;
    return `<div class="quick-answer"><h3>Coverage status</h3><p>${h(label(c.readiness))}</p></div>
      ${para(c.message || c.note)}
      ${facts([["Indexed observations", c.n_observations], ["Index as of", c.index_as_of], ["Loaded rows", list(c.observations).length]])}
      ${c.job?.job_id ? stack([action("View preparation job", "job", c.job.job_id)]) : ""}
      ${group("Observations", stack(list(c.observations).slice(0, 80).map((o) => obsAction(o.obs_id, o.obs_id, o.release)), "Observation rows will appear here when available."))}`;
  }
  if (doc.run_id) {
    const candidates = list(doc.candidates);
    return `<div class="quick-answer"><h3>Field result</h3><p>Inspect recorded sources and candidate links. These outputs use methods that have yet to be validated on real data.</p></div>
      ${facts([["Observations", list(doc.observations).length], ["Pipeline detections", list(doc.detections).length], ["Associated sources", list(doc.sources).length], ["Candidates", candidates.length]])}
      ${group("Candidates", stack(candidates.map((c) => action(label(c.kind), "candidate", c.candidate_id, c.candidate?.validity?.state)), "No candidates were recorded in this run."))}
      ${list(doc.warnings).length ? group("Limitations", list(doc.warnings).map(para).join("")) : ""}`;
  }
  const claims = state.claims;
  const head = list(claims?.claims).find((c) => c.claim_id === claims?.headline_claim);
  const otherAttempt = state.attempt && state.attempt.investigation_id !== doc.investigation_id;
  return `<div class="quick-answer"><h3>Quick answer</h3><p>${h(claims?.quick_answer || "The gated quick answer is unavailable for this record.")}</p></div>
    ${head ? `<div class="content-group">${stack([action(head.claim_type, "claim", head.claim_id, head.strength ? label(head.strength) : "Inspect headline claim")])}</div>` : ""}
    ${head ? group("What the result establishes", para(head.statement) + list(head.does_not_establish).slice(0, 2).map((x) => para(`Does not establish ${x}.`)).join("")) : ""}
    ${facts([["Observations measured", doc.coverage?.n_measured], ["Epochs", doc.coverage?.n_epochs], ["Source", doc.reference?.catalogue], ["Saved", doc.created_at?.slice(0, 10)]])}
    ${otherAttempt ? group("Latest attempt", stack([action(label(state.attempt.status?.execution), "version", state.attempt.investigation_id, "Separate from the latest usable result")])) : ""}
    ${doc.investigation_id ? group("Portable report", reportLink(doc)) : ""}
    ${claims?.error ? empty(claims.error, true) : ""}
    ${state.targetId?.startsWith("NASA-") ? group("Re-run", stack([action("Prepare a new target run", "submit-target", state.targetId)])) : ""}`;
}

function analysis(doc) {
  const dims = list(state.patterns?.dimensions);
  const hyps = list(doc.hypotheses);
  const charts = [
    action("Motion chart", "chart", "motion"),
    action("Flux over time", "chart", "flux"),
    action("Spectrum", "chart", "spectrum"),
    action("Observation table", "chart", "observations"),
  ];
  const open = hyps.filter((x) => ["CANNOT_EXCLUDE", "NOT_ASSESSABLE"].includes(x.status));
  return `<h3>Patterns by dimension</h3>${para(state.patterns?.cross_dimension?.note)}
    ${stack(dims.map((d) => action(label(d.dimension), "dimension", d.dimension, label(d.status))), "No dimension-level assessment was recorded.")}
    ${group("Hypotheses", hyps.length ? `${open.length ? para(`Open alternatives: ${open.map((x) => x.title || x.slug).join(", ")}.`) : ""}
      ${hyps.map((x) => details(x.title || x.slug, badge(x.status, "warn") + para(x.summary) +
        [...list(x.limitations), ...list(x.tests_unavailable)].map((q) => para(typeof q === "string" ? q : JSON.stringify(q))).join("") +
        stack([...list(x.supporting_evidence).map((id) => action(id, "reference", `evidence[${id}]`, "Supporting evidence")),
          ...list(x.contradicting_evidence).map((id) => action(id, "reference", `evidence[${id}]`, "Contradicting evidence"))], "No linked evidence.") + para(x.suggested_next_step))).join("")}`
      : empty("No target hypothesis record. Candidate investigations are available from the map and Overview."))}
    ${group("Charts", stack(charts))}
    ${state.targetId?.startsWith("NASA-") && doc.investigation_id === state.versions?.latest_usable ?
      group("Next test", stack([action("Open follow-up plan", "followup", state.targetId)])) : ""}
    ${state.patterns?.error ? empty(state.patterns.error, true) : ""}`;
}

function evidence(doc) {
  const observations = list(doc.measurements || doc.observations || state.coverage?.observations).filter(matchesAtFrame);
  const manifest = doc.input_manifest?.manifest_id;
  const candidates = list(doc.candidates);
  const sources = list(doc.sources);
  return `<h3>Claims</h3>${list(state.claims?.claims).length ? claimLinks(state.claims.claims) : empty("No target claim record in this view.")}
    ${candidates.length ? group("Candidates", stack(candidates.map((c) => action(label(c.kind), "candidate", c.candidate_id, c.candidate_id)))) : ""}
    ${sources.length ? group("Associated sources", details(`Browse ${sources.length} source records`, stack(sources.map((s) => action(s.source_id, "source", s.source_id, `${s.n_obs} observations`))))) : ""}
    ${group("Observations and measurements", stack(observations.map((m) => obsAction(m.obs_id, m.obs_id, m.status || m.meta?.release))))}
    ${group("Reproduce", `${manifest ? stack([action("Open frozen manifest", "manifest", manifest)]) : para("No frozen manifest is attached to this saved record.")}
      ${facts([["Methods version", doc.methods_version], ["Software version", doc.software_version ?? doc.provenance?.software_version], ["Config hash", doc.config_hash], ["Results hash", doc.results_hash]])}
      ${doc.reproduce ? `<pre>${h(doc.reproduce)}</pre>` : ""}`)}`;
}

function historyView(doc) {
  const versions = doc.run_id ? state.runs.filter((r) => r.run_id !== doc.run_id)
    : list(state.versions?.versions).filter((v) => v.investigation_id);
  const current = doc.investigation_id || doc.run_id;
  const others = versions.filter((r) => (r.investigation_id || r.run_id) !== current);
  return `<h3>${doc.run_id ? "Saved field runs" : "Saved versions"}</h3>
    ${stack(versions.map((v) => {
      const id = v.investigation_id || v.run_id;
      const sub = [v.is_latest_usable ? "Latest usable" : null, v.is_latest_attempt ? "Latest attempt" : null, id === current ? "Viewing" : null,
        v.supersession_reason || label(v.status?.test_status || v.status?.execution)].filter(Boolean).join(" · ");
      return action(id, doc.run_id ? "run" : "version", id, sub);
    }), "No other saved versions.")}
    ${others.length ? group("Compare with this record", `<label for="compare-version" class="compact">Select another saved record</label>
      <select id="compare-version">${others.map((v) => { const id = v.investigation_id || v.run_id; return `<option value="${h(id)}">${h(id)}</option>`; }).join("")}</select>
      <button class="quiet" type="button" data-action="compare" data-id="${h(current)}">Compare records</button>`) : ""}
    ${group("Export selected record", reportLink(doc))}`;
}

function renderInspector() {
  const root = $("record");
  const active = state.trace[state.traceIndex];
  const doc = state.doc || {};
  $("trace-nav").hidden = !state.trace.length;
  $("trace-back").disabled = !active;
  $("trace-forward").disabled = state.traceIndex + 1 >= state.trace.length;
  $("trace-label").textContent = active ? label(active.kind) : "";
  for (const b of $("inspector-tabs").querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.tab === state.tab);
  root.setAttribute("aria-busy", Boolean(state.loading));
  if (state.loading) root.innerHTML = empty("Loading investigation…");
  else if (state.error) root.innerHTML = empty(state.error, true);
  else if (active) root.innerHTML = traceView(state, active);
  else if (!state.doc && !state.coverage) root.innerHTML = empty("Open a saved investigation or use Search sky to check a region.");
  else {
    const pages = { overview, analysis, evidence, history: historyView };
    root.innerHTML = state.tab === "history" && !state.doc ? empty("No saved investigation versions for this view.") : pages[state.tab](doc);
  }
}

function render() {
  refreshMetadata();
  renderHeader();
  renderInspector();
  renderVisuals();
  library();
}

function renderVisuals() {
  drawMap();
  $("chart-kind").value = state.chart;
  renderChart($("chart"), $("chart-subtitle"), state, (r) => matches(r, false));
  const frames = frameRows();
  $("playback").hidden = !frames.length;
  $("frame").max = Math.max(0, frames.length - 1);
  const i = frames.findIndex((f) => f.obs_id === state.frame);
  $("frame").value = Math.max(0, i);
  $("frame-label").textContent = i >= 0 ? `MJD ${number(frames[i].mjd, 3)}` : `${frames.length} observations`;
  $("frame").setAttribute("aria-valuetext", i < 0 ? "All observations" : `MJD ${frames[i].mjd}`);
}

function setTab(tab) {
  state.tab = tab;
  state.trace = [];
  state.traceIndex = -1;
  renderInspector();
  $("record").scrollTop = 0;
}

async function trace(kind, id, request) {
  const item = { kind, id, returnTab: state.tab };
  state.trace.splice(state.traceIndex + 1, Infinity, item);
  state.traceIndex = state.trace.length - 1;
  state.tab = "evidence";
  renderInspector();
  $("record").scrollTop = 0;
  if (request) {
    try { item.data = await request(); }
    catch (e) { item.data = { error: e.message }; }
    if (state.trace[state.traceIndex] === item) renderInspector();
  }
}

function pick(item) {
  if (state.regionPick) {
    if (finite(item.ra) && finite(item.dec)) return pickPosition([item.ra, item.dec]);
    return;
  }
  if (item.id === "picked") return fieldDialog(state.picked);
  if (item.kind === "target") {
    openPanel("investigations", true);
    return loadTarget(item.id);
  }
  if (item.kind === "reference") {
    state.selection = item;
    drawMap();
    openPanel("investigations", true);
    return trace("reference", item.id === "target-seed" ? "target" : "reference");
  }
  if (!item.kind) return;
  state.selection = item;
  if (item.kind === "source") state.chart = "flux";
  renderVisuals();
  openPanel("investigations", true);
  trace(item.kind, item.id);
}

function stopPlayback() {
  clearInterval(state.timer);
  state.timer = null;
  $("play").innerHTML = svg("play");
  $("play").setAttribute("aria-label", "Play observation sequence");
}

function changeFrame(i) {
  state.frame = frameRows()[i]?.obs_id || null;
  renderVisuals();
}

function playFrames() {
  if (state.timer) return stopPlayback();
  const rows = frameRows();
  if (!rows.length) return;
  let index = 0;
  changeFrame(index);
  $("play").innerHTML = svg("pause");
  $("play").setAttribute("aria-label", "Pause observation sequence");
  state.timer = setInterval(() => {
    index++;
    if (index >= frameRows().length) return stopPlayback();
    changeFrame(index);
  }, 1000);
}

let dialogTask = 0;
function openDialog(title, content) {
  const task = ++dialogTask;
  clearInterval(state.jobTimer);
  $("dialog-title").textContent = title;
  $("dialog-content").innerHTML = content;
  if (!$("dialog").open) $("dialog").showModal();
  return task;
}

function dialogContent(task, html) {
  if (task === dialogTask && $("dialog").open) $("dialog-content").innerHTML = html;
}

function fieldDialog(position) {
  const center = finite(position?.ra) ? position : state.picked || sky.view;
  const targets = state.targets.map((t) => `<option value="${h(t.target_id)}">${h(t.name)}</option>`).join("");
  openDialog("Search sky", `<form id="search-form">
    <label for="jump-target" class="compact">Open a registered target</label>
    <select id="jump-target"><option value="">Select target…</option>${targets}</select>
    <div class="content-group"><h3>Explore coordinates</h3><p class="compact">Enter decimal degrees in the ICRS frame.</p></div>
    <div class="field-grid"><label>Right ascension <input name="ra" type="number" min="0" max="360" step="any" required value="${center.ra.toFixed(5)}"></label>
    <label>Declination <input name="dec" type="number" min="-90" max="90" step="any" required value="${center.dec.toFixed(5)}"></label>
    <label>Radius (°) <input name="radius" type="number" min=".005" max=".5" step="any" required value=".05"></label></div>
    <label for="sky-analysis" class="compact">Analysis to queue</label><select id="sky-analysis"><option value="field">Blind field search</option><option value="MOVING_OBJECT">Motion at this position</option><option value="VARIABLE_STAR">Variability at this position</option></select>
    <div class="form-actions content-group"><button type="submit" class="quiet" value="jump">Go to coordinates</button><button type="submit" class="primary" value="coverage">Check coverage</button><button type="submit" class="quiet" value="run">Queue analysis</button></div>
    <p class="compact content-group">Checking an unindexed region can queue archive preparation. The radius applies to the field search and coverage check. Motion and variability use the engine’s coordinate investigation defaults.</p></form>`);
}

function setPickMode(on) {
  state.regionPick = on;
  $("pick-region").setAttribute("aria-pressed", on);
  $("map-stage").dataset.pick = on;
  if (on) notice("Click a position on the sky image, or use Search sky for keyboard coordinate entry.");
  else notice("");
}

function pickPosition([ra, dec]) {
  if (!state.regionPick) return;
  setPickMode(false);
  state.picked = { ra, dec };
  drawMap();
  fieldDialog(state.picked);
}

function setPlane(value) {
  if (!Number.isInteger(value)) return;
  state.plane = Math.max(1, Math.min(102, value));
  $("wavelength-plane").value = state.plane;
  $("plane-prev").disabled = state.plane === 1;
  $("plane-next").disabled = state.plane === 102;
  const wave = state.wavelengths[state.plane - 1];
  $("wavelength-label").textContent = wave ? `${wave.toFixed(3)} µm · ${state.plane}/102` : `${state.plane}/102`;
  sky.survey.settings(state.plane - 1, state.layers.survey, Number($("image-brightness").value));
}

function positionField(field) {
  beginSelection();
  state.loading = null;
  state.field = field;
  state.chart = "observations";
  focusField();
  render();
}

async function fieldCoverage(field, position = true) {
  if (position) positionField(field);
  openPanel("investigations");
  notice("Checking observation coverage…");
  try {
    const coverage = await api("/coverage", { params: field });
    if (state.field !== field) return;
    state.coverage = coverage;
    notice(coverage.readiness === "PREPARING" ? "Archive preparation is queued. Track the job for updates." : "");
    render();
  } catch (e) {
    if (state.field === field) notice(`Coverage check failed: ${e.message}`, "error");
  }
}

function jobContent(data, id) {
  const j = data.job || data;
  const run = data.run?.run_id || data.run_id;
  const inv = data.investigation_id;
  const terminal = ["COMPLETE", "COMPLETED", "CANCELLED", "FAILED", "NO_COVERAGE", "INVALID_SOURCE_DATA", "UNAVAILABLE_AFTER_DEADLINE"].includes(j.state);
  const cancellationAvailable = data.readiness === "PREPARING" ||
    data.readiness === "UNAVAILABLE_AFTER_DEADLINE" && Boolean(j.auto_recovery?.scheduled || j.next_attempt_at) ||
    Boolean(j.state && !terminal);
  return `<p class="mono">${h(id)}</p>${facts([["Job", label(j.kind)], ["State", label(j.state ?? data.readiness ?? data.status?.execution)],
    ["Waiting on", j.waiting_on], ["Next retry", j.next_attempt_at], ["Worker update", j.updated_at]])}
    ${para(data.message || j.reason)}${data.must_resubmit === false && data.readiness === "PREPARING" ? para("The service owns retries. This request is already registered.") : ""}
    ${data.readiness === "UNAVAILABLE_AFTER_DEADLINE" ? para("Archive access could not complete within the initial deadline. This is an availability outcome.") : ""}
    ${run ? group("Result", stack([action("Open saved field run", "run", run)])) : ""}
    ${inv ? group("Result", stack([action("Open saved investigation", "version", inv)])) : ""}
    ${state.coverage?.job?.job_id === id && terminal && state.field ? group("Coverage", stack([action("Refresh coverage view", "refresh-coverage")])) : ""}
    ${cancellationAvailable ? group("Manage", stack([action("Cancel job", "cancel-job", id)])) : ""}
    ${list(data.events).length ? group("Job history", list(data.events).slice(-15).reverse().map((e) => details(`${e.at || e.created_at || ""} ${label(e.state || e.event || e.kind)}`.trim(), json(e))).join("")) : ""}`;
}

function openJob(id) {
  const task = openDialog("Job details", empty("Loading job…"));
  const refresh = async () => {
    if (state.jobRefreshing === task || dialogTask !== task) return;
    state.jobRefreshing = task;
    try {
      const data = await api(`/jobs/${key(id)}`);
      dialogContent(task, jobContent(data, id));
    } catch (e) {
      dialogContent(task, empty(e.message, true));
    } finally {
      if (state.jobRefreshing === task) state.jobRefreshing = null;
    }
  };
  refresh();
  state.jobTimer = setInterval(refresh, 4500);
}

async function showJobs() {
  const task = openDialog("Investigation jobs", empty("Loading jobs…"));
  try {
    const data = await api("/jobs", { params: { limit: 50 } });
    dialogContent(task, para(`${data.total} recorded jobs · showing up to 50`) + stack(list(data.jobs).slice().reverse().map((j) =>
      action(j.job_id, "job", j.job_id, `${label(j.kind)} · ${label(j.state)}`)), "No jobs recorded."));
  } catch (e) { dialogContent(task, empty(e.message, true)); }
}

async function submitJob(path, body) {
  const task = openDialog("Request status", empty("Submitting request…"));
  try {
    const data = await api(path, { body });
    if (task !== dialogTask || !$("dialog").open) return;
    const run = data.run_id || data.run?.run_id;
    const inv = data.investigation_id;
    const job = data.job?.job_id || data.job_id;
    if (run) { $("dialog").close(); openPanel("investigations"); if (!state.runs.some((x) => x.run_id === run)) loadRuns().catch(() => {}); return loadRun(run); }
    if (inv) { $("dialog").close(); openPanel("investigations"); return loadTarget(inv.split(":")[1], inv); }
    if (job) return openJob(job);
    dialogContent(task, para(data.message || data.note || label(data.readiness)));
  } catch (e) { dialogContent(task, empty(e.message, true)); }
}

async function showSystem() {
  const task = openDialog("Data and method status", empty("Loading service status…"));
  const results = await Promise.allSettled([api("/health"), api("/status"), api("/capabilities"), api("/inventory/domains")]);
  const [health, service, capabilities, inventory] = results.map((r) => r.value);
  dialogContent(task, `<h3>Service</h3>${facts([["Software", health?.software_version], ["Mode", health ? health.offline ? "Offline" : "Online" : "Unavailable"],
    ["Worker", health ? health.worker?.running ? "Running" : "Not running" : "Unavailable"], ["Freshness", service?.freshness?.status],
    ["All-sky completeness", service ? service.freshness?.all_sky_complete === true ? "Complete" : "Unverified / incomplete" : "Unavailable"]])}
    ${health && !health.worker?.running ? para("New jobs require a running worker. Saved results are still available.") : ""}
    ${inventory ? group("Inventory domains", list(inventory.domains).map(para).join("") || empty("No planned domains.")) : ""}
    ${service ? details("Service freshness and routes", json(service)) : ""}${capabilities ? details("Analysis capabilities and validation", json(capabilities)) : ""}
    ${results.some((r) => r.status === "rejected") ? empty("Some status records could not be loaded.", true) : ""}`);
}

async function showPreview(sha) {
  const task = openDialog("Cached FITS preview", empty("Checking the stored product and preparing display pixels…"));
  try {
    const p = await api("/evidence/preview", { params: { sha256: sha } });
    if (task !== dialogTask || !$("dialog").open) return;
    const bytes = atob(p.pixels);
    if (p.sha256 !== sha || p.pixel_format !== "grayscale_8_base64" || bytes.length !== p.width * p.height || p.width > 256 || p.height > 256) {
      throw new Error("Invalid or mismatched local preview.");
    }
    dialogContent(task, `${state.doc?.data_origin === "SYNTHETIC" ? badge("SYNTHETIC · NOT REAL DATA", "warn") : ""}
      <p class="compact">Display preview of checksum-matched FITS pixels in image coordinates. Each image uses its own intensity stretch.</p>
      <figure class="fits-preview"><canvas id="fits-canvas" width="${Number(p.width)}" height="${Number(p.height)}" role="img" aria-label="Grayscale preview of the cached FITS science image"></canvas></figure>
      ${facts([["HDU", p.hdu], ["Native image", `${p.source_width} × ${p.source_height} pixels`], ["Sample step", `${p.step_pixels} px`], ["Intensity stretch", `${p.stretch_percentiles.join(" to ")} percentile`]])}
      <p class="mono compact">${h(sha)}</p>`);
    const context = $("fits-canvas").getContext("2d");
    const pixels = context.createImageData(p.width, p.height);
    for (let i = 0; i < bytes.length; i++) {
      const gray = bytes.charCodeAt(i);
      pixels.data.set([gray, gray, gray, 255], i * 4);
    }
    context.putImageData(pixels, 0, 0);
  } catch (e) {
    dialogContent(task, empty(e.message, true));
  }
}

function updateFilter() {
  const getNum = (id) => $(id).value === "" || !finite($(id).valueAsNumber) ? undefined : $(id).valueAsNumber;
  const start = getNum("mjd-start"), end = getNum("mjd-end");
  const waveStart = getNum("wave-start"), waveEnd = getNum("wave-end");
  $("mjd-end").setCustomValidity(finite(start) && finite(end) && start > end ? "End MJD must be at or after start MJD." : "");
  $("wave-end").setCustomValidity(finite(waveStart) && finite(waveEnd) && waveStart > waveEnd ? "Maximum wavelength must be at or above minimum wavelength." : "");
  if (!$("mjd-end").checkValidity()) return $("mjd-end").reportValidity();
  if (!$("wave-start").checkValidity()) return $("wave-start").reportValidity();
  if (!$("wave-end").checkValidity()) return $("wave-end").reportValidity();
  state.filters = { detector: $("detector").value, release: $("release").value, quality: $("quality").value, start, end, waveStart, waveEnd };
  if (!frameRows().some((p) => p.obs_id === state.frame)) state.frame = null;
  if (!state.trace[state.traceIndex]) renderInspector();
  renderVisuals();
}

function download(content, name, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 3000);
}

async function exportReport(format) {
  try {
    const id = state.doc?.investigation_id;
    if (!id) return;
    const report = await api(`/investigations/${key(id)}/report`, { params: { format } });
    download(format === "md" ? report.markdown : JSON.stringify(report, null, 2), `${id.replaceAll(":", "-")}.${format}`, format === "md" ? "text/markdown" : "application/json");
  } catch (e) { notice(`Report export failed: ${e.message}`, "error"); }
}

async function copyView() {
  const url = new URL(location.pathname, location.origin);
  if (state.doc?.run_id) url.searchParams.set("run", state.doc.run_id);
  else if (state.doc?.investigation_id) url.searchParams.set("version", state.doc.investigation_id);
  else if (state.targetId) url.searchParams.set("target", state.targetId);
  for (const [key, value] of Object.entries(sky.view)) url.searchParams.set(key, value.toFixed(6));
  if (state.allSky) url.searchParams.set("scope", "all");
  url.searchParams.set("plane", state.plane);
  for (const [field, value] of Object.entries(state.filters)) {
    if (value !== undefined && value !== "") url.searchParams.set(`filter-${field}`, value);
  }
  try {
    await navigator.clipboard.writeText(url.href);
    notice("View link copied. It reopens this saved record on a server with access to the same data.");
  } catch {
    openDialog("Copy view link", `<label for="share-link">Saved view URL</label><input id="share-link" readonly value="${h(url.href)}">`);
    $("share-link").select();
  }
}

function runAction(button) {
  const { action: kind, id } = button.dataset;
  const close = () => { if ($("dialog").open) $("dialog").close(); };
  switch (kind) {
    case "target": return loadTarget(id);
    case "version": close(); openPanel("investigations"); return loadTarget(id.split(":")[1], id);
    case "run": close(); openPanel("investigations"); return loadRun(id);
    case "chart":
      state.chart = id;
      openPanel("charts", true);
      renderVisuals();
      return;
    case "axis": state.axis = Number(id); return renderVisuals();
    case "fit-evidence":
      focusField(true);
      openPanel("none");
      $("map-heading").scrollIntoView({ block: "nearest" });
      return;
    case "observation": return pick({ kind, id });
    case "candidate": case "source": case "detection": return pick({ kind, id });
    case "lookup": return trace(kind, id, () => api("/evidence/lookup", { params: id.length === 64 ? { sha256: id } : { obs_id: id } }));
    case "manifest": return trace(kind, id, () => api(`/manifests/${key(id)}`));
    case "preview": return showPreview(id);
    case "followup": return trace(kind, id, () => api(`/targets/${key(id)}/followup`));
    case "followup-run": return submitJob(`/followup/${key(id)}/run`, {});
    case "compare": {
      const b = $("compare-version")?.value;
      if (b) return trace(kind, id, () => api("/compare", { params: { a: id, b } }));
      return;
    }
    case "submit-target": return submitJob("/investigations", { target_id: id });
    case "job": return openJob(id);
    case "refresh-coverage": close(); return fieldCoverage(state.field, false);
    case "cancel-job":
      if (window.confirm("Cancel this queued or active job?")) return submitJob(`/jobs/${key(id)}/cancel`, {});
      return;
    case "export": return exportReport(id);
    case "export-run":
      if (state.doc?.run_id) download(JSON.stringify(state.doc, null, 2), `${state.doc.run_id}.json`, "application/json");
      return;
    default: return trace(kind, id);
  }
}

function bindControls() {
  document.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-action], g[data-action]");
    if (b) runAction(b);
  });
  $("chart").addEventListener("keydown", (e) => {
    if (e.target.matches("g[data-action]") && ["Enter", " "].includes(e.key)) {
      e.preventDefault();
      runAction(e.target);
    }
  });
  $("library-tabs").addEventListener("click", (e) => {
    const button = e.target.closest("[data-library]");
    if (!button) return;
    state.library = button.dataset.library;
    library();
    if (state.library === "runs") loadRuns().catch((e) => { if (state.library === "runs") $("library").innerHTML = empty(e.message, true); });
  });
  $("inspector-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]");
    if (b) setTab(b.dataset.tab);
  });
  $("trace-back").onclick = () => {
    const active = state.trace[state.traceIndex];
    if (state.traceIndex >= 0) state.traceIndex--;
    if (state.traceIndex < 0 && active) state.tab = active.returnTab;
    renderInspector();
    $("record").scrollTop = 0;
  };
  $("trace-forward").onclick = () => {
    if (state.traceIndex < state.trace.length - 1) state.traceIndex++;
    state.tab = "evidence";
    renderInspector();
    $("record").scrollTop = 0;
  };
  $("layers").addEventListener("change", (e) => {
    const layer = e.target.dataset.layer;
    if (!layer) return;
    state.layers[layer] = e.target.checked;
    if (layer === "survey") setPlane(state.plane);
    if (layer === "inventory" && e.target.checked) loadInventory();
    drawMap();
  });
  for (const id of ["detector", "release", "quality", "mjd-start", "mjd-end", "wave-start", "wave-end"]) $(id).addEventListener("change", updateFilter);
  $("clear-filters").onclick = () => {
    state.filters = {};
    $("mjd-end").setCustomValidity("");
    $("wave-end").setCustomValidity("");
    refreshMetadata();
    updateFilter();
  };
  $("chart-kind").onchange = () => { state.chart = $("chart-kind").value; renderVisuals(); };
  $("zoom-in").onclick = () => sky.zoom(.55);
  $("zoom-out").onclick = () => sky.zoom(1.8);
  $("focus-field").onclick = () => focusField();
  $("all-sky").onclick = showAllSky;
  $("plane-prev").onclick = () => setPlane(state.plane - 1);
  $("plane-next").onclick = () => setPlane(state.plane + 1);
  $("wavelength-plane").onchange = () => setPlane($("wavelength-plane").valueAsNumber);
  $("image-brightness").oninput = () => setPlane(state.plane);
  $("pick-region").onclick = () => setPickMode(!state.regionPick);
  document.querySelector(".map-tools").addEventListener("click", (event) => {
    const button = event.target.closest("[data-panel]");
    if (!button) return;
    openPanel(workspace.dataset.panel === button.dataset.panel ? "none" : button.dataset.panel);
  });
  $("skip-inspector").onclick = (event) => {
    event.preventDefault();
    openPanel("investigations");
    $("inspector").focus();
  };
  $("play").onclick = playFrames;
  $("frame").oninput = () => { stopPlayback(); changeFrame(Number($("frame").value)); };
  $("show-all").onclick = () => { stopPlayback(); state.frame = null; renderVisuals(); };
  $("open-field").onclick = fieldDialog;
  $("show-jobs").onclick = showJobs;
  $("show-system").onclick = showSystem;
  $("share").onclick = copyView;
  $("close-dialog").onclick = () => $("dialog").close();
  $("dialog").addEventListener("close", () => {
    if ($("dialog").open) return;
    dialogTask++;
    clearInterval(state.jobTimer);
  });
  $("dialog").addEventListener("change", (e) => {
    if (e.target.id !== "jump-target" || !e.target.value) return;
    $("dialog").close();
    openPanel("investigations");
    loadTarget(e.target.value);
  });
  $("dialog").addEventListener("submit", (e) => {
    e.preventDefault();
    if (e.target.id === "api-auth") {
      state.token = new FormData(e.target).get("token");
      $("dialog").close();
      return boot();
    }
    if (e.target.id !== "search-form") return;
    const field = Object.fromEntries([...new FormData(e.target)].map(([k, v]) => [k, Number(v)]));
    const intent = e.submitter?.value || "jump";
    if (intent === "run") {
      const category = $("sky-analysis").value;
      if (category === "field") return submitJob("/runs", field);
      return submitJob("/investigations", { ra: field.ra, dec: field.dec, category });
    }
    $("dialog").close();
    if (intent === "coverage") return fieldCoverage(field);
    return positionField(field);
  });
  new ResizeObserver(() => renderChart($("chart"), $("chart-subtitle"), state, (r) => matches(r, false))).observe($("chart-heading").closest("section"));
}

async function boot() {
  try {
    const health = await api("/health");
    $("connection").dataset.state = "connected";
    $("connection").innerHTML = `<i></i>${health.offline ? "Offline mode" : "API connected"}`;
    $("software-version").textContent = health.software_version ?? "";
    const { targets } = await api("/targets");
    state.targets = list(targets);
    const params = new URLSearchParams(location.search);
    const plane = Number(params.get("plane"));
    if (params.has("plane")) setPlane(plane);
    const version = params.get("version"), run = params.get("run");
    const target = params.get("target") || (state.targets.find((t) => t.target_id === "NASA-02" && t.saved) || state.targets.find((t) => t.saved) || state.targets[0])?.target_id;
    if (run) await loadRun(run);
    else if (version && /^tinv:[^:]+:[a-f0-9]{8}$/.test(version)) await loadTarget(version.split(":")[1], version);
    else if (target) await loadTarget(target);
    else { render(); focusField(); }
    if (params.get("scope") === "all" || !run && !version && !params.has("target")) await showAllSky();
    if (["ra", "dec", "span"].every((k) => params.has(k) && finite(Number(params.get(k))))) {
      sky.setView(...["ra", "dec", "span"].map((k) => Number(params.get(k))));
    }
    for (const field of ["detector", "release", "quality", "start", "end", "waveStart", "waveEnd"]) {
      const value = params.get(`filter-${field}`);
      if (value != null) state.filters[field] = ["start", "end", "waveStart", "waveEnd"].includes(field) && finite(Number(value)) ? Number(value) : value;
    }
    refreshMetadata();
    updateFilter();
  } catch (e) {
    $("connection").dataset.state = "error";
    $("connection").innerHTML = "<i></i>API unavailable";
    if (e.status === 401) {
      openDialog("Connect to HEIMDALL", `<form id="api-auth"><label for="api-token">API access token</label><input id="api-token" name="token" type="password" required autocomplete="off"><div class="content-group"><button class="primary" type="submit">Connect</button></div></form>`);
    }
    state.error = e.status === 401 ? "This server requires an API token." : `Unable to open HEIMDALL data: ${e.message}`;
    render();
  }
}

bindControls();
updateCoordinates(sky.view);
render();
setPlane(2);
wavelengths().then((values) => { state.wavelengths = values; setPlane(state.plane); }).catch(() => {});
boot();
