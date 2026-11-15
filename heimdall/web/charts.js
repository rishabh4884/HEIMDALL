import { empty, finite, h, list, number, table } from "./records.js";
import { DETECTOR_RANGE_UM } from "./bands.js";

// Chart model (pure): every mark keeps its obs_id so map, chart, table and inspector share one identity.
// Fluxes are grouped by the backend's comparable series; points outside any series are never joined to it.
export function chartModel(state, matches, kind = state.chart, axis = state.axis) {
  const doc = state.doc || {};
  const an = doc.analyses || {};
  const omitted = { nonfinite: 0, filtered: 0 };
  const keep = (p) => {
    if (!matches(p)) { omitted.filtered++; return false; }
    return true;
  };
  if (kind === "motion") {
    const track = list(an.motion?.track).filter(keep);
    const points = [];
    for (const m of track) {
      const y = m.measured_offset_arcsec?.[axis], e = m.expected_offset_arcsec?.[axis];
      if (finite(m.mjd) && finite(y)) points.push({ x: m.mjd, y, sigma: m.sigma_arcsec?.[axis], id: m.obs_id, cls: "observed", group: `Measured · D${m.detector ?? "?"}` });
      else omitted.nonfinite++;
      if (finite(m.mjd) && finite(e)) points.push({ x: m.mjd, y: e, id: m.obs_id, cls: "reference", group: "Catalogue expectation" });
    }
    // Excluded epochs have no admissible offset to plot; they are listed with their reasons instead of being drawn.
    const excludedNotes = list(an.motion?.excluded).filter(keep).map((x) => `${x.obs_id}: ${list(x.reasons).join(", ") || "excluded"}`);
    return { kind, points, excluded: [], excludedNotes, omitted, x: "Modified Julian Date", y: `${axis ? "North" : "East"} offset (arcsec)`, line: "reference",
      note: "Measured centroid offsets vs the catalogue expectation at each real epoch. Bars: recorded 1σ. Registration is translation-only; offsets are angular, not physical." };
  }
  if (kind === "spectrum") {
    const source = list(doc.sources).find((s) => s.source_id === state.selection?.id);
    const raw = source ? list(source.spectrum?.points) : list(an.variability?.light_curve);
    const points = [], excluded = [];
    for (const p of raw.filter(keep)) {
      if (!(finite(p.wavelength_um) && finite(p.flux_ujy))) { omitted.nonfinite++; continue; }
      const mark = { x: p.wavelength_um, y: p.flux_ujy, sigma: p.flux_sigma_ujy, xerr: finite(p.bandwidth_um) ? p.bandwidth_um / 2 : null, id: p.obs_id,
        cls: p.usable === false ? "excluded" : "observed", group: `Detector ${p.detector ?? "?"}`, detector: p.detector, mjd: p.mjd, reasons: p.reasons, wsource: p.wavelength_source };
      (p.usable === false ? excluded : points).push(mark);
    }
    const coverage = source ? list(source.spectrum?.coverage) : list(an.variability?.spectrum?.coverage);
    return { kind, points, excluded, omitted, coverage, x: "Wavelength (µm)", y: "Flux density (µJy)",
      note: "Each point is one observation at its own calibrated wavelength (horizontal bar: bandwidth). Points are non-simultaneous and are never joined across detectors; shaded bands are detectors without coverage in this selection." };
  }
  if (kind === "flux") {
    const v = an.variability || {};
    const seriesOf = new Map();
    for (const s of list(v.series)) for (const p of list(s.points)) seriesOf.set(p.obs_id, s.series_id);
    const points = [], excluded = [];
    for (const p of list(v.light_curve).filter(keep)) {
      if (!(finite(p.mjd) && finite(p.flux_ujy))) { omitted.nonfinite++; continue; }
      const mark = { x: p.mjd, y: p.flux_ujy, sigma: p.flux_sigma_ujy, id: p.obs_id, detector: p.detector, wavelength: p.wavelength_um,
        group: seriesOf.get(p.obs_id) || `Not in a comparable series (D${p.detector ?? "?"}, ${number(p.wavelength_um, 3)} µm)`,
        inSeries: seriesOf.has(p.obs_id), cls: p.usable === false ? "excluded" : "observed", reasons: p.reasons };
      (p.usable === false ? excluded : points).push(mark);
    }
    for (const u of list(v.upper_limits).filter(keep)) {
      const y = u.upper_limit_ujy ?? u.limit_ujy;
      if (finite(u.mjd) && finite(y)) points.push({ x: u.mjd, y, id: u.obs_id, cls: "limit", group: "Upper limits (censored)", reasons: u.reasons });
    }
    const series = list(v.series).map((s) => ({ id: s.series_id, detector: s.detector, wavelength: s.wavelength_um, status: s.comparability?.status, test: s.test?.status }));
    return { kind, points, excluded, omitted, series, x: "Modified Julian Date", y: "Flux density (µJy)", line: "series",
      note: "Only points inside one comparable series (same detector and release, wavelength within tolerance) are connected. Different wavelengths are spectral structure, not variability." };
  }
  return { kind: "observations" };
}

