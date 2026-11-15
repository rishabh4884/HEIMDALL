import { empty, finite, h, list, number, svg, table } from "./records.js";

export function chartData(state, matches) {
  const analyses = state.doc?.analyses || {};
  const motion = list(analyses.motion?.track).filter(matches);
  const photometry = list(analyses.variability?.light_curve).filter(matches);
  const upperLimits = list(analyses.variability?.upper_limits).filter(matches);
  const observations = list(state.doc?.measurements || state.doc?.observations || state.coverage?.observations).filter(matches);
  const source = list(state.doc?.sources).find((item) => item.source_id === state.selection?.id);
  const sourceSpectrum = list(source?.spectrum?.points).filter(matches);
  const memberIds = new Set(list(source?.det_ids));
  const detections = source ? list(state.doc?.detections).filter((d) => memberIds.has(d.det_id) && matches(d)) : [];
  return { motion, photometry, upperLimits, observations, detections, sourceSpectrum, sourceCoverage: list(source?.spectrum?.coverage) };
}

function series(kind, d, axis) {
  if (kind === "motion") {
    const points = d.motion.map((m) => ({ x: m.mjd, y: m.measured_offset_arcsec?.[axis], sigma: m.sigma_arcsec?.[axis], id: m.obs_id, cls: "observed" }));
    const reference = d.motion.map((m) => ({ x: m.mjd, y: m.expected_offset_arcsec?.[axis], id: m.obs_id, cls: "reference" }));
    return { points: [...points, ...reference], x: "Modified Julian Date", y: `${axis ? "North" : "East"} offset (arcsec)`,
      note: "Measured centroid offsets and catalogue expectation. Bars show recorded uncertainties.",
      legend: [["observed", "Measured"], ["reference", "Catalogue expectation"]] };
  }
  const spectrum = kind === "spectrum";
  const fromSavedSpectrum = d.sourceSpectrum.some((p) => p.usable === true);
  const usable = d.photometry.filter((p) => p.usable === true);
  const detected = d.detections.filter((p) => p.flux_ujy != null);
  const set = usable.length ? usable : fromSavedSpectrum ? d.sourceSpectrum.filter((p) => p.usable === true) : detected;
  const points = set.map((p) => ({ x: spectrum ? p.wavelength_um : p.mjd, y: p.flux_ujy, sigma: p.flux_sigma_ujy, id: p.obs_id, cls: "observed" }));
  for (const p of d.upperLimits) {
    points.push({ x: spectrum ? p.wavelength_um : p.mjd, y: p.upper_limit_ujy ?? p.limit_ujy, id: p.obs_id, cls: "limit" });
  }
  return { points, x: spectrum ? "Wavelength (μm)" : "Modified Julian Date", y: "Flux (μJy)",
    note: spectrum ? "Individual fluxes at measured wavelengths. Points from different observations may be non-simultaneous."
      : "Individual fluxes. Check comparability before interpreting differences over time.",
    legend: [["observed", fromSavedSpectrum ? "Usable source measurements" : detected.length ? "Source flux" : "Forced flux"], ["limit", "Upper limit"]] };
}

