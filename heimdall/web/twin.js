// Twin observation views from GET /evidence/pair: both images are resampled by the server onto one north-up grid.
// Modes: side by side (camera linked or independent), blink, and difference (only when the server marks the pair comparable).
import { badge, empty, facts, finite, h, label, list, number } from "./records.js";

const reducedMotion = () => globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

export class TwinView {
  constructor(root, client, announce) {
    Object.assign(this, { root, client, announce });
    this.opts = { mode: "side", linked: true, stretch: "own", blinkMs: 700, mask: false };
    this.cams = [{ zoom: 1, x: 0, y: 0 }, { zoom: 1, x: 0, y: 0 }];
    this.generation = 0;
    root.addEventListener("click", (e) => this.onClick(e));
    root.addEventListener("change", (e) => this.onChange(e));
    document.addEventListener("visibilitychange", () => { if (document.hidden) this.stopBlink(); });
  }

  /** candidates: [{obs_id, sha256, detector, mjd, wavelength_um, series}] ; centre {ra, dec}; investigation id or null. */
  setContext({ candidates, centre, investigation, synthetic, pair }) {
    this.ctx = { candidates: list(candidates).filter((c) => c.sha256), centre, investigation, synthetic };
    const ids = this.ctx.candidates.map((c) => c.obs_id);
    let a = pair?.a, b = pair?.b;
    if (!ids.includes(a) || !ids.includes(b) || a === b) {
      const bySeries = new Map();
      for (const c of this.ctx.candidates) if (c.series) bySeries.set(c.series, [...(bySeries.get(c.series) || []), c.obs_id]);
      const same = [...bySeries.values()].find((v) => v.length >= 2);
      [a, b] = same ? [same[0], same[same.length - 1]] : ids.slice(0, 2);
    }
    this.pair = { a, b };
    this.data = null;
    this.error = null;
    this.render();
    if (this.visible) this.load();
  }

  show(visible) {
    this.visible = visible;
    if (visible && !this.data && !this.error && this.pair?.a && this.pair?.b) this.load();
    if (!visible) this.stopBlink();
  }

  async load() {
    const { a, b } = this.pair || {};
    const ca = this.ctx.candidates.find((c) => c.obs_id === a), cb = this.ctx.candidates.find((c) => c.obs_id === b);
    if (!ca || !cb || !this.ctx.centre) return;
    const gen = ++this.generation;
    this.loading = true;
    this.render();
    try {
      const data = await this.client.get("/evidence/pair", { params: { a: ca.sha256, b: cb.sha256, ra: this.ctx.centre.ra, dec: this.ctx.centre.dec,
        investigation: this.ctx.investigation || undefined, npix: 96, size_arcsec: 240 } });
      if (gen !== this.generation) return;
      this.data = data;
      this.error = null;
      if (this.opts.mode.startsWith("difference") && !data.difference) this.opts.mode = "side";
      this.announce?.(`Images aligned: ${data.comparability.status.replaceAll("_", " ").toLowerCase()}`);
    } catch (e) {
      if (gen !== this.generation || e.kind === "aborted") return;
      this.error = e;
      this.data = null;
    } finally {
      if (gen === this.generation) { this.loading = false; this.render(); }
    }
  }

  onChange(e) {
    if (e.target.name === "twin-a" || e.target.name === "twin-b") {
      this.pair = { ...this.pair, [e.target.name.slice(-1)]: e.target.value };
      this.onPair?.(this.pair);
      this.data = null;
      this.load();
    } else if (e.target.name === "twin-stretch") { this.opts.stretch = e.target.value; this.draw(); }
    else if (e.target.name === "twin-mask") { this.opts.mask = e.target.checked; this.draw(); }
    else if (e.target.name === "twin-linked") { this.opts.linked = e.target.checked; if (this.opts.linked) this.cams[1] = { ...this.cams[0] }; this.applyCams(); }
  }

