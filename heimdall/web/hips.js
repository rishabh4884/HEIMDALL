// IRSA HiPS PNG imagery in equatorial coordinates. One-based plane labels in
// the UI, zero-based frame numbers in HiPS URLs. The PNGs are display data.
const ROOT = "/sky/spherex";
const RAD = Math.PI / 180;
const GAL = [
  [-0.0548755604, -0.8734370902, -0.4838350155],
  [0.4941094279, -0.4448296300, 0.7469822445],
  [-0.8676661490, -0.1980763734, 0.4559837762],
];
const unit = (longitude, latitude) => [Math.cos(latitude) * Math.cos(longitude), Math.cos(latitude) * Math.sin(longitude), Math.sin(latitude)];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const LON = (x) => ((x % 360) + 360) % 360;
const latitude = (z) => Math.asin(Math.max(-1, Math.min(1, z)));

export function equatorialToGalactic(ra, dec) {
  const v = unit(ra * RAD, dec * RAD);
  const [x, y, z] = GAL.map((row) => dot(row, v));
  return [Math.atan2(y, x), latitude(z)];
}

function galacticToEquatorial(lon, lat) {
  const [x, y, z] = unit(lon, lat);
  const a = GAL[0][0] * x + GAL[1][0] * y + GAL[2][0] * z;
  const b = GAL[0][1] * x + GAL[1][1] * y + GAL[2][1] * z;
  const c = GAL[0][2] * x + GAL[1][2] * y + GAL[2][2] * z;
  return [LON(Math.atan2(b, a) / RAD), latitude(c) / RAD];
}

export function hammer(lon, lat) {
  const scale = Math.sqrt(1 + Math.cos(lat) * Math.cos(lon / 2));
  return [Math.cos(lat) * Math.sin(lon / 2) / scale, Math.sin(lat) / scale];
}

export function unhammer(x, y) {
  if (x * x + y * y > 1) return null;
  const z = Math.sqrt(1 - (x * x + y * y) / 2);
  return [2 * Math.atan2(Math.SQRT2 * z * x, 2 * z * z - 1), latitude(Math.SQRT2 * z * y)];
}

export function atlasPoint(x, y, center = 0, tilt = 0) {
  const point = unhammer(x, y);
  if (!point) return null;
  const [a, b, c] = unit(...point);
  const forward = a * Math.cos(tilt) - c * Math.sin(tilt);
  const up = a * Math.sin(tilt) + c * Math.cos(tilt);
  return galacticToEquatorial(Math.atan2(b, forward) + center, latitude(up));
}

export function projectSky(ra, dec, center, tilt) {
  const [lon, lat] = equatorialToGalactic(ra, dec);
  const [a, b, c] = unit(lon - center, lat);
  return hammer(Math.atan2(b, a * Math.cos(tilt) + c * Math.sin(tilt)),
    latitude(c * Math.cos(tilt) - a * Math.sin(tilt)));
}

function spread(n) {
  n = (n | (n << 8)) & 0x00ff00ff;
  n = (n | (n << 4)) & 0x0f0f0f0f;
  n = (n | (n << 2)) & 0x33333333;
  return (n | (n << 1)) & 0x55555555;
}

// HEALPix NESTED face and integer coordinates at the requested fine order.
function faceXY(ra, dec, nside) {
  const z = Math.sin(dec * RAD), za = Math.abs(z);
  const tt = LON(ra) / 90;
  if (za <= 2 / 3) {
    const p = nside * (tt + .5), q = nside * z * .75;
    const jp = p - q, jm = p + q;
    const a = Math.floor(jp / nside), b = Math.floor(jm / nside);
    return [a === b ? a | 4 : a < b ? a : b + 8, jm % nside, nside - jp % nside];
  }
  const quadrant = Math.min(3, Math.floor(tt)), r = tt - quadrant;
  const s = nside * Math.sqrt(3 * (1 - za));
  const jp = Math.min(nside - 1e-8, r * s), jm = Math.min(nside - 1e-8, (1 - r) * s);
  return z > 0 ? [quadrant, Math.min(nside - 1e-8, nside - jm), Math.min(nside - 1e-8, nside - jp)] : [quadrant + 8, jp, jm];
}

