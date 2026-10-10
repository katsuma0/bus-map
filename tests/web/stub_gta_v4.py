#!/usr/bin/env python3
"""Stub v4 networks for the ten GTA cities, for the renderer tests until A's trim lands.

NOT the real build (spec A3 to A10): it takes the legacy GTA network (one service date,
data/gta/built/network.json.gz, same origin as the batch area), the unnamed Overture divisions
in data/gta/basemap/divisions.geojson (matched by subtype and the areas of spec 0.1.9), the
recipes and brands in tests/fixtures/gta/, and writes networks shaped like spec 2.9:

  build/stub_v4/<id>/day/network.json.gz    day and rush variants
  build/stub_v4/<id>/week/network.json.gz   week variant (every trip on every day), unless --no-week

Counts are A9's rule on that one date: a trip runs at minute m for ceil(t0/60) <= m <= floor(t1/60),
inside when shapely.contains_xy holds at its position. Brands, groups, credit, mask and panel
side follow A10, A8.2 and A8.7 closely enough for layout, colour and speed tests. The rush frame
is Toronto's pinned one, else the day frame zoomed 1.6x about the busiest inside 0.25 km cell.

  python3 -I tests/web/stub_gta_v4.py [--only gta-toronto,gta-markham] [--no-week]
"""

import argparse
import gzip
import json
import math
import os
import re
import sys

import numpy as np
import shapely
from shapely.geometry import shape as geo_shape
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
FIX = os.path.join(ROOT, "tests", "fixtures", "gta")
OUT = os.path.join(ROOT, "build", "stub_v4")
ORIGIN = (-79.47, 43.80)
KX = 111.32 * math.cos(math.radians(ORIGIN[1]))
KY = 110.574
# E2 frames (D3.2 with cities/defaults.json fit_box) and spec 0.1.9 areas (subtype, km2) of the
# unnamed divisions.
CITY = {
    "gta-toronto": ("county", 661.7, 119.5, [12.5, -21.6]),
    "gta-mississauga": ("locality", 295.8, 86.0, [-12.8, -30.2]),
    "gta-brampton": ("locality", 267.8, 82.0, [-19.8, -16.8]),
    "gta-markham": ("locality", 211.4, 59.0, [16.1, 2.8]),
    "gta-vaughan": ("locality", 272.4, 66.0, [-4.9, -2.8]),
    "gta-oakville": ("locality", 153.5, 54.5, [-17.4, -44.7]),
    "gta-richmond-hill": ("locality", 101.9, 50.0, [5.4, 6.1]),
    "gta-burlington": ("locality", 198.7, 63.5, [-26.8, -52.9]),
    "gta-oshawa": ("locality", 161.6, 71.5, [50.9, 8.0]),
    "gta-whitby": ("locality", 167.0, 70.5, [43.8, 6.3]),
}
TEMPLATES = {
    "day": [["Every {modes_singular} in {place}, 24 hours", "Busiest at {peak_time} with {peak_count} vehicles"],
            ["24 hours of {modes_plural} in {place}", "One average {month} weekday"],
            ["One {month} weekday in {place}", "{trips} trips, rush hour to rush hour"]],
    "rush": [["Every {modes_singular} in {place}, 6:30 to 9:30 am", "Busiest at {peak_time} with {peak_count} vehicles"],
             ["The morning rush in {place}", "Every {modes_singular} on an average {month} weekday"],
             ["Three hours of {modes_plural} in {place}", "Busiest at {peak_time}"]],
    "week": [["Every {modes_singular} in {place}, Monday to Sunday", "{month} timetables, {trips} trips"],
             ["A week of {modes_plural} in {place}", "Busiest at {peak_time} with {peak_count} vehicles"],
             ["Seven {month} days in {place}", "Weekdays, then the weekend"]],
}
LABELS = {"day": "An average October weekday", "rush": "Morning rush, an average October weekday",
          "week": "An average October week"}


def to_km(lon, lat):
    return (np.asarray(lon) - ORIGIN[0]) * KX, (np.asarray(lat) - ORIGIN[1]) * KY


