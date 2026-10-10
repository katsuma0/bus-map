"""City boundaries from Overture divisions, with the names the base map extract lacks.

    python3 -I scripts/fetch_boundary.py fetch  --release 2026-09-23.1 --country CA --region CA-ON \
            --bbox -80.12,43.01,-78.52,44.30 --out cache/overture/2026-09-23.1/divisions/CA-ON.geojson
    python3 -I scripts/fetch_boundary.py select --in cache/overture/2026-09-23.1/divisions/CA-ON.geojson \
            --name Markham --subtypes locality --area-km2 210.93 --out build/gta-markham/boundary.geojson

    python3 -I scripts/fetch_boundary.py union --in build/gta-trains/boundary.parts/0.geojson \
            --in build/gta-trains/boundary.parts/1.geojson ... --out build/gta-trains/boundary.geojson

`fetch` runs once per area. It reads the division_area parquet files of the
release with the row-group bbox pruning of fetch_overture.py and keeps land
localities, localadmins and counties of one country and region that carry a
primary name. `select` picks one city from that file by its exact name and
census area, and writes it as a single GeoJSON Feature for trim_network.py.
The selected Feature's properties are also printed as one JSON line on stdout.
`union` merges Features that `select` (or a recipe's own file) wrote into one
Feature, for a video whose boundary is several divisions (the GTA is Toronto
and the regions of Peel, York, Durham and Halton).
"""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# -I leaves the script directory off sys.path; the sibling modules are ours.
sys.path.insert(0, HERE)

SUBTYPES = ("locality", "localadmin", "county")
MAX_AREA_ERROR = 0.20


def area_km2(geom):
    """Equirectangular area at the latitude of the bbox centre, as the renderer projects."""
    import shapely
    w, s, e, n = geom.bounds
    lat = (s + n) / 2
    k = (111.32 * math.cos(math.radians(lat)), 110.574)
    return float(shapely.transform(geom, lambda c: c * k).area)


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def fetch(args):
    from shapely.geometry import shape

    bbox = tuple(float(x) for x in args.bbox.split(","))
    if len(bbox) != 4:
        sys.exit("--bbox needs west,south,east,north")
    # fetch_overture reads its release and box from the environment at import;
    # setting both first keeps its pruning code exactly as it is.
    os.environ["OVERTURE_RELEASE"] = args.release
    os.environ["BBOX"] = ",".join(str(x) for x in bbox)
    import fetch_overture as fo
    fo.RELEASE = args.release
    fo.BBOX = bbox

    def keep(r):
        names = r.get("names") or {}
        return (r.get("country") == args.country and r.get("region") == args.region and r.get("class") == "land"
                and r.get("subtype") in SUBTYPES and bool((names.get("primary") or "").strip()))

    feats = fo.pull("divisions", "division_area", ["id", "division_id", "names", "subtype", "class", "country", "region"], keep)
    out = []
    for f in feats:
        p = f["properties"]
        g = shape(f["geometry"])
        out.append({"type": "Feature", "properties": {
            "id": p["id"], "division_id": p.get("division_id"), "name": p["names"]["primary"].strip(),
            "subtype": p["subtype"], "class": p["class"], "country": p["country"], "region": p["region"],
            "area_km2": round(area_km2(g), 2), "release": args.release,
        }, "geometry": f["geometry"]})
    # Parquet file and row-group order is not part of the contract; ids are.
    out.sort(key=lambda f: f["properties"]["id"])
    write_json(args.out, {"type": "FeatureCollection", "features": out})
    print(f"wrote {args.out}: {len(out)} divisions ({', '.join(f'{s} {sum(1 for f in out if f['properties']['subtype'] == s)}' for s in SUBTYPES)})",
          file=sys.stderr)


def choose(features, name, subtypes, target_km2):
    """The rule of A7: exact name, first subtype in the list with candidates, closest area."""
    for st in subtypes:
        cands = [f for f in features if f["properties"].get("name") == name and f["properties"].get("subtype") == st]
        if cands:
            cands.sort(key=lambda f: (abs(f["properties"]["area_km2"] - target_km2), f["properties"]["id"]))
            best = cands[0]
            err = abs(best["properties"]["area_km2"] - target_km2) / target_km2
            if err > MAX_AREA_ERROR:
                raise ValueError(f"{name!r} ({st}) has {best['properties']['area_km2']} km2, "
                                 f"{err:.0%} from the census {target_km2} km2 (limit {MAX_AREA_ERROR:.0%})")
            return best
    found = sorted({f["properties"].get("subtype") for f in features if f["properties"].get("name") == name})
    raise ValueError(f"no division named {name!r} with subtype in {list(subtypes)}"
                     + (f" (that name exists as {found})" if found else ""))


