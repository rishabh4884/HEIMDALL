const escaped = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const h = (x) => String(x ?? "").replace(/[&<>"']/g, (c) => escaped[c]);
export const list = (x) => Array.isArray(x) ? x : [];
export const first = (x) => Array.isArray(x) ? x[0] : x;
export const finite = (x) => typeof x === "number" && Number.isFinite(x);
export const number = (x, digits = 2) => finite(x) ? new Intl.NumberFormat("en", { maximumFractionDigits: digits, useGrouping: false }).format(x) : "—";
export const label = (x) => String(x ?? "Unknown").replaceAll("_", " ").toLowerCase().replace(/^[a-z]/, (a) => a.toUpperCase());
export const svg = (id) => `<svg aria-hidden="true"><use href="#i-${id}"></use></svg>`;
export const key = (x) => encodeURIComponent(x).replaceAll("%3A", ":").replaceAll("%2B", "+");
export const empty = (x, error = false) => `<p class="empty${error ? " error" : ""}">${h(x)}</p>`;
export const facts = (rows) => `<dl class="facts">${rows.filter(([, v]) => v != null && v !== "").map(([k, v]) => `<div><dt>${h(k)}</dt><dd>${h(v)}</dd></div>`).join("")}</dl>`;
export const badge = (text, tone = "") => text ? `<span class="chip ${tone}">${h(text)}</span>` : "";
export const group = (title, body) => `<section class="content-group"><h3>${h(title)}</h3>${body}</section>`;
export const para = (x) => x ? `<p class="compact">${h(x)}</p>` : "";
export const action = (text, kind, id = "", sub = "") => `<button class="record-link" type="button" data-action="${h(kind)}" data-id="${h(id)}"><span>${h(text)}${sub ? `<small>${h(sub)}</small>` : ""}</span>${svg("arrow")}</button>`;
export const stack = (items, fallback = "No records available.") => items.length ? `<div class="stack">${items.join("")}</div>` : empty(fallback);
export const details = (title, body) => `<details class="record-detail"><summary>${h(title)}</summary>${body}</details>`;
export const json = (x) => `<pre>${h(JSON.stringify(x, null, 2))}</pre>`;
export const apiPath = (p, params) => `/api/v1${p}${params ? `?${new URLSearchParams(params)}` : ""}`;
export function table(headers, rows) {
  if (!rows.length) return empty("No matching records.");
  return `<div class="table-wrap" tabindex="0" aria-label="Data table"><table><thead><tr>${headers.map((x) => `<th scope="col">${h(x)}</th>`).join("")}</tr></thead><tbody>${rows.map((cols) => `<tr>${cols.map((x) => `<td>${x}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

export function dataOf(state) {
  const doc = state.doc || {};
  const products = new Map(list(doc.products || doc.input_manifest?.products).map((p) => [p.obs_id, p]));
  const observations = new Map(list(doc.observations).map((o) => [o.obs_id, o]));
  const measurements = new Map(list(doc.measurements).map((m) => [m.obs_id, m]));
  return { doc, products, observations, measurements };
}

export function productFor(state, id) {
  const { products, observations } = dataOf(state);
  const o = observations.get(id);
  return products.get(id) || (o && {
    obs_id: id, sha256: o.product_checksum, release: o.meta?.release, origin: o.retrieval_origin,
    access_reference: o.product_access_reference,
  });
}

export const obsAction = (id, text = id, sub = "") => action(text, "observation", id, sub);
export function claimLinks(claims) {
  return stack(list(claims).map((c) => action(c.claim_type, "claim", c.claim_id, label(c.analysis))));
}

function fieldRelationships(investigation) {
  const evidence = new Map(list(investigation.evidence).map((e) => [e.evidence_id, e]));
  const link = (id) => {
    const record = evidence.get(id);
    return record ? action(label(record.type), "run-evidence", id, label(record.domain)) : para(id);
  };
  const pairs = list(investigation.evidence_relations).map((r) =>
    `<div class="evidence-pair">${link(r.a)}<p class="compact">${h(label(r.kind))} · ${r.merged ? "This relation merged the groups" : "This relation did not merge the groups"}</p>${link(r.b)}</div>`);
  const groups = list(investigation.dependency_groups).map((g) =>
    details(`Evidence group ${g.group_id}`, list(g.merged_because).map((x) => para(label(x))).join("") + stack(list(g.evidence_ids).map(link))));
  const conclusions = list(investigation.hypotheses).map((x) =>
    details(label(x.hypothesis_id), badge(x.status, "warn") + para(x.statement) + para(x.status_rule) +
      facts([["Supporting groups", x.n_supporting_groups], ["Contradicting groups", x.n_contradicting_groups]]) +
      details("Predictions and checks", json(x.predictions || []))));
  return details("Pattern and evidence relationships", stack(pairs, "No cross-evidence links recorded.") + groups.join("")) +
    details("Competing explanations", conclusions.join("") || empty("No competing hypotheses recorded."));
}

export function claimView(state, id) {
  const c = list(state.claims?.claims).find((c) => c.claim_id === id);
  if (!c) return empty("This claim is unavailable in the selected version.");
  const quantities = list(c.values).map((v) => [label(v.name || v.quantity), [finite(v.value) ? number(v.value, 4) : Array.isArray(v.value) ? v.value.map((x) => number(x, 4)).join(", ") : v.value,
    v.uncertainty == null ? "" : `± ${Array.isArray(v.uncertainty) ? v.uncertainty.map((x) => number(x, 4)).join(", ") : number(v.uncertainty, 4)}`, v.unit].filter((x) => x !== "" && x != null).join(" ")]);
  const refs = list(c.evidence_refs);
  return `<h3>${h(c.claim_type)}</h3>${para(c.statement)}${badge(c.strength, "warn")}${facts(quantities)}
    ${group("Evidence", stack(refs.map((r) => action(r, "reference", r))))}
    ${group("Observations used", stack([...new Set(list(c.observations_used))].map((r) => obsAction(r)), "No observations support this claim."))}
    ${group("What this does not establish", list(c.does_not_establish).map(para).join("") || empty("No additional statement."))}
    ${group("Defensibility checks", list(c.gate?.checks).map((g) => details(`${label(g.check || g.name || g.id)} · ${g.status || (g.passed === true ? "Passed" : g.passed === false ? "Failed" : "Recorded")}`, json(g))).join("") || empty("Checks not recorded."))}
    ${c.downgraded_from ? para(`Downgraded from ${c.downgraded_from}.`) : ""}${details("Method record", json(c.method || {}))}`;
}

// The allowed syntax matches recorded JSON paths. No client-side evaluation.
function findPath(doc, ref) {
  if (!/^\w+(?:\.\w+|\[\d+\])*$/.test(ref)) return undefined;
  return ref.match(/\w+/g).reduce((obj, field) => obj && Object.hasOwn(obj, field) ? obj[field] : undefined, doc);
}

export function traceView(state, trace) {
  const { doc, observations, measurements } = dataOf(state);
  const { id, kind, data } = trace;
  if (kind === "claim") return claimView(state, id);
  if (kind === "reference") {
    const ev = /^evidence\[(.+)\]$/.exec(id);
    const value = ev ? list(doc.evidence).find((x) => x.evidence_id === ev[1]) : findPath(doc, id);
    const ids = new Set();
    for (const item of Array.isArray(value) ? value : [value]) if (item?.obs_id) ids.add(item.obs_id);
    const siblings = list(state.claims?.claims).filter((c) => list(c.evidence_refs).includes(id));
    for (const c of siblings) for (const o of list(c.observations_used)) ids.add(o);
    return `<h3 class="mono">${h(id)}</h3>${value === undefined ? empty("This reference does not resolve to a record in this version.") : json(value)}
      ${ids.size ? group("Related observations", stack([...ids].map((o) => obsAction(o)))) : ""}
      ${siblings.length ? group("Claims using this record", claimLinks(siblings)) : ""}`;
  }
  if (kind === "observation") {
    const m = measurements.get(id);
    const o = observations.get(id);
    const p = productFor(state, id);
    const f = m?.forced || {};
    const footprint = o || list(state.coverage?.observations).find((x) => x.obs_id === id);
    const matching = list(state.claims?.claims).filter((c) => list(c.observations_used).includes(id));
    return `<h3>Observation</h3><p class="mono">${h(id)}</p>
      ${facts([["Detector", m?.detector ?? o?.meta?.detector ?? footprint?.detector], ["MJD", number(m?.mjd ?? o?.meta?.mjd_avg ?? footprint?.t_min_mjd, 6)], ["Wavelength", finite(m?.wavelength_um) ? `${number(m.wavelength_um, 4)} μm` : null], ["Measurement", m?.status ?? f.status], ["Forced flux", finite(f.flux_ujy) ? `${number(f.flux_ujy)} µJy` : null], ["Flux uncertainty", finite(f.flux_sigma_ujy) ? `${number(f.flux_sigma_ujy)} µJy` : null], ["S/N", finite(f.snr) ? number(f.snr) : null], ["Included in run", o?.included == null ? null : o.included ? "Yes" : "No"]])}
      ${m ? para("Forced photometry at the requested position is a measurement. Usability and claim strength are assessed separately.") : ""}
      ${[...list(m?.reasons), ...list(o?.exclusion_reasons)].map(para).join("")}
      ${finite(m?.centroid?.ra_deg) && finite(m?.centroid?.dec_deg) ? group("On the sky map", stack([action("Zoom to measured positions", "fit-evidence")])) : ""}
      ${p ? group("Product provenance", stack([action("Inspect source product", "product", id, p.release),
        ...(p.sha256 ? [action("View cached FITS pixels", "preview", p.sha256)] : [])])) : empty("Product provenance is unavailable in this view.")}
      ${matching.length ? group("Claims using this observation", claimLinks(matching)) : ""}
      ${m || footprint ? details("Full observation record", json(m || footprint)) : ""}`;
  }
  if (kind === "product") {
    const p = productFor(state, id);
    if (!p) return empty("No saved product record.");
    const man = state.doc?.input_manifest?.manifest_id;
    return `<h3>Source product</h3><p class="mono">${h(id)}</p>${facts([["Release", p.release], ["Retrieval origin", p.origin], ["Retrieved at", p.retrieved_at], ["Product version", p.product_version]])}
      ${group("Access reference", `<p class="mono compact">${h(p.access_reference || "Unavailable")}</p>`)}
      ${group("SHA-256", `<p class="mono compact">${h(p.sha256 || "Unavailable")}</p>`)}
      ${group("Trace this product", stack([...(p.sha256 ? [action("View cached FITS pixels", "preview", p.sha256)] : []), obsAction(id, "Back to observation"),
        action("Find dependent results", "lookup", p.sha256 || id), ...(man ? [action("Open frozen manifest", "manifest", man)] : [])]))}`;
  }
  if (kind === "dimension") {
    const d = list(state.patterns?.dimensions).find((x) => x.dimension === id);
    if (!d) return empty("Pattern dimension unavailable.");
    const refs = Object.values(d.links || {}).flat().filter((x) => typeof x === "string");
    return `<h3>${h(label(id))} pattern</h3>${badge(d.status)}${para(d.finding)}${para(d.reason)}${para(d.guard)}${para(d.limitation)}${para(d.physical_interpretation)}
      ${group("Evidence dependencies", para(list(d.dependency_groups).join(", ") || "None recorded.") + para(state.patterns?.cross_dimension?.note))}
      ${refs.length ? group("Linked records", stack(refs.map((r) => action(r, "reference", r.startsWith("ev:") ? `evidence[${r}]` : r)))) : ""}
      ${details("Pattern record", json(d))}`;
  }
  if (kind === "run-evidence") {
    const investigations = list(doc.investigations?.investigations);
    const item = investigations.flatMap((i) => list(i.evidence)).find((e) => e.evidence_id === id);
    if (!item) return empty("This evidence is unavailable in the saved field run.");
    return `<h3>${h(label(item.type))}</h3><p class="mono">${h(id)}</p>
      ${badge(item.admissible ? "Admissible" : "Not admissible", item.admissible ? "good" : "warn")}${list(item.admissibility_reasons).map(para).join("")}
      ${facts([["Domain", item.domain], ["Relation", item.relation], ["Quality grade", item.quality_grade], ["Saved analysis", item.ref]])}
      ${group("Underlying observations", stack(list(item.basis?.obs_ids).map((o) => obsAction(o))))}
      ${list(item.basis?.det_ids).length ? group("Underlying detections", stack(item.basis.det_ids.map((d) => action(d, "detection", d)))) : ""}
      ${details("Full evidence record", json(item))}`;
  }
  if (kind === "candidate" || kind === "source" || kind === "detection") {
    const types = { candidate: "candidates", source: "sources", detection: "detections" };
    const row = list(doc[types[kind]]).find((x) => (x.candidate_id || x.source_id || x.det_id) === id);
    if (!row) return empty("The selected map record is no longer available.");
    const inv = list(doc.investigations?.investigations).filter((i) => list(i.candidate_ids).includes(id));
    const memberIds = new Set(list(row.det_ids));
    const ids = new Set(list(doc.detections).filter((d) => memberIds.has(d.det_id)).map((d) => d.obs_id));
    if (row.obs_id) ids.add(row.obs_id);
    const related = [...ids].map((o) => obsAction(o));
    if (row.subject_kind === "SOURCE" && list(doc.sources).some((s) => s.source_id === row.subject_id)) related.unshift(action("Explore associated source", "source", row.subject_id));
    return `<h3>${h(label(kind))}</h3><p class="mono">${h(id)}</p>${para(row.explanation || row.physical_interpretation)}
      ${kind === "candidate" ? para("Candidate for follow-up. See the recorded interpretation and its limitations.") : ""}
      ${facts([["Category", row.kind || row.category], ["Recorded validity", row.candidate?.validity?.state], ["Position RA", row.position?.ra_deg ?? row.ra], ["Position Dec", row.position?.dec_deg ?? row.dec], ["Quality grade", row.quality_grade], ["Observations", row.n_obs], ["Flux", finite(row.flux_ujy) ? `${number(row.flux_ujy)} µJy` : null]])}
      ${related.length ? group("Related evidence", stack(related)) : ""}
      ${inv.map((i) => group(`Investigation · ${label(i.interpretation?.status)}`,
        para(i.interpretation?.note) + list(i.interpretation?.contradictions).map(para).join("") +
        stack(list(i.evidence).map((e) => action(label(e.type), "run-evidence", e.evidence_id, e.admissible ? "Admissible" : "Not admissible"))) +
        fieldRelationships(i))).join("")}
      ${details("Full record", json(row))}`;
  }
  if (kind === "lookup") {
    if (!data) return empty("Looking up dependent results…");
    if (data.error) return empty(data.error, true);
    const rows = [...list(data.versions), ...list(data.runs).map((r) => typeof r === "string" ? { run_id: r } : r)];
    return `<h3>Dependent results</h3>${stack(rows.flatMap((r) => {
      const id = r.investigation_id || r.run_id;
      return id ? [action(id, r.run_id ? "run" : "version", id, r.superseded_by ? "Superseded" : undefined)] : [];
    }), "No dependent result links were returned.")}
    ${list(data.manifests).length ? group("Dependent manifests", stack(data.manifests.map((id) => action(id, "manifest", id)))) : ""}
    ${details("Lookup response", json(data))}`;
  }
  const title = { manifest: "Frozen input manifest", compare: "Version comparison", followup: "Follow-up plan" }[kind] || label(kind);
  if (!data) return empty(`Loading ${title.toLowerCase()}…`);
  if (data.error) return empty(data.error, true);
  if (kind === "compare") return `<h3>${title}</h3>${badge(data.comparability?.status, "warn")}${list(data.comparability?.reasons).map(para).join("")}
    ${data.changes != null ? group("Recorded changes", json(data.changes)) : para("The backend did not compute changes for this pair.")}${details("Comparison record", json(data))}`;
  if (kind === "followup") return `<h3>${title}</h3>${para(data.stopping_reason)}${list(data.actions).map((a) =>
    details(a.title || a.action_id, json(a) + (data.next_action === a.action_id || data.next_action?.action_id === a.action_id ? action("Queue this follow-up", "followup-run", a.action_id) : ""))).join("")}${details("Budget and plan record", json(data))}`;
  return `<h3>${h(title)}</h3>${json(data)}`;
}
