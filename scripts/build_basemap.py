"""Boil the raw Overture GeoJSON down to the flat arrays the renderer draws.

Coordinates come out in kilometres east/north of the city's origin (the frame
centre), so the page only needs a scale and an offset to put them on screen.
Roads are split into major/minor/rail, water into polygons and lines. The
optional city outline and the optional OSM bus-route layer only exist for
cities whose config asks for them.

    python3 scripts/build_basemap.py [--city tsukuba|gta]

Everything city-specific (origin, clip box, directories, tolerances, which
water classes count) lives in cities/<id>.json so one script serves every city.
"""
import argparse
import gzip
import json
import math
import os
import time

import numpy as np
import shapely
from shapely.geometry import shape, box

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))

MAJOR = {"motorway", "trunk", "primary", "secondary", "tertiary"}
MINOR = {"residential", "unclassified", "living_street"}
# Subway, tram and light rail are drawn by the transit network layer, which
# animates vehicles on them; only heavy rail belongs to the base map.
RAIL = {"standard_gauge", "unknown"}


def load_city(city_id):
    with open(os.path.join(ROOT, "cities", f"{city_id}.json"), encoding="utf-8") as fh:
        return json.load(fh)


class Proj:
    """Equirectangular degrees -> km about the origin, matching the renderer."""

    def __init__(self, origin):
        self.origin = np.array(origin, dtype=float)
        self.k = np.array([111.32 * math.cos(math.radians(origin[1])), 110.574])

    def __call__(self, geom):
        return shapely.transform(geom, lambda c: (c - self.origin) * self.k)


def lines_of(geom):
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        return [g for part in geom.geoms for g in lines_of(part)]
    return []


def polys_of(geom):
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        return [g for part in geom.geoms for g in polys_of(part)]
    return []


def flat(coords, nd=3):
    out = []
    for x, y in coords:
        out.append(round(x, nd))
        out.append(round(y, nd))
    return out


def load_features(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["features"]


def build_roads(feats, clip, proj, cfg):
    tol, min_len = cfg["simplify_km"], cfg["min_road_km"]
    roads = {"major": [], "minor": [], "rail": []}
    for f in feats:
        p = f["properties"]
        if p["subtype"] == "rail":
            if p["class"] not in RAIL:
                continue
            bucket = "rail"
        elif p["class"] in MAJOR:
            bucket = "major"
        elif p["class"] in MINOR:
            bucket = "minor"
        else:
            continue
        g = shape(f["geometry"]).intersection(clip)
        if g.is_empty:
            continue
        g = proj(g).simplify(tol[bucket])
        for ln in lines_of(g):
            if ln.length < min_len:
                continue
            roads[bucket].append(flat(ln.coords))
    return roads


def build_water(feats, clip, proj, cfg):
    tol = cfg["simplify_km"]
    poly_classes = set(cfg["water_polys"])
    line_classes = set(cfg["water_lines"])
    water = {"poly": [], "line": []}
    for f in feats:
        p = f["properties"]
        gtype = f["geometry"]["type"]
        if gtype in ("Polygon", "MultiPolygon"):
            if p["class"] not in poly_classes:
                continue
            g = shape(f["geometry"]).intersection(clip)
            if g.is_empty:
                continue
            g = proj(g).simplify(tol["water"])
            for pg in polys_of(g):
                # Below the threshold a pond is a speck at frame scale.
                if pg.area < cfg["min_water_area_km2"]:
                    continue
                water["poly"].append(flat(pg.exterior.coords))
        elif gtype in ("LineString", "MultiLineString"):
            if p["class"] not in line_classes:
                continue
            g = shape(f["geometry"]).intersection(clip)
            if g.is_empty:
                continue
            g = proj(g).simplify(tol["water_line"])
            for ln in lines_of(g):
                if p["class"] == "stream" and ln.length < cfg["min_stream_km"]:
                    continue
                water["line"].append({"c": p["class"], "xy": flat(ln.coords)})
    return water


def tsukuba_boundary(feats, proj):
    """Tsukuba city is the one locality spanning Mt Tsukuba down to the Ushiku
    border, roughly 139.99-140.18 E, 35.94-36.24 N; the bounds pick it out
    because Overture localities carry no stable name field we can key on."""
    boundary = []
    for f in feats:
        if f["properties"]["subtype"] != "locality":
            continue
        g = shape(f["geometry"])
        b = g.bounds
        if abs(b[0] - 139.996) < 0.01 and abs(b[3] - 36.237) < 0.01:
            g = proj(g).simplify(0.01)
            for pg in polys_of(g):
                boundary.append(flat(pg.exterior.coords))
    return boundary


def build_osm_routes(feats, clip, proj, cfg):
    """Every OSM bus route relation as a faint optional layer (ODbL)."""
    out = []
    for f in feats:
        p = f["properties"]
        g = shape(f["geometry"]).intersection(clip)
        if g.is_empty:
            continue
        g = proj(g).simplify(cfg["simplify_km"]["osm"])
        for ln in lines_of(g):
            if ln.length < cfg["min_osm_km"]:
                continue
            out.append({"op": p.get("operator") or p.get("network") or "", "name": p.get("name", ""), "xy": flat(ln.coords)})
    return out


def build(city):
    t0 = time.time()
    cfg = city["basemap"]
    raw = os.path.join(ROOT, city["basemap_dir"])
    out_dir = os.path.join(ROOT, city["built_dir"])
    proj = Proj(city["origin"])
    clip = box(*city["clip"])

    roads = build_roads(load_features(os.path.join(raw, "segments.geojson")), clip, proj, cfg)
    print(f"roads {({k: len(v) for k, v in roads.items()})}  {time.time() - t0:.0f}s", flush=True)
    water = build_water(load_features(os.path.join(raw, "water.geojson")), clip, proj, cfg)
    print(f"water {({k: len(v) for k, v in water.items()})}  {time.time() - t0:.0f}s", flush=True)

    boundary = []
    if city.get("boundary") == "tsukuba":
        boundary = tsukuba_boundary(load_features(os.path.join(raw, "divisions.geojson")), proj)
    elif city.get("boundary"):
        raise SystemExit(f"unknown boundary rule {city['boundary']!r}; only 'tsukuba' or null are implemented")

    out = {"origin": list(city["origin"]), "km_per_deg": proj.k.tolist(), "roads": roads, "water": water, "boundary": boundary}
    if city.get("osm_routes"):
        out["osm_routes"] = build_osm_routes(load_features(os.path.join(ROOT, city["osm_routes"])), clip, proj, cfg)
        print(f"osm_routes {len(out['osm_routes'])}  {time.time() - t0:.0f}s", flush=True)

    os.makedirs(out_dir, exist_ok=True)
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    if city.get("gzip"):
        path = os.path.join(out_dir, "basemap.json.gz")
        with open(path, "wb") as fh:
            fh.write(gzip.compress(text.encode("utf-8"), compresslevel=6))
    else:
        path = os.path.join(out_dir, "basemap.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    print({k: len(v) for k, v in roads.items()}, {k: len(v) for k, v in water.items()}, "boundary rings", len(boundary),
          "osm_routes", len(out.get("osm_routes", [])))
    print(f"wrote {path} {os.path.getsize(path) / 1e6:.1f} MB (json {len(text.encode('utf-8')) / 1e6:.1f} MB) in {time.time() - t0:.0f}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="tsukuba", help="city id, reads cities/<id>.json (default tsukuba)")
    args = ap.parse_args()
    build(load_city(args.city))


if __name__ == "__main__":
    main()