def select(args):
    with open(args.inp, encoding="utf-8") as fh:
        fc = json.load(fh)
    subtypes = [s.strip() for s in args.subtypes.split(",") if s.strip()] or list(SUBTYPES)
    try:
        best = choose(fc["features"], args.name, subtypes, args.area_km2)
    except ValueError as e:
        sys.exit(f"select: {e}")
    props = dict(best["properties"])
    props["source"] = f"Overture {props.get('release', '?')} division_area {props['id']}"
    feat = {"type": "Feature", "properties": props, "geometry": best["geometry"]}
    write_json(args.out, feat)
    from shapely.geometry import shape
    props_out = dict(props)
    props_out["bbox"] = [round(v, 6) for v in shape(best["geometry"]).bounds]
    print(json.dumps(props_out, ensure_ascii=False, sort_keys=True))
    print(f"{args.name}: {props['subtype']} {props['id']}, {props['area_km2']} km2 (census {args.area_km2})", file=sys.stderr)


def union(args):
    import hashlib

    import shapely
    from shapely.geometry import mapping, shape

    parts = []
    for path in args.inp:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        if obj.get("type") == "FeatureCollection":
            if len(obj.get("features", [])) != 1:
                sys.exit(f"union: {path} must hold exactly one Feature")
            obj = obj["features"][0]
        parts.append(obj)
    if len(parts) < 2:
        sys.exit("union: needs at least two --in Features")
    geoms = [shape(p["geometry"]) for p in parts]
    # Neighbouring divisions share their border vertices, so the union has no
    # slivers along it; a real gap between parts stays as it is.
    geom = shapely.union_all(geoms)
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        sys.exit(f"union: the parts make a {geom.geom_type}, not a polygon")
    props = [p.get("properties") or {} for p in parts]
    ids = [str(q.get("id")) for q in props]
    releases = sorted({q["release"] for q in props if q.get("release")})
    keep = ("id", "division_id", "name", "subtype", "area_km2", "release", "source")
    out = {
        # The parts keep their own ids; the union's id only has to be stable and short.
        "id": "union:" + hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest()[:16],
        "division_id": None,
        "name": " + ".join(str(q.get("name")) for q in props),
        "subtype": "union",
        "area_km2": round(area_km2(geom), 2),
        "parts": [{k: q[k] for k in keep if k in q} for q in props],
    }
    if len(releases) == 1 and all(q.get("release") for q in props):
        out["release"] = releases[0]
        out["source"] = f"Overture {releases[0]} division_area " + " + ".join(ids)
    else:
        out["source"] = " + ".join(str(q.get("source") or q.get("id")) for q in props)
    feat = {"type": "Feature", "properties": out, "geometry": mapping(geom)}
    write_json(args.out, feat)
    props_out = dict(out)
    props_out["bbox"] = [round(v, 6) for v in geom.bounds]
    print(json.dumps(props_out, ensure_ascii=False, sort_keys=True))
    print(f"union of {len(parts)}: {out['name']}, {out['area_km2']} km2", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="divisions of one region from Overture")
    f.add_argument("--release", required=True)
    f.add_argument("--country", required=True)
    f.add_argument("--region", required=True)
    f.add_argument("--bbox", required=True, help="west,south,east,north")
    f.add_argument("--out", required=True)
    s = sub.add_parser("select", help="one city out of a fetched divisions file")
    s.add_argument("--in", dest="inp", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--subtypes", default=",".join(SUBTYPES), help="comma list in preference order")
    s.add_argument("--area-km2", type=float, required=True)
    s.add_argument("--out", required=True)
    u = sub.add_parser("union", help="one Feature out of several selected ones")
    u.add_argument("--in", dest="inp", action="append", required=True, help="a Feature file; give two or more")
    u.add_argument("--out", required=True)
    # A box that starts west of Greenwich ("--bbox -80.12,...") looks like an
    # option to argparse; glue it to its flag so the A2 command line works.
    argv, rest = [], list(sys.argv[1:])
    while rest:
        a = rest.pop(0)
        argv.append("--bbox=" + rest.pop(0) if a == "--bbox" and rest else a)
    args = ap.parse_args(argv)
    if args.cmd == "fetch":
        fetch(args)
    elif args.cmd == "union":
        union(args)
    else:
        select(args)


if __name__ == "__main__":
    main()