def project(geom):
    return shapely.transform(geom, lambda c: np.column_stack(to_km(c[:, 0], c[:, 1])))


def area_km2(feature_geom):
    b = feature_geom.bounds
    kx = 111.32 * math.cos(math.radians((b[1] + b[3]) / 2))
    return shapely.transform(feature_geom, lambda c: np.column_stack(((c[:, 0]) * kx, c[:, 1] * KY))).area


def boundaries(recipes):
    with open(os.path.join(ROOT, "data", "gta", "basemap", "divisions.geojson"), encoding="utf-8") as fh:
        feats = json.load(fh)["features"]
    out = {}
    for cid, (subtype, km2, _, _) in CITY.items():
        lon, lat = recipes[cid]["center"]
        best = None
        for f in feats:
            if f["properties"].get("subtype") != subtype:
                continue
            g = geo_shape(f["geometry"])
            a = area_km2(g)
            if abs(a - km2) / km2 > 0.02:
                continue
            if not shapely.contains_xy(g.buffer(0.05), lon, lat):
                continue
            if best is None or abs(a - km2) < abs(best[1] - km2):
                best = (g, a, f["properties"]["id"])
        if best is None:
            raise SystemExit(f"{cid}: no {subtype} division of about {km2} km2 around {lon}, {lat}")
        poly = shapely.simplify(project(best[0]), 0.02, preserve_topology=True)
        out[cid] = (poly, best[1], best[2])
    return out


def positions(net):
    """Every (trip, minute) a trip runs, with its km position: the A9 running rule."""
    shapes = [(np.asarray(s["xy"], float).reshape(-1, 2), np.asarray(s["cum"], float)) for s in net["shapes"]]
    tr, mi, xs, ys = [], [], [], []
    for n, trip in enumerate(net["trips"]):
        t = np.asarray(trip["t"], float)
        d = np.asarray(trip["d"], float)
        m = np.arange(math.ceil(t[0] / 60), math.floor(t[-1] / 60) + 1)
        if not len(m):
            continue
        T = 60.0 * m
        i = np.clip(np.searchsorted(t, T, side="right") - 1, 0, len(t) - 2)
        dt = t[i + 1] - t[i]
        with np.errstate(invalid="ignore", divide="ignore"):
            dist = np.where(dt > 0, d[i] + (T - t[i]) / np.where(dt > 0, dt, 1) * (d[i + 1] - d[i]), d[i])
        xy, cum = shapes[trip["s"]]
        k = np.clip(np.searchsorted(cum, dist, side="right") - 1, 0, len(cum) - 2)
        seg = cum[k + 1] - cum[k]
        f = np.clip(np.where(seg > 0, (dist - cum[k]) / np.where(seg > 0, seg, 1), 0), 0, 1)
        tr.append(np.full(len(m), n))
        mi.append(m)
        xs.append(xy[k, 0] + (xy[k + 1, 0] - xy[k, 0]) * f)
        ys.append(xy[k, 1] + (xy[k + 1, 1] - xy[k, 1]) * f)
    return np.concatenate(tr), np.concatenate(mi), np.concatenate(xs), np.concatenate(ys)