const PALETTE = ["#167c70", "#a9641f", "#3d6fb5", "#9a4f8e", "#5b7d1f", "#b2463f", "#4b5563"];

function extent(values) {
  const v = values.filter(finite);
  let lo = Math.min(...v), hi = Math.max(...v);
  if (!v.length) return [0, 1];
  if (lo === hi) { const pad = Math.abs(lo) * .05 || 1; return [lo - pad, hi + pad]; }
  const pad = (hi - lo) * .08;
  return [lo - pad, hi + pad];
}

function ticks(lo, hi, n = 5) {
  const step = Math.pow(10, Math.floor(Math.log10((hi - lo) / n)));
  const nice = [1, 2, 5, 10].map((m) => m * step).find((s) => (hi - lo) / s <= n) || step * 10;
  const out = [];
  for (let t = Math.ceil(lo / nice) * nice; t <= hi + 1e-12; t += nice) out.push(Number(t.toPrecision(12)));
  return out;
}

function tableFor(model) {
  const rows = [...model.points, ...list(model.excluded)].sort((a, b) => a.x - b.x);
  return table([model.x, model.y, "±1σ", "Group", "Observation", "State"], rows.map((p) => [number(p.x, 5), number(p.y, 4), finite(p.sigma) ? number(p.sigma, 4) : "—",
    h(p.group), `<button type="button" class="text-button" data-action="observation" data-id="${h(p.id)}">${h(p.id)}</button>`,
    h(p.cls === "excluded" ? `Excluded: ${list(p.reasons).join(", ") || "not usable"}` : p.cls === "limit" ? "Upper limit" : p.cls === "reference" ? "Expected" : "Usable")]));
}

