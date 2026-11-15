// Canonical identities and the shareable view state. Pure functions only (tested under node).
//
// One identity model is shared by the map, tables, charts, inspector, history and export:
//   target   NASA-0N | COORD-...          a registered or coordinate target (its latest usable version is resolved by the server)
//   version  tinv:<target>:<8 hex>        one immutable saved investigation version
//   run      R<yyyymmdd>T<hhmmss>-<8 hex> one saved blind field run
//   field    ra,dec,radius                a coordinate region being browsed (coverage only; never an analysis)
// Selections inside a record: observation (obs_id), product (sha256), candidate, source, detection, claim, evidence, manifest.

export const PATTERNS = {
  target: /^(NASA-0[1-5]|COORD-[0-9.]+[+-][0-9.]+)$/,
  version: /^tinv:[A-Za-z0-9.+-]+:[0-9a-f]{8}$/,
  run: /^R\d{8}T\d{6}-[0-9a-f]{8}$/,
  manifest: /^man:[0-9a-f]{8,64}$/,
  sha256: /^[0-9a-f]{64}$/,
  job: /^job:[a-z_]+:[0-9a-f]{12}$/,
};
export const SELECTION_KINDS = ["observation", "product", "candidate", "source", "detection", "claim", "evidence", "reference", "manifest"];
export const TABS = ["answer", "evidence", "deeper", "history"];
export const LAYERS = ["survey", "grid", "reference", "coverage", "detections", "sources", "candidates", "tracks", "associations",
  "catalogue", "inventory", "planes", "constellations"];
export const DEFAULT_LAYERS = new Set(["survey", "reference", "coverage", "detections", "sources", "candidates", "tracks"]);
const FILTER_KEYS = { detector: "f.det", release: "f.rel", quality: "f.q", start: "f.t0", end: "f.t1", waveStart: "f.w0", waveEnd: "f.w1" };
const NUMERIC_FILTERS = new Set(["start", "end", "waveStart", "waveEnd"]);
const finite = (x) => typeof x === "number" && Number.isFinite(x);

export function targetOfVersion(id) {
  const m = /^tinv:(.+):[0-9a-f]{8}$/.exec(id || "");
  return m ? m[1] : null;
}

/** Identify a typed or pasted identifier. Returns {kind, id} or null. */
export function identify(text) {
  const t = String(text || "").trim();
  for (const kind of ["version", "run", "manifest", "job", "target"]) if (PATTERNS[kind].test(t)) return { kind, id: t };
  if (PATTERNS.sha256.test(t.toLowerCase())) return { kind: "product", id: t.toLowerCase() };
  if (/^\d{4}W\d{2}_\w+_D?\d/.test(t) || /^\d{4}W\d{2}_[0-9A-Z_]+$/.test(t)) return { kind: "observation", id: t };
  return null;
}