def brand_routes(net, brands):
    """A10's first-match rule on the legacy routes; color_raw is '' where build_feed wrote 2f6bff."""
    def informative(hexs):
        if not re.fullmatch(r"[0-9a-f]{6}", hexs or "") or hexs in ("ffffff", "000000"):
            return False
        r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (0, 2, 4))
        mx, mn = max(r, g, b), min(r, g, b)
        return mx - mn > 0.06
    entries = {f: e for e in brands["agencies"] for f in e["feeds"]}
    meta_brands = {}
    route_brand = []
    for r in net["routes"]:
        raw = "" if r.get("color", "").lower() == "2f6bff" else r.get("color", "").lower()
        e = entries.get(r["feed"])
        if e is None:
            bid, b = f"mode:{r['mode']}", {"id": f"mode:{r['mode']}", "label": r["mode"], "hex": "", "kind": "mode",
                                          "entry": None, "rail": r["mode"] != "bus", "alt": None, "verified": False}
        elif e["lines"] in ("rail", "all") and (r["mode"] != "bus" or e["lines"] == "all") and informative(raw) \
                and raw != e["color"].lstrip("#").lower():
            bid = f"{e['id']}:line:{raw}"
            b = {"id": bid, "label": r["short"], "hex": raw, "kind": "line", "entry": e["id"], "rail": True,
                 "alt": None, "verified": e["verified"]}
        else:
            rule = next((x for x in e.get("rules", []) if x.get("short") and re.fullmatch(x["short"], r["short"] or "")), None)
            if rule:
                bid = f"{e['id']}:{rule['id']}"
                b = {"id": bid, "label": rule["label"], "hex": rule["color"].lstrip("#").lower(), "kind": "rule",
                     "entry": e["id"], "rail": False, "alt": None, "verified": rule["verified"]}
            else:
                bid = e["id"]
                b = {"id": bid, "label": e["label"], "hex": e["color"].lstrip("#").lower(), "kind": "agency",
                     "entry": e["id"], "rail": False, "alt": (e.get("alt") or "").lstrip("#").lower() or None,
                     "verified": e["verified"]}
        meta_brands.setdefault(bid, dict(b, source="stub"))
        route_brand.append(bid)
    return meta_brands, route_brand


def rle_mask(poly, bbox):
    cell = 0.025
    x0, y0 = bbox[0] - 0.1, bbox[1] - 0.1
    nx = math.ceil((bbox[2] + 0.1 - x0) / cell)
    ny = math.ceil((bbox[3] + 0.1 - y0) / cell)
    cx = x0 + (np.arange(nx) + 0.5) * cell
    rle = []
    shapely.prepare(poly)
    for iy in range(ny):
        row = shapely.contains_xy(poly, cx, np.full(nx, y0 + (iy + 0.5) * cell))
        edges = np.flatnonzero(np.diff(np.concatenate(([0], row.astype(np.int8), [0]))))
        # runs alternate outside / inside, starting outside
        prev, runs = 0, []
        for k in range(0, len(edges), 2):
            runs += [int(edges[k] - prev), int(edges[k + 1] - edges[k])]
            prev = edges[k + 1]
        runs.append(int(nx - prev))
        if runs[-1] == 0 and len(runs) > 1:
            runs.pop()
        rle += runs
    return {"cell_km": cell, "x0": round(x0, 4), "y0": round(y0, 4), "nx": nx, "ny": ny, "rle": rle}


_FONT = {}


def credit_width(text):
    if not _FONT:
        f = TTFont(os.path.join(ROOT, "web", "fonts", "InterX.woff2"))
        f = instancer.instantiateVariableFont(f, {"wght": 400, "opsz": 22})
        _FONT.update(cmap=f.getBestCmap(), hm=f["hmtx"], upm=f["head"].unitsPerEm)
    return sum(_FONT["hm"][_FONT["cmap"][ord(c)]][0] for c in text if ord(c) in _FONT["cmap"]) / _FONT["upm"] * 22


def credit_lines(text, width=504):
    lines, cur = [], ""
    for w in text.split(" "):
        nxt = f"{cur} {w}" if cur else w
        if cur and credit_width(nxt) > width:
            lines.append(cur)
            cur = w
        else:
            cur = nxt
    return lines + [cur]


def argmax(h, lo, hi):
    best = lo
    for m in range(lo, hi):
        if h[m % len(h)] > h[best % len(h)]:
            best = m
    return best