  onClick(e) {
    const b = e.target.closest("[data-twin]");
    if (!b) return;
    const act = b.dataset.twin;
    if (["side", "blink", "difference"].includes(act)) {
      if (act === "difference" && !this.data?.difference) return;
      if (act === "difference" && !this.acknowledged) { this.opts.mode = "difference-ack"; this.stopBlink(); this.render(); return; }
      this.opts.mode = act;
      this.stopBlink();
      this.render();
    } else if (act === "ack") { this.acknowledged = true; this.opts.mode = "difference"; this.render(); this.announce?.("Display subtraction shown; it is not evidence of change."); }
    else if (act === "swap") { this.pair = { a: this.pair.b, b: this.pair.a }; this.onPair?.(this.pair); this.data = null; this.load(); }
    else if (act === "blink-toggle") { this.blinkTimer ? this.stopBlink() : this.startBlink(); this.renderBlinkButton(); }
    else if (act === "blink-step") { this.blinkFrame = 1 - (this.blinkFrame || 0); this.draw(); }
    else if (act === "zoom-in" || act === "zoom-out" || act === "reset") {
      const i = Number(b.dataset.view || 0);
      const cam = this.cams[i];
      if (act === "reset") Object.assign(cam, { zoom: 1, x: 0, y: 0 }); else cam.zoom = Math.max(1, Math.min(8, cam.zoom * (act === "zoom-in" ? 1.5 : 1 / 1.5)));
      if (this.opts.linked) this.cams[1 - i] = { ...cam };
      this.applyCams();
    }
  }

  startBlink() {
    if (reducedMotion()) { this.announce?.("Reduced motion: use Show other image to step manually."); return; }
    this.stopBlink();
    this.blinkTimer = setInterval(() => { this.blinkFrame = 1 - (this.blinkFrame || 0); this.draw(); }, this.opts.blinkMs);
  }

  stopBlink() { clearInterval(this.blinkTimer); this.blinkTimer = null; }

  renderBlinkButton() {
    const btn = this.root.querySelector('[data-twin="blink-toggle"]');
    if (btn) { btn.textContent = this.blinkTimer ? "Pause blink" : "Start blink"; btn.setAttribute("aria-pressed", Boolean(this.blinkTimer)); }
  }

  render() {
    const focused = this.root.contains(document.activeElement) ? (document.activeElement.name || document.activeElement.dataset?.twin) : null;
    this.renderInner();
    if (focused) this.root.querySelector(`[name="${focused}"], [data-twin="${focused}"]`)?.focus();
  }