export function tileSample(ra, dec, order, pixelBits, fractional = false) {
  const [face, ix, iy] = faceXY(ra, dec, 2 ** (order + pixelBits));
  const size = 1 << pixelBits;
  const nside = 2 ** (order + pixelBits);
  const x = Math.min(nside - 1e-8, ix), y = Math.min(nside - 1e-8, iy);
  const tile = face * 4 ** order + spread(x >> pixelBits) + 2 * spread(y >> pixelBits);
  // The PNG maps the west corner to top right and north to bottom right.
  return fractional ? [tile, y % size - .5, x % size - .5] : [tile, Math.floor(y % size), Math.floor(x % size)];
}

function sampleBilinear(image, originX, originY, size, u, v, out, offset, scale) {
  const x = Math.max(0, Math.min(size - 1, u)), y = Math.max(0, Math.min(size - 1, v));
  const i = Math.floor(x), j = Math.floor(y);
  const fx = x - i, fy = y - j;
  const a = ((originY + j) * image.width + originX + i) * 4;
  const b = a + (i + 1 < size ? 4 : 0);
  const c = a + (j + 1 < size ? image.width * 4 : 0);
  const d = c + b - a;
  const data = image.data;
  for (let channel = 0; channel < 4; channel++) {
    const top = data[a + channel] + fx * (data[b + channel] - data[a + channel]);
    const bottom = data[c + channel] + fx * (data[d + channel] - data[c + channel]);
    out[offset + channel] = (top + fy * (bottom - top)) * (channel === 3 ? 1 : scale);
  }
}

function decode(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const timeout = setTimeout(() => {
      img.onload = null;
      img.onerror = null;
      img.removeAttribute("src");
      reject(new Error("SPHEREx tile timed out"));
    }, 11000);
    img.onerror = () => { clearTimeout(timeout); reject(new Error("SPHEREx tile unavailable")); };
    img.onload = () => {
      clearTimeout(timeout);
      try {
        const canvas = document.createElement("canvas");
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context.drawImage(img, 0, 0);
        resolve(context.getImageData(0, 0, canvas.width, canvas.height));
      } catch (error) { reject(error); }
    };
    img.src = url;
  });
}

export async function wavelengths() {
  const response = await fetch(`${ROOT}/properties`, { signal: AbortSignal.timeout(9000) });
  if (!response.ok) return [];
  const meta = (await response.text()).split("\n").find((line) => line.startsWith("data_cube_crval3_arr"));
  const values = (meta?.split("=")[1] || "").trim().split(/\s+/).map(Number);
  return values.length === 102 && values.every((x) => x > 0) ? values : [];
}

export class SurveyImage {
  constructor(onChange) {
    this.layer = document.createElement("canvas");
    this.context = this.layer.getContext("2d");
    this.onChange = onChange;
    this.frame = 1;
    this.visible = true;
    this.contrast = 0;
    this.cache = new Map();
    this.pending = new Set();
    this.failed = new Map();
    this.retryTimers = new Map();
    this.current = null;
    this.generation = 0;
  }

  settings(frame, visible, contrast) {
    if (frame === this.frame && visible === this.visible && contrast === this.contrast) return;
    this.current = null;
    this.signature = "";
    Object.assign(this, { frame, visible, contrast });
    this.onChange();
  }

  status() {
    if (!this.visible) return "Imagery hidden";
    const url = this.atlasURL();
    if (!this.cache.has(url)) return this.failed.has(url) ? "Plane unavailable · check network or local image cache" : "Loading SPHEREx DR1 imagery…";
    if (this.current?.order >= 3) return this.current.ready ? "DR1 mosaic · detail tiles" : "DR1 mosaic · overview while detail loads";
    return "DR1 mosaic · visualization stretch";
  }

  atlasURL() { return `${ROOT}/allsky/${this.frame}.png`; }