export function renderChart(root, subtitle, state, matches) {
  const kind = state.chart;
  if (kind === "observations") {
    const rows = list(state.doc?.measurements || state.doc?.observations || state.coverage?.observations);
    const shown = rows.filter(matches);
    subtitle.textContent = `${shown.length} of ${rows.length} observation records match the current filters.`;
    root.innerHTML = table(["Observation", "Detector", "MJD", "λ (µm)", "State"], shown.map((o) => [
      `<button class="text-button" type="button" data-action="observation" data-id="${h(o.obs_id)}">${h(o.obs_id)}</button>`,
      h(o.detector ?? o.meta?.detector ?? "—"), number(o.mjd ?? o.meta?.mjd_avg ?? o.t_min_mjd, 5), number(o.wavelength_um, 4),
      h(o.status ?? (o.included == null ? "Indexed" : o.included ? "Included" : "Excluded"))]));
    return;
  }
  const model = chartModel(state, matches);
  const omittedText = [model.omitted.filtered ? `${model.omitted.filtered} hidden by filters` : "", model.omitted.nonfinite ? `${model.omitted.nonfinite} without a finite value` : "",
    list(model.excluded).length ? `${model.excluded.length} excluded (hollow, with reasons)` : ""].filter(Boolean).join(" · ");
  subtitle.textContent = model.note;
  const plotted = [...model.points, ...list(model.excluded)];
  if (!plotted.length) {
    root.innerHTML = empty(state.doc?.run_id && kind === "spectrum" ? "Select a field source on the map to view its spectrum."
      : state.doc?.run_id ? "Field runs record per-source measurements: select a source." : `No ${kind} measurements in this record${omittedText ? ` (${omittedText})` : ""}.`)
      + `<button class="text-button" type="button" data-action="chart" data-id="observations">See observation records</button>`;
    return;
  }
  if (state.tableView) { root.innerHTML = `<p class="compact">${h(omittedText)}</p>${tableFor(model)}${list(model.excludedNotes).length ? `<p class="compact">Excluded from the motion fit: ${h(model.excludedNotes.join("; "))}</p>` : ""}`; return; }
  const width = Math.max(300, root.clientWidth - 24), height = 230;
  const plot = { left: width < 460 ? 54 : 70, right: width - 14, top: 12, bottom: 182 };
  const [x0, x1] = extent([...plotted.flatMap((p) => [p.x - (p.xerr || 0), p.x + (p.xerr || 0)]), ...(kind === "spectrum" ? list(model.coverage).flatMap((c) => c.state !== "MEASURED" ? [] : []) : [])]);
  const [y0, y1] = extent(plotted.flatMap((p) => [p.y - (p.sigma || 0), p.y + (p.sigma || 0)]));
  const px = (x) => plot.left + (x - x0) / (x1 - x0) * (plot.right - plot.left);
  const py = (y) => plot.bottom - (y - y0) / (y1 - y0) * (plot.bottom - plot.top);
  const groups = [...new Set(plotted.map((p) => p.group))];
  const colour = (g = "") => g === "Catalogue expectation" ? "#a9641f" : g.startsWith("Upper") ? "#78538f" : PALETTE[groups.indexOf(g) % PALETTE.length];
  let under = "";
  if (kind === "spectrum") {
    for (const [det, [lo, hi]] of Object.entries(DETECTOR_RANGE_UM)) {
      if (hi < x0 || lo > x1) continue;
      const cov = list(model.coverage).find((c) => Number(c.detector) === Number(det));
      const a = px(Math.max(lo, x0)), b = px(Math.min(hi, x1));
      if (!cov || cov.state !== "MEASURED") under += `<rect class="nocov" x="${a}" y="${plot.top}" width="${Math.max(0, b - a)}" height="${plot.bottom - plot.top}"><title>Detector ${det}: ${h(cov?.state || "no coverage")}</title></rect>`;
      under += `<path class="det-edge" d="M ${px(Math.max(lo, x0))} ${plot.top} V ${plot.bottom}"/><text class="det-label" x="${a + 3}" y="${plot.top + 10}">D${det}</text>`;
    }
  }
  const xt = ticks(x0, x1), yt = ticks(y0, y1);
  const grid = yt.map((t) => `<path class="grid" d="M ${plot.left} ${py(t)} H ${plot.right}"/><text x="${plot.left - 6}" y="${py(t) + 4}" text-anchor="end">${number(t, 3)}</text>`).join("")
    + xt.map((t) => `<path class="grid" d="M ${px(t)} ${plot.top} V ${plot.bottom}"/><text x="${px(t)}" y="${plot.bottom + 16}" text-anchor="middle">${number(t, kind === "spectrum" ? 2 : 1)}</text>`).join("")
    + (y0 < 0 && y1 > 0 ? `<path class="zero" d="M ${plot.left} ${py(0)} H ${plot.right}"/>` : "");
  let lines = "";
  if (model.line) {
    for (const g of groups) {
      if (model.line === "series" && !plotted.some((p) => p.group === g && p.inSeries)) continue;
      if (model.line === "reference" && g !== "Catalogue expectation") continue;
      const pts = model.points.filter((p) => p.group === g && p.cls !== "limit").sort((a, b) => a.x - b.x);
      if (pts.length > 1) lines += `<polyline fill="none" stroke="${colour(g)}" stroke-width="1.2" ${g === "Catalogue expectation" ? 'stroke-dasharray="4 4"' : ""} points="${pts.map((p) => `${px(p.x)},${py(p.y)}`).join(" ")}"/>`;
    }
  }
  const active = new Set([state.selection?.id, ...(state.frameIds || [])].filter(Boolean));
  const marks = plotted.map((p) => {
    const x = px(p.x), y = py(p.y), c = colour(p.group);
    const title = `${p.id} · ${p.group} · ${number(p.x, 5)}, ${number(p.y, 4)}${finite(p.sigma) ? ` ± ${number(p.sigma, 4)}` : ""}${p.cls === "excluded" ? ` · excluded: ${list(p.reasons).join(", ")}` : ""}`;
    const bars = (finite(p.sigma) ? `<path stroke="${c}" d="M ${x} ${py(p.y - p.sigma)} V ${py(p.y + p.sigma)}"/>` : "") + (finite(p.xerr) ? `<path stroke="${c}" d="M ${px(p.x - p.xerr)} ${y} H ${px(p.x + p.xerr)}"/>` : "");
    const r = active.has(p.id) ? 6.5 : 4.2;
    const shape = p.cls === "limit" ? `<path d="M ${x - 5} ${y - 4} L ${x} ${y + 4} L ${x + 5} ${y - 4} Z" fill="none" stroke="${c}"/>`
      : p.cls === "reference" ? `<rect x="${x - 3.5}" y="${y - 3.5}" width="7" height="7" fill="none" stroke="${c}"/>`
      : `<circle cx="${x}" cy="${y}" r="${r}" fill="${p.cls === "excluded" ? "#fff" : c}" stroke="${c}" stroke-width="${active.has(p.id) ? 2.5 : 1.2}"/>`;
    return `${bars}<g tabindex="${p.cls === "reference" ? -1 : 0}" role="button" aria-label="${h(title)}" class="chart-point ${p.cls}" data-action="observation" data-id="${h(p.id)}"><title>${h(title)}</title>${shape}</g>`;
  }).join("");
  root.innerHTML = `<div class="chart-legend">${groups.map((g) => `<span><i class="legend-dot" data-c="${h(colour(g))}"></i>${h(g)}</span>`).join("")}
      ${kind === "motion" ? `<button type="button" class="text-button" data-action="axis" data-id="${state.axis ? 0 : 1}">Show ${state.axis ? "east" : "north"} offset</button>` : ""}</div>
    <svg class="chart" viewBox="0 0 ${width} ${height}" role="group" aria-label="${h(model.y)} versus ${h(model.x)}; ${plotted.length} points. A table view is available.">${under}${grid}${lines}${marks}
      <text x="${(plot.left + plot.right) / 2}" y="${height - 8}" text-anchor="middle">${h(model.x)}</text>
      <text transform="translate(12 ${(plot.top + plot.bottom) / 2}) rotate(-90)" text-anchor="middle">${h(model.y)}</text></svg>
    ${omittedText ? `<p class="compact">${h(omittedText)}</p>` : ""}
    ${list(model.excludedNotes).length ? `<p class="compact">Excluded from the motion fit (not plotted): ${h(model.excludedNotes.join("; "))}</p>` : ""}
    ${kind === "flux" && list(model.series).length ? `<p class="compact">Comparable series: ${model.series.map((s) => `${h(s.id)} (D${s.detector}, ${number(s.wavelength, 3)} µm${s.status ? `, ${h(s.status)}` : ""}, test ${h(s.test || "—")})`).join("; ")}</p>` : ""}
    ${list(model.points).some((p) => p.wsource && p.wsource !== "CWAVE_CBAND") ? `<p class="compact">Some wavelengths are not the calibrated CWAVE/CBAND values (see the table's source column).</p>` : ""}`;
  for (const dot of root.querySelectorAll(".legend-dot[data-c]")) dot.style.background = dot.dataset.c; // CSSOM (CSP-safe)
}
