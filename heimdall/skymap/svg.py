"""Minimal SVG rendering of the sky-map contract (functional, not the product UI).

Local gnomonic view about the field centre, east to the left. Draws only the
contract's layers: footprints, detections, sources, association segments,
track polylines and candidates, plus a small Mollweide locator. Synthetic
results carry a visible watermark.
"""

from __future__ import annotations

import math
from html import escape

import numpy as np

from .projection import local_map_xy, mollweide

LEVEL_COLOURS = {"ATTEND": "#c0392b", "WATCH": "#e67e22", "INCONCLUSIVE": "#7f8c8d", "EXPLAINED": "#2e86c1",
                 "NOT_ASSESSABLE": "#95a5a6"}


def render_svg(skymap: dict, size: int = 820) -> str:
    field = skymap["field"]
    ra0, dec0, radius = field["ra_deg"], field["dec_deg"], field["radius_deg"]
    margin = 40
    scale = (size - 2 * margin) / (2.3 * radius)
    cx = cy = size / 2

    def xy(ra, dec):
        x, y = local_map_xy(np.asarray(ra, dtype=float), np.asarray(dec, dtype=float), ra0, dec0)
        return cx + np.asarray(x) * scale, cy - np.asarray(y) * scale

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size + 60}" viewBox="0 0 {size} {size + 60}" '
           'font-family="sans-serif" font-size="11">', f'<rect width="{size}" height="{size + 60}" fill="#ffffff"/>']
    step = 10 ** math.floor(math.log10(radius)) / 2
    for k in range(-30, 31):
        d = dec0 + k * step
        if abs(d - dec0) <= 1.3 * radius:
            ras = ra0 + np.linspace(-1.4, 1.4, 50) * radius / max(math.cos(math.radians(dec0)), 0.05)
            x, y = xy(ras, np.full(50, d))
            out.append('<polyline fill="none" stroke="#eeeeee" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in zip(x, y)) + '"/>')
        r = ra0 + k * step / max(math.cos(math.radians(dec0)), 0.05)
        if abs(k * step) <= 1.3 * radius:
            x, y = xy(np.full(50, r), dec0 + np.linspace(-1.4, 1.4, 50) * radius)
            out.append('<polyline fill="none" stroke="#eeeeee" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in zip(x, y)) + '"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{radius * scale:.1f}" fill="none" stroke="#bbbbbb" stroke-dasharray="4 3"/>')
    layers = skymap["layers"]
    for c in layers["coverage"]:
        if not c.get("polygon"):
            continue
        x, y = xy(c["polygon"]["ra"], c["polygon"]["dec"])
        colour = "#2471a3" if c["included"] else "#cb4335"
        out.append(f'<polygon fill="{colour}" fill-opacity="0.03" stroke="{colour}" stroke-opacity="0.35" stroke-width="0.6" points="'
                   + " ".join(f"{a:.1f},{b:.1f}" for a, b in zip(x, y)) + f'"><title>{escape(c["obs_id"])}</title></polygon>')
    ext = skymap.get("external_layers") or {}
    for st in (ext.get("star_catalogue") or {}).get("items", []):
        x, y = (float(v) for v in xy(st["ra"], st["dec"]))
        out.append(f'<path d="M {x - 2.5:.1f} {y:.1f} H {x + 2.5:.1f} M {x:.1f} {y - 2.5:.1f} V {y + 2.5:.1f}" stroke="#d4ac0d" '
                   f'stroke-width="0.7"><title>Gaia DR3 {escape(str(st["source_id"]))} G={st["g_mag"]}</title></path>')
    for b in (ext.get("solar_system_ephemerides") or {}).get("items", []):
        x, y = (float(v) for v in xy(b["ra"], b["dec"]))
        out.append(f'<rect x="{x - 4:.1f}" y="{y - 4:.1f}" width="8" height="8" fill="none" stroke="#117a65" stroke-width="1">'
                   f'<title>{escape(b["name"])} predicted at MJD {b["mjd"]} (JPL SB Ident)</title></rect>')
    for a in layers["associations"]:
        for seg in a["segments"]:
            x, y = xy([seg["from"][0], seg["to"][0]], [seg["from"][1], seg["to"][1]])
            out.append(f'<line x1="{x[0]:.1f}" y1="{y[0]:.1f}" x2="{x[1]:.1f}" y2="{y[1]:.1f}" stroke="#aab7b8" stroke-width="0.5"/>')
    for d in layers["detections"]:
        x, y = xy(d["ra"], d["dec"])
        out.append(f'<circle cx="{float(x):.1f}" cy="{float(y):.1f}" r="1.2" fill="#566573"/>')
    for s in layers["sources"]:
        x, y = xy(s["ra"], s["dec"])
        out.append(f'<circle cx="{float(x):.1f}" cy="{float(y):.1f}" r="3.5" fill="none" stroke="#1c2833" stroke-width="0.8">'
                   f'<title>{escape(s["source_id"])} n_obs={s["n_obs"]}</title></circle>')
    for t in layers["tracks"]:
        x, y = xy([p["ra"] for p in t["points"]], [p["dec"] for p in t["points"]])
        out.append('<polyline fill="none" stroke="#8e44ad" stroke-width="1.6" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in zip(x, y))
                   + f'"><title>{escape(t["track_id"])} (candidate linkage, not an orbit)</title></polyline>')
    for c in layers["candidates"]:
        x, y = (float(v) for v in xy(c["ra"], c["dec"]))
        colour = LEVEL_COLOURS.get(c["attention_level"], "#000000")
        out.append(f'<path d="M {x:.1f} {y - 7:.1f} L {x + 7:.1f} {y:.1f} L {x:.1f} {y + 7:.1f} L {x - 7:.1f} {y:.1f} Z" fill="none" '
                   f'stroke="{colour}" stroke-width="1.6"><title>{escape(c["candidate_id"])}: {c["attention_level"]}, '
                   f'validity {c["validity"]}</title></path>')
    mx, my = mollweide(np.array([ra0]), np.array([dec0]))
    ox, oy, s = size - 130, size - 70, 40
    out.append(f'<ellipse cx="{ox}" cy="{oy}" rx="{2 * s * math.sqrt(2) / 2:.1f}" ry="{s * math.sqrt(2) / 2:.1f}" fill="#fbfcfc" stroke="#999"/>')
    out.append(f'<circle cx="{ox + float(mx[0]) * s / 2:.1f}" cy="{oy - float(my[0]) * s / 2:.1f}" r="2.5" fill="#c0392b"/>')
    title = f'HEIMDALL sky map - run {escape(str(skymap.get("run_id")))} - field RA {ra0:.4f}, Dec {dec0:.4f}, r = {radius:.3f} deg (ICRS, east left)'
    out.append(f'<text x="{margin}" y="{size + 20}" fill="#1c2833">{title}</text>')
    legend = "diamonds: candidates (red ATTEND, orange WATCH, grey INCONCLUSIVE, blue EXPLAINED); purple: linked track; circles: sources"
    out.append(f'<text x="{margin}" y="{size + 40}" fill="#566573">{escape(legend)}</text>')
    if skymap.get("watermark"):
        out.append(f'<text x="{cx}" y="{margin}" text-anchor="middle" font-size="22" fill="#c0392b" fill-opacity="0.75">'
                   f'{escape(skymap["watermark"])}</text>')
    out.append("</svg>")
    return "\n".join(out)