  load(url, max, atlas = false) {
    const existing = this.cache.get(url);
    if (existing) return existing;
    if (this.pending.has(url) || this.pending.size >= 4 || (this.failed.get(url) || 0) > Date.now()) return null;
    this.pending.add(url);
    decode(url).then((pixels) => {
      if (atlas ? pixels.width % 64 !== 0 || Math.floor(pixels.width / 64) * Math.floor(pixels.height / 64) < 768 : pixels.width !== 512 || pixels.height !== 512) {
        throw new Error("Unexpected tile dimensions");
      }
      this.cache.set(url, pixels);
      const peers = [...this.cache.keys()].filter((key) => key.includes("/allsky/") === atlas);
      if (peers.length > max) this.cache.delete(peers[0]);
      this.failed.delete(url);
    }).catch(() => {
      if (this.failed.size >= 200) this.failed.delete(this.failed.keys().next().value);
      const until = Date.now() + 25000;
      this.failed.set(url, until);
      if (atlas && this.retryTimers.size < 2 && !this.retryTimers.has(url)) {
        this.retryTimers.set(url, setTimeout(() => {
          this.retryTimers.delete(url);
          if (url === this.atlasURL() && this.visible) { this.generation++; this.onChange(); }
        }, 25500));
      }
    }).finally(() => {
      this.pending.delete(url);
      this.generation++;
      this.onChange();
    });
    return null;
  }

  draw(ctx, map, geometry) {
    if (!this.visible) return;
    const atlas = this.load(this.atlasURL(), 2, true);
    if (!atlas) return;
    const { left, right, top, bottom, all } = geometry;
    const width = Math.max(1, Math.min(720, Math.round(right - left)));
    const height = Math.max(1, Math.round(width * (bottom - top) / (right - left)));
    const moving = performance.now() < (this.movingUntil || 0);
    const signature = [width, height, right - left, bottom - top, all, map.view.ra, map.view.dec, map.view.span, this.frame, this.contrast, this.generation, moving].join("/");
    if (signature !== this.signature) {
      this.signature = signature;
      const order = all ? 0 : Math.min(5, Math.max(0, Math.ceil(Math.log2(58.6 * Math.max(geometry.sx, geometry.sy) / 512))));
      this.render(atlas, map, geometry, width, height, order, moving);
    }
    ctx.drawImage(this.layer, left, top, right - left, bottom - top);
  }

  render(atlas, map, g, width, height, order, moving) {
    this.layer.width = width;
    this.layer.height = height;
    const out = this.context.createImageData(width, height);
    const cols = atlas.width / 64;
    const tiles = new Map();
    const rgba = out.data;
    const stepX = (g.right - g.left) / width, stepY = (g.bottom - g.top) / height;
    const scale = this.contrast ? 2 ** (this.contrast / 50) : 1;
    let ready = false;
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const location = map.world(g.left + (x + .5) * stepX, g.top + (y + .5) * stepY, g);
        if (!location || location[1] < -90 || location[1] > 90) continue;
        const [ra, dec] = location;
        let pixel, u, v, ox = 0, oy = 0, size = 512;
        if (order >= 3) {
          const [id, x, y] = tileSample(ra, dec, order, 9, true);
          const url = `${ROOT}/${order}/${id}/${this.frame}.png`;
          if (!tiles.has(id) && tiles.size < 32) tiles.set(id, moving ? this.cache.get(url) : this.load(url, 28));
          pixel = tiles.get(id);
          u = x; v = y;
          ready ||= Boolean(pixel);
        }
        if (!pixel) {
          const [id, x, y] = tileSample(ra, dec, 3, 6, true);
          pixel = atlas;
          size = 64;
          ox = id % cols * 64; oy = Math.floor(id / cols) * 64;
          u = x; v = y;
        }
        sampleBilinear(pixel, ox, oy, size, u, v, rgba, (y * width + x) * 4, scale);
      }
    }
    this.context.putImageData(out, 0, 0);
    this.current = { order, ready };
  }
}