def build_city(cid, recipe, net, pos, bnd, brands, week):
    poly, km2, div_id = bnd
    subtype, _, kmv, center = CITY[cid]
    period = 10080 if week else 1440
    bbox = [round(v, 3) for v in poly.bounds]
    hw = kmv * 9 / 16 / 2 * 1.25 + 1
    hh = kmv / 2 * 1.25 + 1
    box = [center[0] - hw, center[1] - hh, center[0] + hw, center[1] + hh]
    meta_brands, route_brand = brand_routes(net, brands)
    tr, mi, xs, ys = pos
    near = (xs >= bbox[0]) & (xs <= bbox[2]) & (ys >= bbox[1]) & (ys <= bbox[3])
    ins = np.zeros(len(tr), bool)
    shapely.prepare(poly)
    ins[near] = shapely.contains_xy(poly, xs[near], ys[near])
    routes = net["routes"]
    trip_route = np.array([t["r"] for t in net["trips"]])
    r_of = trip_route[tr[ins]]
    m_of = mi[ins] % 1440
    mode_ids = [m["id"] for m in net["meta"]["modes"]]
    day = np.bincount(m_of, minlength=1440).astype(float)
    by_mode = {md: np.bincount(m_of[np.array([routes[r]["mode"] == md for r in r_of], bool)], minlength=1440).astype(float)
               if len(r_of) else np.zeros(1440) for md in mode_ids}
    brand_of = np.array([route_brand[r] for r in r_of])
    by_brand = {b: np.bincount(m_of[brand_of == b], minlength=1440).astype(float) for b in sorted(set(brand_of))}
    am = argmax(day, 300, 631)
    total_am = day[am] or 1
    share = {b: by_brand.get(b, np.zeros(1440))[am] / total_am for b in meta_brands}
    cand = {}
    for b, info in meta_brands.items():
        key = b if info["kind"] in ("mode", "gtfs") else info["entry"]
        cand[key] = cand.get(key, 0) + share[b]
    shown = [k for k, s in sorted(cand.items(), key=lambda kv: (-kv[1], kv[0])) if s >= 0.03][:3]
    groups = []
    for k in shown:
        entry = next((e for e in brands["agencies"] if e["id"] == k), None)
        groups.append({"id": k, "label": entry["label"] if entry else k, "brand": k, "share": round(cand[k], 4)})
    other = round(sum(s for k, s in cand.items() if k not in shown), 4)
    if other > 0:
        groups.append({"id": "other", "label": "other", "brand": None, "share": other})
    group_of_brand = {b: ((b if i["kind"] in ("mode", "gtfs") else i["entry"]) if
                          (b if i["kind"] in ("mode", "gtfs") else i["entry"]) in shown else "other")
                      for b, i in meta_brands.items()}
    by_group = {g["id"]: np.zeros(1440) for g in groups}
    for b, h in by_brand.items():
        g = group_of_brand[b]
        if g in by_group:
            by_group[g] += h

    # Trips in the trim box, shapes re-indexed in first-use order.
    shp = net["shapes"]
    sb = [(min(s["xy"][0::2]), min(s["xy"][1::2]), max(s["xy"][0::2]), max(s["xy"][1::2])) for s in shp]
    keep = [t for t in net["trips"]
            if not (sb[t["s"]][2] < box[0] or sb[t["s"]][0] > box[2] or sb[t["s"]][3] < box[1] or sb[t["s"]][1] > box[3])]
    s_map, r_map, out_shapes, out_routes, out_trips = {}, {}, [], [], []
    order = sorted(meta_brands, key=lambda b: (-share[b], b))
    b_index = {b: i for i, b in enumerate(order)}
    for t in keep:
        if t["s"] not in s_map:
            s_map[t["s"]] = len(out_shapes)
            out_shapes.append(shp[t["s"]])
        if t["r"] not in r_map:
            r = dict(routes[t["r"]])
            raw = "" if r.get("color", "").lower() == "2f6bff" else r.get("color", "").lower()
            r.update(color_raw=raw, agency=r["feed"], type=3 if r["mode"] == "bus" else 2,
                     brand=b_index[route_brand[t["r"]]], group=group_of_brand[route_brand[t["r"]]])
            r_map[t["r"]] = len(out_routes)
            out_routes.append(r)
        nt = {"r": r_map[t["r"]], "s": s_map[t["s"]], "t": t["t"], "d": t["d"]}
        if week:
            nt["w"] = 127
        out_trips.append(nt)

    tile = (lambda h: np.tile(h, 7)) if week else (lambda h: h)
    hist = tile(day)
    am = argmax(hist, 300, 631)
    pm = argmax(hist, 870, 1171)
    feeds_inside = sorted({routes[r]["feed"] for r in r_of})
    labels = []
    for k, s in sorted(cand.items(), key=lambda kv: (-kv[1], kv[0])):
        if s <= 0:
            continue
        entry = next((e for e in brands["agencies"] if e["id"] == k), None)
        labels.append(entry["label"] if entry else k)
    credit = f"Data: {', '.join(labels)} · Map: Overture, OSM"
    if len(credit_lines(credit)) > 2:
        credit = f"Data: {len(labels)} transit agencies · Map: Overture, OSM"

    def variant(name, start, end, frames, render, frame=None):
        pk = argmax(hist, start // 60, math.ceil(end / 60))
        return {"start": start, "end": end, "label": LABELS[name], "frame": frame,
                "peak": {"count": round(float(hist[pk % period])), "time": pk * 60},
                "render": dict({"DURATION_FRAMES": frames, "HOLD_START": 0, "HOLD_END": 0}, **render)}

    if week:
        variants = {"week": variant("week", am * 60, am * 60 + 604800, 1800,
                                    {"LOOP": "wrap", "TIME_WARP_MODE": "activity-daily", "TIME_WARP_FLOOR": 0.3,
                                     "SPARK_SMOOTH_MIN": 60, "TRAIL_MINUTES": 30, "BUS_HALO_ALPHA": 0.1,
                                     "BUS_CORE_R": 0, "WEEKEND_BAND": True, "CLOCK_ROUND": 60})}
    else:
        rf = (recipe.get("rush") or {}).get("frame")
        if not rf:
            # Busiest inside 0.25 km cell at the am peak, day frame zoomed 1.6x about it.
            sel = ins & (mi % 1440 == am)
            if sel.any():
                cx = float(np.median(xs[sel]))
                cy = float(np.median(ys[sel]))
            else:
                cx, cy = center
            rk = kmv / 1.6
            rhw, rhh = rk * 9 / 16 / 2, rk / 2
            cx = min(max(cx, box[0] + rhw), box[2] - rhw)
            cy = min(max(cy, box[1] + rhh), box[3] - rhh)
            rf = {"km_vertical": round(rk, 1), "center_km": [round(cx, 1), round(cy, 1)]}
            trail = max(5, min(10, round(8 * 1.6 / 1.6)))
        else:
            trail = 8
        variants = {
            "day": variant("day", am * 60, am * 60 + 86400, 1500,
                           {"LOOP": "wrap", "TIME_WARP_MODE": "activity", "SPARK_SMOOTH_MIN": 35}),
            "rush": variant("rush", 23400, 34200, 750,
                            {"LOOP": "xfade", "TIME_WARP_MODE": "linear", "SPARK_SMOOTH_MIN": 15, "TRAIL_MINUTES": trail},
                            frame=rf),
        }
    if "rush" not in recipe.get("variants", ["day", "rush", "week"]):
        variants.pop("rush", None)
    # Panel side (A8.7) at the am peak in the day frame.
    s = 1920 / kmv
    sel = ins & (mi % 1440 == am)
    px = 540 + (xs[sel] - center[0]) * s
    py = 960 - (ys[sel] - center[1]) * s
    # cities/defaults.json panel.rect and its right-hand position against SAFE.x1 800.
    under = {side: int(((px >= x0) & (px <= x0 + 560) & (py >= 1080) & (py <= 1440)).sum())
             for side, x0 in (("left", 120), ("right", 240))}
    side = "left" if under["left"] < under["right"] else "right"
    if abs(under["left"] - under["right"]) < 0.10 * max(under.values() or [0]) or under["left"] == under["right"]:
        side = recipe.get("variety", {}).get("panel_side", "left")
    first = next(iter(variants.values()))
    rings = [[round(c, 3) for xy in p.exterior.coords for c in xy] for p in getattr(poly, "geoms", [poly])]
    holes = [[round(c, 3) for xy in r.coords for c in xy] for p in getattr(poly, "geoms", [poly]) for r in p.interiors]
    place = recipe["place"]
    meta = {
        "schema": 4, "kind": "city", "id": cid, "batch": "gta", "area": "gta", "place": place, "title": place.upper(),
        "subtitle": first["label"], "service_date": "2026-10", "origin": list(ORIGIN),
        "day_start": first["start"], "day_end": first["end"],
        "frame": {"km_vertical": kmv, "center_km": center},
        "trim": {"scale": 1.25, "box_km": [round(v, 3) for v in box]},
        "modes": [{k: v for k, v in m.items() if k not in ("color", "trail")} for m in net["meta"]["modes"]],
        "attribution": [credit], "credit": credit, "build_key": "stub",
        "feeds": [{"id": f, "name": f, "major": True} for f in feeds_inside],
        "trips_total": len(out_trips) * (7 if week else 1),
        "timeline": {"kind": "week" if week else "day", "period": 604800 if week else 86400,
                     "basis": "average-week" if week else "average-weekday", "month": "2026-10",
                     "month_label": "October", "fallback_feeds": []},
        "hist_period": period,
        "am_peak": {"count": round(float(hist[am])), "time": am * 60},
        "pm_peak": {"count": round(float(hist[pm])), "time": pm * 60},
        "hist_by_mode": {k: [round(float(v), 2) for v in tile(h)] for k, h in by_mode.items()},
        "groups": groups,
        "hist_by_group": {k: [round(float(v), 2) for v in tile(h)] for k, h in by_group.items()},
        "boundary": {"name": place, "rings": rings, "holes": holes, "area_km2": round(km2, 1), "bbox_km": bbox,
                     "source": f"stub: Overture division {div_id}", "mask": rle_mask(poly, bbox)},
        "panel": {"side": side, "inside_under": under, "why": "stub"},
        "brands": [dict(meta_brands[b], share=round(share[b], 4)) for b in order],
        "color_by": "brand", "preset": "shorts", "theme": {"batch": "lake"}, "render": {},
        "variants": variants,
        "card": {"title": place.upper(), "templates": {k: TEMPLATES[k] for k in variants}},
    }
    return {"meta": meta, "routes": out_routes, "shapes": out_shapes, "trips": out_trips,
            "hist": [round(float(v), 2) for v in hist]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None)
    ap.add_argument("--no-week", action="store_true")
    args = ap.parse_args()
    ids = args.only.split(",") if args.only else list(CITY)
    recipes = {}
    for cid in CITY:
        with open(os.path.join(FIX, "recipes", f"{cid}.json"), encoding="utf-8") as fh:
            recipes[cid] = json.load(fh)
    with open(os.path.join(FIX, "brands.json"), encoding="utf-8") as fh:
        brands = json.load(fh)
    with gzip.open(os.path.join(ROOT, "data", "gta", "built", "network.json.gz"), "rt", encoding="utf-8") as fh:
        net = json.load(fh)
    pos = positions(net)
    bnds = boundaries(recipes)
    for cid in ids:
        for week in ([False] if args.no_week or "week" not in recipes[cid].get("variants", []) else [False, True]):
            obj = build_city(cid, recipes[cid], net, pos, bnds[cid], brands, week)
            d = os.path.join(OUT, cid, "week" if week else "day")
            os.makedirs(d, exist_ok=True)
            with gzip.open(os.path.join(d, "network.json.gz"), "wt", encoding="utf-8", compresslevel=6) as fh:
                json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
            m = obj["meta"]
            print(f"{cid:18s} {'week' if week else 'day ':4s} trips {len(obj['trips']):6d}  am {m['am_peak']}  "
                  f"groups {[g['id'] for g in m['groups']]}  panel {m['panel']['side']}  credit {m['credit']!r}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