/** Parse "RA Dec" in decimal degrees or sexagesimal (h:m:s d:m:s). Returns {ra, dec} or {error}. */
export function parseCoordinates(text) {
  const t = String(text || "").trim().replace(/[,;]+/g, " ").replace(/\s+/g, " ");
  if (!t) return { error: "Enter a position." };
  const sexa = /^(\d{1,2})[:h ](\d{1,2})[:m ](\d{1,2}(?:\.\d+)?)s?\s+([+-]?)(\d{1,2})[:d° ](\d{1,2})[:m' ](\d{1,2}(?:\.\d+)?)["s]?$/i.exec(t);
  if (sexa) {
    const [, h, m, s, sign, d, dm, ds] = sexa;
    const ra = 15 * (Number(h) + Number(m) / 60 + Number(s) / 3600);
    const dec = (sign === "-" ? -1 : 1) * (Number(d) + Number(dm) / 60 + Number(ds) / 3600);
    if (Number(h) > 23 || Number(m) > 59 || Number(s) >= 60 || Number(dm) > 59 || Number(ds) >= 60) return { error: "Sexagesimal fields are out of range." };
    return validPosition(ra, dec);
  }
  const parts = t.split(" ");
  if (parts.length === 2 && parts.every((p) => /^[+-]?\d+(\.\d+)?$/.test(p))) return validPosition(Number(parts[0]), Number(parts[1]));
  return { error: "Use decimal degrees (e.g. 150.1 2.2) or h:m:s d:m:s." };
}

function validPosition(ra, dec) {
  if (!finite(ra) || ra < 0 || ra >= 360) return { error: "Right ascension must be in [0, 360)." };
  if (!finite(dec) || dec < -90 || dec > 90) return { error: "Declination must be in [-90, 90]." };
  return { ra, dec };
}

/** Serialize the reproducible part of the view. Credentials are never part of the state. */
export function serialize(view) {
  const p = new URLSearchParams();
  const r = view.record;
  if (r?.kind === "version" || r?.kind === "run" || r?.kind === "target") p.set(r.kind, r.id);
  else if (r?.kind === "field" && r.field) p.set("field", [r.field.ra, r.field.dec, r.field.radius].map((x) => round(x, 6)).join(","));
  if (view.camera) {
    p.set("ra", round(view.camera.ra, 6));
    p.set("dec", round(view.camera.dec, 6));
    p.set("span", round(view.camera.span, 6));
  }
  if (Number.isInteger(view.plane)) p.set("plane", String(view.plane));
  if (view.layers) {
    const on = LAYERS.filter((l) => view.layers[l]);
    const isDefault = on.length === DEFAULT_LAYERS.size && on.every((l) => DEFAULT_LAYERS.has(l));
    if (!isDefault) p.set("layers", on.join(",") || "none");
  }
  for (const [name, param] of Object.entries(FILTER_KEYS)) {
    const v = view.filters?.[name];
    if (v !== undefined && v !== null && v !== "") p.set(param, String(v));
  }
  if (view.selection?.kind && view.selection.id) p.set("sel", `${view.selection.kind}:${view.selection.id}`);
  if (view.frame) p.set("frame", view.frame);
  if (view.tab && view.tab !== "answer") p.set("tab", view.tab);
  if (view.compare?.a && view.compare?.b) p.set("cmp", `${view.compare.a}..${view.compare.b}`);
  if (view.twin?.a && view.twin?.b) p.set("twin", `${view.twin.a},${view.twin.b}`);
  if (view.separated) p.set("sep", "1");
  return p.toString();
}

const round = (x, digits) => String(Number(Number(x).toFixed(digits)));

/** Parse a location search string. Invalid parts are reported in `problems` and dropped; valid parts are kept. */
export function parse(search) {
  const p = new URLSearchParams(search || "");
  const out = { problems: [], filters: {}, layers: null };
  const bad = (what, value) => out.problems.push(`${what} "${String(value).slice(0, 80)}" is not valid and was ignored.`);
  for (const kind of ["version", "run", "target"]) {
    if (!p.has(kind)) continue;
    const id = p.get(kind);
    if (PATTERNS[kind].test(id)) { out.record = { kind, id }; break; }
    bad(`The ${kind} identifier`, id);
  }
  if (!out.record && p.has("field")) {
    const [ra, dec, radius] = p.get("field").split(",").map(Number);
    if (finite(ra) && finite(dec) && finite(radius) && ra >= 0 && ra < 360 && Math.abs(dec) <= 90 && radius > 0 && radius <= 1) {
      out.record = { kind: "field", field: { ra, dec, radius } };
    } else bad("The field", p.get("field"));
  }
  if (["ra", "dec", "span"].some((k) => p.has(k))) {
    const [ra, dec, span] = ["ra", "dec", "span"].map((k) => Number(p.get(k)));
    if (finite(ra) && finite(dec) && finite(span) && Math.abs(dec) <= 90 && span > 0 && span <= 360) out.camera = { ra: ((ra % 360) + 360) % 360, dec, span };
    else bad("The camera", `${p.get("ra")},${p.get("dec")},${p.get("span")}`);
  }
  if (p.has("plane")) {
    const plane = Number(p.get("plane"));
    if (Number.isInteger(plane) && plane >= 1 && plane <= 102) out.plane = plane;
    else bad("The wavelength plane", p.get("plane"));
  }
  if (p.has("layers")) {
    const names = p.get("layers") === "none" ? [] : p.get("layers").split(",");
    const unknown = names.filter((n) => !LAYERS.includes(n));
    if (unknown.length) bad("The layer list entry", unknown.join(","));
    out.layers = Object.fromEntries(LAYERS.map((l) => [l, names.includes(l)]));
  }
  for (const [name, param] of Object.entries(FILTER_KEYS)) {
    if (!p.has(param)) continue;
    const raw = p.get(param);
    if (NUMERIC_FILTERS.has(name)) {
      if (finite(Number(raw)) && raw.trim() !== "") out.filters[name] = Number(raw);
      else bad(`The ${name} filter`, raw);
    } else if (name === "quality" && !["usable", "excluded"].includes(raw)) bad("The quality filter", raw);
    else if (name === "detector" && !/^[1-6]$/.test(raw)) bad("The detector filter", raw);
    else out.filters[name] = raw;
  }
  if (p.has("sel")) {
    const raw = p.get("sel");
    const i = raw.indexOf(":");
    const kind = raw.slice(0, i), id = raw.slice(i + 1);
    if (i > 0 && SELECTION_KINDS.includes(kind) && id && id.length <= 200) out.selection = { kind, id };
    else bad("The selection", raw);
  }
  if (p.has("frame")) {
    const f = p.get("frame");
    if (f && f.length <= 120 && /^[\w.-]+$/.test(f)) out.frame = f; else bad("The time frame", f);
  }
  if (p.has("tab")) {
    if (TABS.includes(p.get("tab"))) out.tab = p.get("tab"); else bad("The tab", p.get("tab"));
  }
  if (p.has("cmp")) {
    const [a, b] = p.get("cmp").split("..");
    const ok = (x) => PATTERNS.version.test(x) || PATTERNS.run.test(x);
    if (ok(a) && ok(b) && a !== b) out.compare = { a, b }; else bad("The comparison pair", p.get("cmp"));
  }
  if (p.has("twin")) {
    const [a, b] = p.get("twin").split(",");
    if (a && b && a !== b && [a, b].every((x) => /^[\w.-]{3,120}$/.test(x))) out.twin = { a, b }; else bad("The twin-view pair", p.get("twin"));
  }
  out.separated = p.get("sep") === "1";
  for (const secret of ["token", "access_token", "authorization"]) if (p.has(secret)) out.problems.push("Credentials in links are ignored; enter the token in the connection dialog.");
  return out;
}

/** Deterministic ordering of coincident or region-selected items: priority by kind, then identifier. */
const KIND_ORDER = ["candidate", "observation", "detection", "source", "reference", "target", "catalogue"];
export function orderItems(items) {
  const rank = (k) => { const i = KIND_ORDER.indexOf(k); return i < 0 ? KIND_ORDER.length : i; };
  return [...items].sort((a, b) => rank(a.kind) - rank(b.kind) || String(a.id).localeCompare(String(b.id)));
}