  renderInner() {
    const c = this.ctx;
    if (!c) { this.root.innerHTML = empty("Open an investigation with cached products to compare its observation images."); return; }
    if (c.candidates.length < 2) { this.root.innerHTML = empty("At least two observations with cached, checksum-identified products are needed for a twin view."); return; }
    const option = (o, sel) => `<option value="${h(o.obs_id)}" ${o.obs_id === sel ? "selected" : ""}>${h(o.obs_id)} · D${h(o.detector ?? "?")} · MJD ${number(o.mjd, 3)} · ${number(o.wavelength_um, 3)} µm${o.series ? ` · ${h(o.series)}` : ""}</option>`;
    const d = this.data;
    const cmp = d?.comparability;
    const diffOk = Boolean(d?.difference);
    this.root.innerHTML = `${c.synthetic || d?.images?.some((i) => i.synthetic) ? `<p class="synthetic-flag">SYNTHETIC · NOT REAL DATA</p>` : ""}
      <div class="twin-controls">
        <label>Image A <select name="twin-a">${c.candidates.map((o) => option(o, this.pair.a)).join("")}</select></label>
        <button type="button" class="quiet" data-twin="swap" aria-label="Swap images A and B">⇄</button>
        <label>Image B <select name="twin-b">${c.candidates.map((o) => option(o, this.pair.b)).join("")}</select></label>
      </div>
      <div class="twin-controls">
        <div class="segmented" role="group" aria-label="Comparison mode">
          <button type="button" data-twin="side" aria-pressed="${this.opts.mode === "side"}">Side by side</button>
          <button type="button" data-twin="blink" aria-pressed="${this.opts.mode === "blink"}">Blink</button>
          <button type="button" data-twin="difference" aria-pressed="${this.opts.mode.startsWith("difference")}" ${diffOk ? "" : `disabled aria-describedby="twin-diff-why"`}>Difference (display only)</button>
        </div>
        <label>Stretch <select name="twin-stretch"><option value="own" ${this.opts.stretch === "own" ? "selected" : ""}>Per image</option><option value="common" ${this.opts.stretch === "common" ? "selected" : ""}>Common to both</option></select></label>
        <label class="inline"><input type="checkbox" name="twin-linked" ${this.opts.linked ? "checked" : ""}> Link cameras</label>
        <label class="inline"><input type="checkbox" name="twin-mask" ${this.opts.mask ? "checked" : ""}> Show pixels excluded from photometry</label>
      </div>
      ${this.loading ? empty("Aligning the two cached products on the server…") : ""}
      ${this.error ? empty(this.error.status === 404 ? "Both exact products must be cached locally (checksum-matched) to align them. Nothing is fetched for a preview." : `Alignment failed: ${this.error.message}`, true) : ""}
      ${d ? `<div class="twin-status">${badge(cmp.status, cmp.status === "NOT_COMPARABLE" ? "warn" : "good")}
          ${cmp.reasons.map((r) => `<p class="compact">${h(r)}</p>`).join("")}
          ${!diffOk ? `<p class="compact" id="twin-diff-why">Difference disabled: ${h(cmp.reasons.join("; ") || "pair not comparable")}. Side-by-side and blink remain display comparisons only.</p>` : ""}
          ${cmp.limitations.length ? `<p class="compact">Limitations: ${h(cmp.limitations.join("; "))}</p>` : ""}</div>
        ${this.opts.mode.startsWith("difference") && d.difference ? `<div class="diff-disclosure" role="note" aria-labelledby="diff-disc-title"><strong id="diff-disc-title">Not PSF-matched · display only · not evidence of change</strong>
          <p>${h(d.difference.disclosure || "Visual subtraction without PSF matching; not evidence of astrophysical change.")}</p>
          ${this.opts.mode === "difference-ack" ? `<button type="button" class="quiet" data-twin="ack">I understand: show the display subtraction</button>` : ""}</div>` : ""}
        ${this.opts.mode === "difference-ack" ? "" : `<div class="twin-views" data-mode="${this.opts.mode}">${this.opts.mode === "side" ? [0, 1].map((i) => this.viewHtml(i)).join("") : this.viewHtml(0)}</div>`}
        ${this.opts.mode === "blink" ? `<div class="twin-controls"><button type="button" class="quiet" data-twin="blink-toggle" aria-pressed="false">Start blink</button><button type="button" class="quiet" data-twin="blink-step">Show other image</button><output id="blink-label" aria-live="off"></output></div>` : ""}
        ${this.opts.mode === "difference" ? `<p class="compact">${h(d.difference.order)} in ${h(d.difference.units)}, clipped at ±${d.difference.clip_sigma}; grey = masked or no data (${number(100 * d.difference.masked_fraction, 1)}%). Preparation: ${h(d.difference.preparation.join("; "))}. ${h(d.difference.limitations.slice(-1)[0])}</p>` : ""}
        <table class="twin-meta"><thead><tr><th scope="col"></th><th scope="col">A</th><th scope="col">B</th></tr></thead><tbody>
          ${[["Observation", "obs_id"], ["Detector", "detector"], ["MJD", "mjd"], ["λ at position (µm)", "wavelength_um"], ["Bandwidth (µm)", "bandwidth_um"], ["λ source", "wavelength_source"],
            ["Release", "release"], ["Pipeline", "pipeline_version"], ["PSF FWHM (″)", "psf_fwhm_arcsec"], ["Native pixel (″)", "native_pixel_arcsec"], ["Units", "bunit"], ["Coverage of grid", "coverage_fraction"], ["Usable for photometry", "usable_fraction"]]
            .map(([k, f]) => `<tr><th scope="row">${k}</th>${d.images.map((im) => `<td>${h(finite(im[f]) ? number(im[f], 5) : im[f] ?? "—")}</td>`).join("")}</tr>`).join("")}
          <tr><th scope="row">Display stretch (MJy/sr)</th>${d.images.map((im) => `<td>${this.opts.stretch === "common" ? d.common_stretch.map((x) => number(x, 3)).join(" to ") : im.display.own_stretch.map((x) => number(x, 3)).join(" to ")}</td>`).join("")}</tr>
        </tbody></table>
        ${facts([["Grid", `${d.grid.npix}×${d.grid.npix} px · ${d.grid.pixel_arcsec}″/px · ${d.grid.orientation}`], ["Centre", `${number(d.grid.ra_deg, 5)}, ${number(d.grid.dec_deg, 5)}`]])}
        <p class="compact">${h(d.display_note)}. The page brightness slider does not affect these images.</p>` : ""}`;
    this.draw();
    this.applyCams();
  }

