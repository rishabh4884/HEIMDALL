import { SurveyImage, atlasPoint, equatorialToGalactic, hammer, projectSky } from "./hips.js";

export const wrapRA = (ra) => ((ra % 360) + 360) % 360;
export const deltaRA = (ra, center) => wrapRA(ra - center + 180) - 180;
const clamp = (x, min, max) => Math.min(max, Math.max(min, x));
const steps = [.00001, .00002, .00005, .0001, .0002, .0005, .001, .002, .005, .01, .02, .05, .1, .2, .5, 1, 2, 5, 10, 15, 30, 45, 60, 90];
const color = { coverage: "#6d9fd0", detections: "#f2c879", sources: "#c2d8ec", candidates: "#efa8d9",
  associations: "#91aac8", tracks: "#6ddcca", catalogue: "#a6b3d6", inventory: "#6ddcca", reference: "#9ee9dd" };
const valid = (x) => typeof x === "number" && Number.isFinite(x);
const RAD = Math.PI / 180;

export function atlasRadii(width, height) {
  const rx = Math.max(0, Math.min((width - 20) / 2, height - 20));
  return { rx, ry: rx / 2 };
}

// The all-sky outline opens into a local view as the projection is zoomed.
// Both imagery and overlays use the same reversible radial transform.
export function openProjection(x, y, amount) {
  const scale = 1 / Math.max(1e-10, 1 - amount * (x * x + y * y));
  return [x * scale, y * scale];
}

export function closeProjection(x, y, amount) {
  const scale = inverseLensScale(x, y, amount);
  return [x * scale, y * scale];
}

const inverseLensScale = (x, y, amount) => 2 / (1 + Math.sqrt(1 + 4 * amount * (x * x + y * y)));

export class SkyMap {
  constructor(canvas, onMove, onPick, tooltip, onImage, onPosition) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onMove = onMove;
    this.onPick = onPick;
    this.tooltip = tooltip;
    this.view = { ra: 266.40499, dec: -28.93617, span: 360 };
    this.onPosition = onPosition;
    this.survey = new SurveyImage(() => {
      this.requestDraw();
    });
    this.onImage = onImage;
    this.data = {};
    this.points = [];
    this.pointers = new Map();
    this.bind();
    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  setView(ra, dec, span) {
    this.survey.movingUntil = performance.now() + 130;
    clearTimeout(this.settleTimer);
    this.settleTimer = setTimeout(() => this.requestDraw(), 150);
    this.view = span >= 360 ? { ra: 266.40499, dec: -28.93617, span: 360 } :
      { ra: wrapRA(ra), dec: clamp(dec, -90, 90), span: clamp(span, .00008, 360) };
    this.onMove(this.view);
    this.requestDraw();
  }

  setData(data, layers, selected) {
    Object.assign(this, { data, layers, selected });
    this.requestDraw();
  }

  requestDraw() {
    if (this.redrawPending) return;
    this.redrawPending = true;
    requestAnimationFrame(() => {
      this.redrawPending = false;
      this.draw();
      this.onImage(this.survey.status());
    });
  }

  geometry() {
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    const all = this.view.span === 360;
    const left = 10, right = w - 10;
    const fit = atlasRadii(right - left, h - 42);
    const rx = fit.rx * 360 / this.view.span, ry = rx / 2;
    const sx = rx * RAD / (2 * Math.SQRT2), sy = sx;
    const [center, tilt] = all ? [0, 0] : equatorialToGalactic(this.view.ra, this.view.dec);
    const amount = 1 - (this.view.span / 360) ** 3;
    return { w, h, sx, sy, left, right, top: 12, bottom: h - 30, cx: (left + right) / 2, cy: (h - 18) / 2, all, rx, ry, center, tilt, amount };
  }

  xy(ra, dec, g = this.geometry()) {
    const [x, y] = openProjection(...projectSky(ra, dec, g.center, g.tilt), g.amount);
    return [g.cx - x * g.rx, g.cy - y * g.ry];
  }

  world(x, y, g = this.geometry()) {
    const px = (g.cx - x) / g.rx, py = (g.cy - y) / g.ry;
    const scale = inverseLensScale(px, py, g.amount);
    return atlasPoint(px * scale, py * scale, g.center, g.tilt);
  }

  pixel(event) {
    const box = this.canvas.getBoundingClientRect();
    return [event.clientX - box.left, event.clientY - box.top];
  }

  zoom(factor, x, y) {
    const span = clamp(this.view.span * factor, .00008, 360);
    const g = this.geometry();
    const fraction = 1 - span / this.view.span;
    const [ra, dec] = (x != null && this.world(g.cx + (x - g.cx) * fraction, g.cy + (y - g.cy) * fraction, g))
      || [this.view.ra, this.view.dec];
    this.setView(ra, dec, span);
  }

