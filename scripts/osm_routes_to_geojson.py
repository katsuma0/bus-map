"""Turn an Overpass `relation[route=bus]; out body geom;` reply into a compact
GeoJSON of one MultiLineString per route relation.

Only the member ways' geometry survives. Stops, platforms and per-node tags are
dropped because the renderer only needs the line.
"""
import json, sys

src, dst = sys.argv[1], sys.argv[2]
data = json.load(open(src, encoding="utf-8"))
feats = []
for el in data.get("elements", []):
    if el.get("type") != "relation":
        continue
    tags = el.get("tags", {})
    lines = []
    for m in el.get("members", []):
        if m.get("type") != "way" or not m.get("geometry"):
            continue
        if m.get("role", "") not in ("", "forward", "backward", "route"):
            continue  # platforms / stops carry roles like "platform", "stop"
        coords = [[round(p["lon"], 6), round(p["lat"], 6)] for p in m["geometry"]]
        if len(coords) >= 2:
            lines.append(coords)
    if not lines:
        continue
    keep = {k: tags.get(k) for k in ("name", "ref", "operator", "network", "from", "to", "colour") if tags.get(k)}
    keep["osm_id"] = el["id"]
    feats.append({"type": "Feature", "properties": keep,
                  "geometry": {"type": "MultiLineString", "coordinates": lines}})
with open(dst, "w", encoding="utf-8") as fh:
    json.dump({"type": "FeatureCollection", "features": feats}, fh, ensure_ascii=False, separators=(",", ":"))
print(f"{len(feats)} bus route relations -> {dst}")
