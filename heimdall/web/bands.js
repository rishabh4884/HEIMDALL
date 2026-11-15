// The 102 SPHEREx DR1 cube planes, from the published HiPS metadata (pure; tested under node).
// IRSA properties: "frame index = 17*(band-1) + (channel-1). Frames 50 and 51 (band 3 channel 17, band 4 channel 1,
// the dichroic channels) are blank." Band = detector. Wavelengths are the cube's nominal central values
// (data_cube_crval3_arr); calibrated per-pixel wavelengths of target measurements come from CWAVE/WCS-WAVE instead.

export const PLANES = 102;
export const CHANNELS = 17;
export const BLANK = new Set([51, 52]); // one-based
export const DETECTOR_RANGE_UM = { 1: [0.75, 1.09], 2: [1.1, 1.62], 3: [1.63, 2.41], 4: [2.42, 3.82], 5: [3.83, 4.41], 6: [4.42, 5.0] };

export function planeInfo(plane, wavelengths = []) {
  const index = plane - 1;
  const detector = Math.floor(index / CHANNELS) + 1;
  const channel = (index % CHANNELS) + 1;
  const wavelength = wavelengths.length === PLANES ? wavelengths[index] : null;
  return { plane, detector, channel, wavelength, blank: BLANK.has(plane),
    provenance: wavelength == null ? "Nominal wavelength unavailable (cube metadata not loaded)" : "Nominal cube central wavelength (HiPS data_cube_crval3_arr)" };
}

export function allPlanes(wavelengths = []) {
  return Array.from({ length: PLANES }, (_, i) => planeInfo(i + 1, wavelengths));
}

/** Planes whose nominal wavelength lies in [lo, hi] (inclusive); blank planes keep their place but are flagged. */
export function planesInRange(wavelengths, lo, hi) {
  return allPlanes(wavelengths).filter((p) => p.wavelength != null && p.wavelength >= lo && p.wavelength <= hi).map((p) => p.plane);
}

/**
 * Measurement marks for the strip: each saved measurement at its own calibrated wavelength and detector.
 * They are deliberately not snapped to a cube plane: a plane is survey imagery, a mark is a target measurement.
 */
export function measurementMarks(measurements = []) {
  return measurements.filter((m) => Number.isFinite(m?.wavelength_um) && Number.isInteger(m?.detector)).map((m) => ({
    obs_id: m.obs_id, detector: m.detector, wavelength: m.wavelength_um, bandwidth: Number.isFinite(m.bandwidth_um) ? m.bandwidth_um : null,
    usable: m.status === "MEASURED" || m.usable === true, source: m.wavelength_source || "measurement record",
  }));
}

/** Position (0..1) of a wavelength on the merged strip, linear in wavelength between the first and last plane. */
export function stripPosition(wavelength, wavelengths) {
  if (wavelengths.length !== PLANES || !Number.isFinite(wavelength)) return null;
  const lo = wavelengths[0], hi = wavelengths[PLANES - 1];
  return Math.min(1, Math.max(0, (wavelength - lo) / (hi - lo)));
}

/** Position (0..1) inside a detector lane, using the detector's nominal range. */
export function lanePosition(wavelength, detector) {
  const r = DETECTOR_RANGE_UM[detector];
  if (!r || !Number.isFinite(wavelength)) return null;
  return Math.min(1, Math.max(0, (wavelength - r[0]) / (r[1] - r[0])));
}

/**
 * Fractional plane index (0-based, within the detector's 17 channels) of a calibrated wavelength, by linear
 * interpolation between the detector's nominal plane wavelengths, so marks line up with the plane cells.
 * Returns null outside the detector's nominal planes (shown as off-strip rather than guessed).
 */
export function fractionalChannel(wavelength, detector, wavelengths) {
  if (wavelengths.length !== PLANES || !Number.isFinite(wavelength) || !(detector >= 1 && detector <= 6)) return null;
  const base = (detector - 1) * CHANNELS;
  const w = Array.from({ length: CHANNELS }, (_, i) => wavelengths[base + i]);
  const half0 = (w[1] - w[0]) / 2, half1 = (w[CHANNELS - 1] - w[CHANNELS - 2]) / 2;
  if (wavelength < w[0] - half0 || wavelength > w[CHANNELS - 1] + half1) return null;
  if (wavelength <= w[0]) return (wavelength - w[0]) / (2 * half0);
  for (let i = 0; i < CHANNELS - 1; i++) if (wavelength <= w[i + 1]) return i + (wavelength - w[i]) / (w[i + 1] - w[i]);
  return CHANNELS - 1 + (wavelength - w[CHANNELS - 1]) / (2 * half1);
}