  hit(x, y) {
    let best;
    let distance = 11;
    for (const point of this.points) {
      const d = Math.hypot(x - point.x, y - point.y);
      if (d < distance) {
        best = point;
        distance = d;
      }
    }
    return best;
  }

  pan(dx, dy, g) {
    if (g.all) return;
    const next = this.world(g.cx - dx, g.cy - dy, g);
    if (next) this.setView(...next, this.view.span);
  }

  bind() {
    const canvas = this.canvas;
    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      this.zoom(Math.exp(clamp(event.deltaY / 480, -.6, .6)), ...this.pixel(event));
    }, { passive: false });
    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      canvas.setPointerCapture(event.pointerId);
      this.pointers.set(event.pointerId, this.pixel(event));
      this.moved = this.pointers.size > 1;
      this.tooltip.hidden = true;
    });
    canvas.addEventListener("pointermove", (event) => {
      const next = this.pixel(event);
      const prev = this.pointers.get(event.pointerId);
      if (!prev) {
        const point = this.hit(...next);
        this.tooltip.hidden = !point;
        if (point) this.tooltip.textContent = point.item.label || point.item.id;
        return;
      }
      const [dx, dy] = [next[0] - prev[0], next[1] - prev[1]];
      this.moved ||= Math.abs(dx) + Math.abs(dy) > 1;
      this.pointers.set(event.pointerId, next);
      const g = this.geometry();
      if (this.pointers.size === 2) {
        const other = [...this.pointers.entries()].find(([id]) => id !== event.pointerId)[1];
        const before = Math.hypot(prev[0] - other[0], prev[1] - other[1]);
        const after = Math.hypot(next[0] - other[0], next[1] - other[1]);
        if (before > 2 && after > 2) this.zoom(before / after, (next[0] + other[0]) / 2, (next[1] + other[1]) / 2);
      } else if (this.moved) {
        this.pan(dx, dy, g);
      }
    });
    canvas.addEventListener("pointerup", (event) => {
      const pixel = this.pixel(event);
      const point = this.hit(...pixel);
      this.pointers.delete(event.pointerId);
      if (!this.moved && point) this.onPick(point.item);
      else if (!this.moved) {
        const coord = this.world(...pixel);
        if (coord && Math.abs(coord[1]) <= 90) this.onPosition?.(coord);
      }
    });
    for (const type of ["pointercancel", "lostpointercapture"]) {
      canvas.addEventListener(type, (event) => this.pointers.delete(event.pointerId));
    }
    canvas.addEventListener("pointerleave", () => { this.tooltip.hidden = true; });
    canvas.addEventListener("dblclick", (event) => this.zoom(.45, ...this.pixel(event)));
    canvas.addEventListener("keydown", (event) => {
      const g = this.geometry();
      const dx = g.w / 10, dy = g.h / 10;
      const moves = { ArrowLeft: [dx, 0], ArrowRight: [-dx, 0], ArrowUp: [0, dy], ArrowDown: [0, -dy] };
      if (Object.hasOwn(moves, event.key)) {
        this.pan(...moves[event.key], g);
      } else if (["+", "=", "-"].includes(event.key)) this.zoom(event.key === "-" ? 1.5 : .67);
      else return;
      event.preventDefault();
    });
  }

  draw() {
    const g = this.geometry();
    if (g.w < 70 || g.h < 65) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const { ctx } = this;
    const width = Math.round(g.w * dpr), height = Math.round(g.h * dpr);
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, g.w, g.h);
    this.points = [];
    this.survey.draw(ctx, this, g);
    if (this.layers?.grid !== false) this.grid(g);
    ctx.save();
    ctx.beginPath();
    ctx.rect(g.left, g.top, g.right - g.left, g.bottom - g.top);
    ctx.clip();
    this.drawPaths(g);
    this.drawPoints(g);
    ctx.restore();
  }

  grid(g) {
    const { ctx } = this;
    ctx.save();
    ctx.beginPath();
    ctx.rect(g.left, g.top, g.right - g.left, g.bottom - g.top);
    ctx.clip();
    ctx.lineWidth = 1;
    ctx.strokeStyle = "#496170";
    const curve = (count, position) => {
      ctx.beginPath();
      for (let i = 0; i <= count; i++) {
        const [x, y] = openProjection(...hammer(...position(i / count)), g.amount);
        const px = g.cx - x * g.rx, py = g.cy - y * g.ry;
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      }
      ctx.stroke();
    };
    const lonRange = g.all ? 180 : Math.min(180, (g.right-g.left) / (2*g.sx) * 1.3);
    const latRange = g.all ? 90 : Math.min(90, (g.bottom-g.top) / (2*g.sy) * 1.3);
    const step = g.all ? 30 : steps.find((s) => s * g.sx > 55) || 90;
    for (let lon = -Math.floor(lonRange / step) * step; lon <= lonRange; lon += step) {
      curve(40, (t) => [lon * RAD, (2*t-1) * latRange * RAD]);
    }
    for (let lat = -Math.floor(latRange / step) * step; lat <= latRange; lat += step) {
      curve(60, (t) => [(2*t-1) * lonRange * RAD, lat * RAD]);
    }
    ctx.restore();
  }
  drawPaths(g) {
    const { ctx } = this;
    for (const key of ["inventory", "coverage", "associations", "tracks", "catalogue"]) {
      if (!this.layers?.[key]) continue;
      for (const item of this.data[key] || []) {
        if (!item.coords?.length) continue;
        ctx.strokeStyle = color[key];
        ctx.fillStyle = color[key];
        ctx.lineWidth = key === "tracks" ? 1.5 : 1;
        ctx.setLineDash(item.dashed || key === "catalogue" ? [4, 4] : []);
        this.drawAtlasPath(item, key, g);
      }
    }
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);
  }

  drawAtlasPath(item, key, g) {
    const points = item.coords.map(([ra, dec]) => this.xy(ra, dec, g));
    if (points.length < 2) return;
    const { ctx } = this;
    const parts = item.closed ? [...points, points[0]] : points;
    const cut = parts.slice(1).some((p, i) => Math.abs(p[0] - parts[i][0]) > g.rx);
    ctx.beginPath();
    parts.forEach(([x, y], i) => {
      if (i === 0 || Math.abs(x - parts[i - 1][0]) > g.rx) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    if (item.closed && !cut) {
      ctx.closePath();
      ctx.globalAlpha = key === "inventory" && item.certified ? .3 : .08;
      ctx.fill();
    }
    ctx.globalAlpha = .55;
    ctx.stroke();
  }

  drawPoints(g) {
    const { ctx } = this;
    for (const key of ["catalogue", "reference", "sources", "detections", "candidates"]) {
      if (!this.layers?.[key]) continue;
      for (const item of this.data[key] || []) {
        if (!valid(item.ra) || !valid(item.dec)) continue;
        const [x, y] = this.xy(item.ra, item.dec, g);
        if (x < g.left || x > g.right || y < g.top || y > g.bottom) continue;
        const selected = item.id === this.selected;
        const radius = key === "detections" ? 3.5 : key === "catalogue" ? 2.4 : 5;
        ctx.beginPath();
        ctx.strokeStyle = color[key]; ctx.fillStyle = color[key]; ctx.lineWidth = 1.2;
        if (key === "reference" || key === "candidates") {
          ctx.moveTo(x, y - radius); ctx.lineTo(x + radius, y); ctx.lineTo(x, y + radius); ctx.lineTo(x - radius, y); ctx.closePath(); ctx.stroke();
        } else {
          ctx.arc(x, y, radius, 0, 2 * Math.PI);
          if (key === "sources") ctx.stroke();
          else ctx.fill();
        }
        if (selected) {
          ctx.beginPath(); ctx.arc(x, y, radius + 5, 0, 2 * Math.PI); ctx.stroke();
        }
        if (item.ellipse && (selected || this.view.span < .004)) {
          this.ellipse(item, x, y, g);
        }
        if (item.id) this.points.push({ x, y, item });
      }
    }
  }

  ellipse(item, x, y, g) {
    const e = item.ellipse;
    if (![e.a_arcsec, e.b_arcsec, e.pa_deg].every(valid)) return;
    const step = Math.PI / 12;
    const pa = e.pa_deg * Math.PI / 180;
    const cosDec = Math.max(.0001, Math.abs(Math.cos(item.dec * Math.PI / 180)));
    this.ctx.beginPath();
    for (let i = 0; i <= 24; i++) {
      const a = Math.cos(i * step) * e.a_arcsec, b = Math.sin(i * step) * e.b_arcsec;
      const east = a * Math.sin(pa) + b * Math.cos(pa), north = a * Math.cos(pa) - b * Math.sin(pa);
      const [px, py] = this.xy(item.ra + east / (3600 * cosDec), item.dec + north / 3600, g);
      if (i === 0) this.ctx.moveTo(px, py);
      else this.ctx.lineTo(px, py);
    }
    this.ctx.stroke();
  }
}
