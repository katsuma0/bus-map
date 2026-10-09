"""Pull Overture Maps roads, rail, water and the city boundary for the Tsukuba
frame straight from the public S3 bucket.

Only the parquet row groups whose bbox statistics touch our frame get read, so
this moves ~100 MB instead of the 70 GB planet. Output is GeoJSON in
data/basemap/, which the renderer loads directly.
"""
import json, re, sys, time, os
import requests
import pyarrow.parquet as pq
import pyarrow.compute as pc
from fsspec.implementations.http import HTTPFileSystem
from shapely import wkb
from shapely.geometry import mapping

RELEASE = os.environ.get("OVERTURE_RELEASE", "2026-09-23.1")
BUCKET = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "basemap")
# West, south, east, north. Wider than the final frame so the layout can move.
BBOX = tuple(float(x) for x in os.environ.get("BBOX", "139.80,35.80,140.40,36.40").split(","))

fs = HTTPFileSystem(client_kwargs={"trust_env": True})

def list_keys(prefix):
    keys, token = [], None
    while True:
        url = f"{BUCKET}?list-type=2&prefix={prefix}&max-keys=1000" + (f"&continuation-token={requests.utils.quote(token)}" if token else "")
        t = requests.get(url, timeout=60).text
        keys += re.findall(r"<Key>([^<]+)</Key>", t)
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", t)
        if not m:
            return keys
        token = m.group(1)

def row_group_hits(pf):
    w, s, e, n = BBOX
    hits = []
    for i in range(pf.metadata.num_row_groups):
        rg = pf.metadata.row_group(i)
        st = {}
        for j in range(rg.num_columns):
            c = rg.column(j)
            if c.path_in_schema.startswith("bbox."):
                st[c.path_in_schema] = c.statistics
        if not st:
            hits.append(i); continue
        if st["bbox.xmin"].min > e or st["bbox.xmax"].max < w or st["bbox.ymin"].min > n or st["bbox.ymax"].max < s:
            continue
        hits.append(i)
    return hits

def pull(theme, type_, columns, keep=None):
    prefix = f"release/{RELEASE}/theme={theme}/type={type_}/"
    keys = list_keys(prefix)
    w, s, e, n = BBOX
    feats = []
    t0 = time.time()
    for k, key in enumerate(keys):
        f = fs.open(BUCKET + key, block_size=8 * 1024 * 1024)
        pf = pq.ParquetFile(f)
        hits = row_group_hits(pf)
        if not hits:
            f.close(); continue
        tbl = pf.read_row_groups(hits, columns=columns + ["bbox", "geometry"])
        bb = tbl.column("bbox")
        xmin = pc.struct_field(bb, "xmin"); xmax = pc.struct_field(bb, "xmax")
        ymin = pc.struct_field(bb, "ymin"); ymax = pc.struct_field(bb, "ymax")
        mask = pc.and_(pc.and_(pc.less_equal(xmin, e), pc.greater_equal(xmax, w)),
                       pc.and_(pc.less_equal(ymin, n), pc.greater_equal(ymax, s)))
        tbl = tbl.filter(mask)
        rows = tbl.to_pylist()
        for r in rows:
            if keep and not keep(r):
                continue
            g = wkb.loads(r.pop("geometry"))
            r.pop("bbox", None)
            props = {c: r.get(c) for c in columns}
            feats.append({"type": "Feature", "properties": props, "geometry": mapping(g)})
        print(f"  {theme}/{type_} file {k+1}/{len(keys)} rg={len(hits)} kept={len(feats)} {time.time()-t0:.0f}s", flush=True)
        f.close()
    return feats

def write(name, feats):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    with open(p, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {p} ({len(feats)} features, {os.path.getsize(p)/1e6:.1f} MB)", flush=True)

if __name__ == "__main__":
    what = sys.argv[1:] or ["segments", "water", "divisions"]
    if "segments" in what:
        def keep(r):
            return r.get("subtype") in ("road", "rail")
        feats = pull("transportation", "segment", ["id", "subtype", "class", "subclass"], keep)
        for ft in feats:
            # names is a nested struct; drop it to keep the file small
            ft["properties"].pop("names", None)
        write("segments.geojson", feats)
    if "water" in what:
        feats = pull("base", "water", ["id", "subtype", "class"])
        write("water.geojson", feats)
    if "divisions" in what:
        feats = pull("divisions", "division_area", ["id", "subtype", "class", "country", "region"])
        write("divisions.geojson", feats)
