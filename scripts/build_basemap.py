"""Boil the raw Overture GeoJSON down to the flat arrays the renderer draws.

Coordinates come out in kilometres east/north of ORIGIN (the frame centre), so
the page only needs a scale and an offset to put them on screen. Roads are
split into major/minor/rail, water into polygons and lines, and only the
Tsukuba city outline is kept from the divisions layer.
"""
import json, os, math
from shapely.geometry import shape, box, mapping
from shapely.ops import transform

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "data", "basemap")
OUT = os.path.join(HERE, "..", "data", "built")
ORIGIN = (140.085, 36.09)
# Generous clip so the frame can still be nudged in the renderer.
CLIP = box(139.93, 35.88, 140.24, 36.30)

KX = 111.32 * math.cos(math.radians(ORIGIN[1]))
KY = 110.574

def to_km(x, y, z=None):
    return ((x - ORIGIN[0]) * KX, (y - ORIGIN[1]) * KY)

MAJOR = {"motorway", "trunk", "primary", "secondary", "tertiary"}
MINOR = {"residential", "unclassified", "living_street"}

def lines_of(geom):
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return list(geom.geoms)
    return []

def polys_of(geom):
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    return []

def flat(coords, nd=3):
    out = []
    for x, y in coords:
        out.append(round(x, nd)); out.append(round(y, nd))
    return out

def load(name):
    with open(os.path.join(RAW, name)) as fh:
        return json.load(fh)["features"]

def build():
    roads = {"major": [], "minor": [], "rail": []}
    for f in load("segments.geojson"):
        p = f["properties"]
        if p["subtype"] == "rail":
            if p["class"] not in ("standard_gauge", "unknown"):
                continue
            bucket, tol = "rail", 0.004
        elif p["class"] in MAJOR:
            bucket, tol = "major", 0.003
        elif p["class"] in MINOR:
            bucket, tol = "minor", 0.005
        else:
            continue
        g = shape(f["geometry"]).intersection(CLIP)
        if g.is_empty:
            continue
        g = transform(to_km, g).simplify(tol)
        for ln in lines_of(g):
            if ln.length < 0.02:
                continue
            roads[bucket].append(flat(ln.coords))

    water = {"poly": [], "line": []}
    for f in load("water.geojson"):
        p = f["properties"]
        g = shape(f["geometry"])
        if g.geom_type in ("Polygon", "MultiPolygon"):
            if p["class"] in ("swimming_pool", "wastewater", "basin") :
                continue
            g = g.intersection(CLIP)
            if g.is_empty:
                continue
            g = transform(to_km, g).simplify(0.004)
            for pg in polys_of(g):
                if pg.area < 0.004:  # under 0.4 ha reads as noise at this scale
                    continue
                water["poly"].append(flat(pg.exterior.coords))
        else:
            if p["class"] not in ("river", "canal", "stream"):
                continue
            g = g.intersection(CLIP)
            if g.is_empty:
                continue
            g = transform(to_km, g).simplify(0.006)
            for ln in lines_of(g):
                if ln.length < 0.3 and p["class"] == "stream":
                    continue
                water["line"].append({"c": p["class"], "xy": flat(ln.coords)})

    boundary = []
    for f in load("divisions.geojson"):
        p = f["properties"]
        if p["subtype"] != "locality":
            continue
        g = shape(f["geometry"])
        b = g.bounds
        # Tsukuba city: the one locality that spans Mt Tsukuba down to the
        # Ushiku border, roughly 139.99-140.18 E, 35.94-36.24 N.
        if abs(b[0] - 139.996) < 0.01 and abs(b[3] - 36.237) < 0.01:
            g = transform(to_km, g).simplify(0.01)
            for pg in polys_of(g):
                boundary.append(flat(pg.exterior.coords))

    os.makedirs(OUT, exist_ok=True)
    out = {"origin": list(ORIGIN), "km_per_deg": [KX, KY], "roads": roads, "water": water, "boundary": boundary}
    path = os.path.join(OUT, "basemap.json")
    with open(path, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print({k: len(v) for k, v in roads.items()}, {k: len(v) for k, v in water.items()}, "boundary rings", len(boundary))
    print(f"wrote {path} {os.path.getsize(path)/1e6:.1f} MB")

if __name__ == "__main__":
    build()