  viewHtml(i) {
    const caption = this.opts.mode === "difference" ? "B − A (σ) · display subtraction, not PSF-matched" : this.opts.mode === "blink" ? "Blink" : i ? "B" : "A";
    return `<figure class="twin-view"><div class="twin-frame"><canvas data-view="${i}" width="${this.data.grid.npix}" height="${this.data.grid.npix}" role="img" aria-label="${caption} image, north up, east left"></canvas><span class="twin-compass" aria-hidden="true">N↑ E←</span></div>
      <figcaption>${caption}<span class="twin-zoom"><button type="button" class="text-button" data-twin="zoom-in" data-view="${i}" aria-label="Zoom ${caption} in">＋</button><button type="button" class="text-button" data-twin="zoom-out" data-view="${i}" aria-label="Zoom ${caption} out">－</button><button type="button" class="text-button" data-twin="reset" data-view="${i}">Reset</button></span></figcaption></figure>`;
  }

  draw() {
    const d = this.data;
    if (!d) return;
    const paint = (canvas, b64, diverging, maskB64) => {
      const n = d.grid.npix, bytes = atob(b64);
      if (bytes.length !== n * n) return;
      const ctx = canvas.getContext("2d"), img = ctx.createImageData(n, n);
      const mask = maskB64 && this.opts.mask ? atob(maskB64) : null;
      for (let k = 0; k < bytes.length; k++) {
        const v = bytes.charCodeAt(k);
        let rgb;
        if (v === 0) rgb = [70, 78, 86]; // masked / no data
        else if (diverging) { const t = (v - 128) / 127; rgb = t >= 0 ? [255, Math.round(255 * (1 - t)), Math.round(255 * (1 - t))] : [Math.round(255 * (1 + t)), Math.round(255 * (1 + t)), 255]; }
        else rgb = [v, v, v];
        if (mask && mask.charCodeAt(k)) rgb = [Math.round(rgb[0] * .5 + 120), Math.round(rgb[1] * .5 + 70), Math.round(rgb[2] * .5)];
        img.data.set([...rgb, 255], k * 4);
      }
      ctx.putImageData(img, 0, 0);
    };
    const key = this.opts.stretch === "common" ? "common" : "own";
    for (const canvas of this.root.querySelectorAll("canvas[data-view]")) {
      const i = Number(canvas.dataset.view);
      if (this.opts.mode === "difference") paint(canvas, d.difference.pixels, true);
      else if (this.opts.mode === "blink") {
        const f = this.blinkFrame || 0;
        paint(canvas, d.images[f].display[key], false, d.images[f].display.engine_mask);
        const out = this.root.querySelector("#blink-label");
        if (out) out.textContent = `Showing ${f ? "B" : "A"} · ${d.images[f].obs_id} · MJD ${number(d.images[f].mjd, 3)}`;
      } else paint(canvas, d.images[i].display[key], false, d.images[i].display.engine_mask);
    }
  }

  applyCams() {
    for (const canvas of this.root.querySelectorAll("canvas[data-view]")) {
      const cam = this.cams[Number(canvas.dataset.view)] || this.cams[0];
      canvas.style.transform = `scale(${cam.zoom}) translate(${cam.x}px, ${cam.y}px)`;
    }
  }
}