export function renderChart(root, subtitle, state, matches) {
  const d = chartData(state, matches);
  const kind = state.chart;
  if (kind === "observations") {
    subtitle.textContent = "Saved and indexed observation records matching the current filters.";
    root.innerHTML = table(["Observation", "Detector", "MJD", "State"], d.observations.map((o) => [
      `<button class="text-button" data-action="observation" data-id="${h(o.obs_id)}">${h(o.obs_id)}</button>`,
      h(o.detector ?? o.meta?.detector ?? "—"), number(o.mjd ?? o.meta?.mjd_avg ?? o.t_min_mjd, 4),
      h(o.status ?? (o.included == null ? "Indexed" : o.included ? "Included" : "Excluded")),
    ]));
    return;
  }
  const s = series(kind, d, state.axis);
  subtitle.textContent = s.note;
  const points = s.points.filter((p) => finite(p.x) && finite(p.y));
  if (!points.length) {
    root.innerHTML = empty(state.doc?.run_id && kind !== "motion" ? "Select a field source on the map to view its flux measurements." : "No plottable measurements match this view.") +
      `<button class="text-button" data-action="chart" data-id="observations">See observation records ${svg("arrow")}</button>`;
    return;
  }
  const width = Math.max(280, root.clientWidth - 24), height = 170;
  const plot = { left: width < 430 ? 48 : 65, right: width - 14, top: 10, bottom: 124 };
  const extent = (vals) => {
    const low = Math.min(...vals), high = Math.max(...vals);
    const pad = (high - low) * .09 || Math.abs(low) * .001 || 1;
    return [low - pad, high + pad];
  };
  const [x0, x1] = extent(points.map((p) => p.x));
  const [y0, y1] = extent(points.flatMap((p) => [p.y - (p.sigma || 0), p.y + (p.sigma || 0)]));
  const px = (x) => plot.left + (x - x0) / (x1 - x0) * (plot.right - plot.left);
  const py = (y) => plot.bottom - (y - y0) / (y1 - y0) * (plot.bottom - plot.top);
  const grid = [0, .25, .5, .75, 1].map((f) => {
    const x = px(x0 + f * (x1 - x0)), y = py(y0 + f * (y1 - y0));
    return `<path class="grid" d="M ${plot.left} ${y} H ${plot.right} M ${x} ${plot.top} V ${plot.bottom}"/>
      <text x="${plot.left - 6}" y="${y + 4}" text-anchor="end">${number(y0 + f * (y1 - y0), 2)}</text>
      <text x="${x}" y="${plot.bottom + 16}" text-anchor="middle">${number(x0 + f * (x1 - x0), s.x.startsWith("Modified") ? (x1 - x0 > 10 ? 0 : 2) : 3)}</text>`;
  }).join("");
  const reference = points.filter((p) => p.cls === "reference").sort((a, b) => a.x - b.x);
  const curve = reference.length > 1
    ? `<polyline class="reference" points="${reference.map((p) => `${px(p.x)},${py(p.y)}`).join(" ")}" fill-opacity="0" stroke-dasharray="4 4"/>` : "";
  const marks = points.map((p) => {
    const x = px(p.x), y = py(p.y);
    const title = `${p.id} · ${number(p.x, 5)}, ${number(p.y, 4)}${finite(p.sigma) ? ` ± ${number(p.sigma, 4)}` : ""}`;
    const size = state.selection?.id === p.id || state.frame === p.id ? 6 : 4;
    const bars = finite(p.sigma) ? `<path class="${p.cls}" d="M ${x} ${py(p.y - p.sigma)} V ${py(p.y + p.sigma)}"/>` : "";
    const mark = p.cls === "limit" ? `<path d="M ${x - 4} ${y - 3} l 4 6 4 -6"/>` : `<circle cx="${x}" cy="${y}" r="${size}"/>`;
    return `${bars}<g tabindex="${p.cls === "reference" ? "-1" : "0"}" role="button" aria-label="${h(title)}" class="chart-point ${p.cls}" data-action="observation" data-id="${h(p.id)}"><title>${h(title)}</title>${mark}</g>`;
  }).join("");
  root.innerHTML = `<div class="chart-legend">${s.legend.filter(([cls]) => points.some((p) => p.cls === cls)).map(([cls, text]) => `<span><i class="legend-dot ${cls}"></i>${h(text)}</span>`).join("")}
    <span>${h(s.y)}</span>${kind === "motion" ? `<button type="button" class="text-button" data-action="axis" data-id="${state.axis ? 0 : 1}">Show ${state.axis ? "east" : "north"} offset</button>` : ""}</div>
    <svg class="chart" viewBox="0 0 ${width} ${height}" role="group" aria-label="${h(s.y)} versus ${h(s.x)}">${grid}${curve}${marks}
      <text x="${(plot.left + plot.right) / 2}" y="${height - 5}" text-anchor="middle">${h(s.x)}</text></svg>
    ${kind === "spectrum" && d.sourceCoverage.length ? `<p class="compact">Detector coverage · ${d.sourceCoverage.map((c) => `D${number(c.detector, 0)}: ${number(c.n_usable, 0)} usable`).join(" · ")}</p>` : ""}`;
}
