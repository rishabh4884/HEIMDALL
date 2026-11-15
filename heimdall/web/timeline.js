// Real-epoch time model for the ribbon and playback (pure; tested under node). No intermediate measurement is ever
// interpolated: playback only visits recorded exposures. SPHEREx images all six detectors simultaneously, so one
// exposure time ("epoch") can hold several observations at different wavelengths.

const SAME_EXPOSURE_DAYS = 1e-4; // ~9 s: observations closer than this are one simultaneous exposure

export function mjdToDate(mjd) {
  return Number.isFinite(mjd) ? new Date((mjd - 40587) * 86400000) : null;
}

export function formatEpoch(mjd) {
  const d = mjdToDate(mjd);
  return d ? `MJD ${mjd.toFixed(4)} · ${d.toISOString().slice(0, 16).replace("T", " ")} UTC` : "—";
}

/**
 * Build epochs from observation-like rows {obs_id, mjd, detector?, wavelength_um?, usable?, reasons?}.
 * Returns ascending epochs with their observations, excluded ones kept and labelled.
 */
export function buildEpochs(rows) {
  const valid = rows.filter((r) => r && Number.isFinite(r.mjd) && r.obs_id).sort((a, b) => a.mjd - b.mjd || String(a.obs_id).localeCompare(String(b.obs_id)));
  const epochs = [];
  for (const r of valid) {
    const last = epochs[epochs.length - 1];
    if (last && r.mjd - last.mjd <= SAME_EXPOSURE_DAYS) last.observations.push(r);
    else epochs.push({ mjd: r.mjd, observations: [r] });
  }
  epochs.forEach((e, i) => {
    e.index = i;
    e.id = e.observations[0].obs_id;
    e.excluded = e.observations.every((o) => o.usable === false);
  });
  return epochs;
}

/** Ribbon positions (0..1): proportional to elapsed time, or equally spaced frames. */
export function positions(epochs, mode = "elapsed") {
  if (!epochs.length) return [];
  if (epochs.length === 1) return [0.5];
  if (mode === "equal") return epochs.map((_, i) => i / (epochs.length - 1));
  const t0 = epochs[0].mjd, span = epochs[epochs.length - 1].mjd - t0 || 1;
  return epochs.map((e) => (e.mjd - t0) / span);
}

/** Gaps worth marking: intervals much longer than the median interval (and at least a day). */
export function gaps(epochs, factor = 4) {
  if (epochs.length < 3) return [];
  const d = epochs.slice(1).map((e, i) => e.mjd - epochs[i].mjd);
  const median = [...d].sort((a, b) => a - b)[Math.floor(d.length / 2)];
  return d.flatMap((days, i) => days >= Math.max(1, factor * median) ? [{ after: i, days }] : []);
}

/**
 * Dwell (ms) before advancing from epoch i during playback. Equal stepping: a fixed beat. Elapsed mode: the
 * wait scales with the real interval to the next epoch (whole sequence ~12 s at 1x), clamped so it stays watchable.
 */
export function dwell(epochs, i, mode, speed = 1) {
  const beat = 1000 / speed;
  if (mode !== "elapsed" || i >= epochs.length - 1) return beat;
  const span = epochs[epochs.length - 1].mjd - epochs[0].mjd;
  if (!(span > 0)) return beat;
  const share = (epochs[i + 1].mjd - epochs[i].mjd) / span;
  return Math.max(250, Math.min(4000, 12000 * share)) / speed;
}
