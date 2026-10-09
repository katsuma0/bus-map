"""Print what each GTFS zip in a directory covers: feed_info dates, calendar
and calendar_dates ranges, route types, and the stop bounding box. Run it
before picking a service date so no feed is silently out of range.
"""
import csv, io, os, sys, zipfile, collections

def table(zf, base):
    for n in zf.namelist():
        if n.split("/")[-1].split(".")[0] == base:
            return list(csv.DictReader(io.TextIOWrapper(zf.open(n), encoding="utf-8-sig")))
    return []

def main(d):
    for z in sorted(os.listdir(d)):
        if not z.endswith(".zip"):
            continue
        try:
            zf = zipfile.ZipFile(os.path.join(d, z))
        except zipfile.BadZipFile:
            print(f"{z:<16} BAD ZIP")
            continue
        fi = table(zf, "feed_info")
        cal = table(zf, "calendar")
        cd = table(zf, "calendar_dates")
        routes = table(zf, "routes")
        stops = table(zf, "stops")
        types = collections.Counter(r.get("route_type") for r in routes)
        rng = [(c["start_date"], c["end_date"]) for c in cal]
        dates = sorted(c["date"] for c in cd)
        lat = [float(s["stop_lat"]) for s in stops if s.get("stop_lat")]
        lon = [float(s["stop_lon"]) for s in stops if s.get("stop_lon")]
        info = (fi[0].get("feed_start_date"), fi[0].get("feed_end_date")) if fi else None
        print(f"{z:<16} feed_info={info} calendar={min(r[0] for r in rng) if rng else '-'}..{max(r[1] for r in rng) if rng else '-'} "
              f"calendar_dates={dates[0] if dates else '-'}..{dates[-1] if dates else '-'} routes={len(routes)} types={dict(types)} "
              f"stops={len(stops)} bbox={min(lat):.2f}..{max(lat):.2f} {min(lon):.2f}..{max(lon):.2f}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/gtfs")
