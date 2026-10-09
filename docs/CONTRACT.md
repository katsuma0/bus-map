# Data contract and render spec

Everything the renderer draws comes from two JSON files in `data/built/`.
Coordinates in both are kilometres east (x) and north (y) of `origin`
(lon 140.085, lat 36.09, the frame centre). Screen y is flipped by the
renderer. Conversion used by the builders:

    x_km = (lon - 140.085) * 111.32 * cos(36.09°)   # 89.97 km per degree
    y_km = (lat - 36.09)   * 110.574

## data/built/basemap.json  (already built, 5 MB)

    {
      "origin": [140.085, 36.09],
      "km_per_deg": [89.97, 110.574],
      "roads": {
        "major": [[x,y,x,y,...], ...],   // motorway..tertiary, 18k polylines
        "minor": [[x,y,x,y,...], ...],   // residential/unclassified, 67k polylines
        "rail":  [[x,y,x,y,...], ...]    // Tsukuba Express, Joban line etc.
      },
      "water": {
        "poly": [[x,y,x,y,...], ...],    // outer rings of ponds/reservoirs/river polygons
        "holes": [[x,y,...], ...],       // islands: interior rings above the area threshold, only present when there are any; the renderer fills them with the land colour
        "line": [{"c":"river"|"canal"|"stream", "xy":[x,y,...]}, ...]
      },
      "boundary": [[x,y,...]],           // Tsukuba city outline, one ring
      "osm_routes": [{"op":"関東鉄道","name":"...","xy":[x,y,...]}, ...]  // every OSM bus route relation in the area (ODbL); optional faint layer
    }

Flat arrays: `[x0,y0,x1,y1,...]`. Roads only cover lon 139.93–140.24, lat 35.88–36.30.

## data/built/network.json  (produced by scripts/build_network.py)

    {
      "meta": {
        "service_date": "2026-10-09",
        "title": "TSUKUBA BUSES",
        "subtitle": "Friday 9th October",
        "origin": [140.085, 36.09],
        "day_start": 16200,              // seconds since midnight: 04:30
        "day_end": 102600,               // 28:30, i.e. 04:30 next morning
        "feeds": [
          {"id":"tsukubus","name":"つくバス","publisher":"つくば市","license":"CC BY 4.0",
           "version":"tsukubus_20260401","trips_on_date":283}
        ],
        "trips_total": 330,
        "peak": {"count": 31, "time": 30600}
      },
      "routes": [
        {"id":"tsukubus:ROT_000001","short":"H","long":"北部シャトル","color":"6bbc68","feed":"tsukubus"}
      ],
      "shapes": [
        {"xy":[x0,y0,x1,y1,...], "cum":[0, d1, d2, ...]}   // cum = cumulative km along the polyline, one entry per point
      ],
      "trips": [
        {"r": 0, "s": 3, "t": [21600, 21780, ...], "d": [0, 1.42, ...]}
        // r = index into routes, s = index into shapes
        // t = stop times in seconds since midnight, non-decreasing, may exceed 86400
        // d = km along shape at each stop, non-decreasing, same length as t
      ],
      "hist": [0,0,...]   // buses running per minute, index = minutes since 00:00, length 1800 (30 h)
    }

### Position of a bus at time T (seconds)

A trip is running when `t[0] <= T <= t[last]`. Find i with `t[i] <= T < t[i+1]`;
if `t[i+1] == t[i]` the bus dwells at `d[i]`, else
`dist = d[i] + (T - t[i]) / (t[i+1] - t[i]) * (d[i+1] - d[i])`.
Then find k with `cum[k] <= dist <= cum[k+1]` in the shape and interpolate
between points k and k+1. "Buses running" at T = number of running trips.

### Builder rules (scripts/build_network.py)

* Read every `data/gtfs/*.zip`. GTFS-JP files may be `.txt` or `.csv`
  (`trips.csv`, `stop_times.csv`, `shapes.csv`, `calendar.csv`,
  `calendar_dates.csv` all occur). Match on the base name. Encoding is
  UTF-8 with an optional BOM.
* Service date defaults to `2026-10-09` (a Friday). Resolve active
  service_ids with `calendar` (weekday flag and date range) plus
  `calendar_dates` (exception_type 1 adds, 2 removes). A feed may have
  no `calendar` at all (つくばね号 only has calendar_dates).
* Times like `25:10:00` are after midnight and stay > 86400.
* Blank arrival/departure times are interpolated linearly by shape distance
  between the nearest filled times.
* `shape_dist_traveled` is usually absent: project each stop onto its trip's
  shape by nearest point, searching forward from the previous stop's
  position so loops stay monotonic. If a trip has no shape, build one from
  the stop coordinates.
* Expand `frequencies.txt` if present. Skip trips with fewer than 2 stops.
* The default build takes feeds whose agency is in Tsukuba city
  (tsukubus, tsukubane). `--feeds all` also includes tsuchimaru (Tsuchiura).
* Print a summary: trips on date per feed, peak buses and when, first and
  last departure.

## Visual spec (web/index.html + web/app.js)

Frame 1080x1920 (9:16), 30 fps. `render(T)` must be a pure function of T
so Playwright can request frames in any order. No shadowBlur in the hot
path: pre-render glow sprites and the panel backdrop once.

Default map scale: 1920 px covers 36 km north to south (53.33 px/km),
centre 2.2 km west of origin so the network sits in the middle of the frame;
both tunable from the `CONFIG` object at the top of app.js. Every number
below is a CONFIG value or a literal in drawHUD(); the values were measured
against the Melbourne reference frames at 1.875x.

Layers, back to front:

1. Background `#07080c`.
2. Water polygons `#1c1f27`, water lines `#242831`, rivers 2 px, streams 1 px.
3. Minor roads `#23252b` 1 px, major roads `#2e3138` 1.6 px, rail `#2a2d35` dashed 1.2 px.
4. City boundary `#2b2e36` 1 px, 40% alpha.
5. Dormant route network: every GTFS shape in `#4864de` at 13% alpha, 1.8 px, `lighter` compositing. Kept dim so the glow is earned by trails and the map goes dark at night. (`osm_routes` off by default.)
6. Trails: for each bus, the last 25 simulated minutes of its path, split into 16 age bands and stroked once per band (6 px core plus a 22 px soft shoulder on the 7 freshest bands), colour `[120,140,255]` fading from 70% alpha to 0, `lighter`. Red is kept close to green so overlaps go white rather than cyan.
7. Buses: white core 3 px radius plus a halo of radius 11 px at 35% alpha, `lighter`.
8. HUD (plain `source-over`), all y values are canvas pixels:
   * Title `TSUKUBA BUSES`, Montserrat 600, 58 px, letter-spacing 0.20 em, white, centred, baseline 150.
   * Subtitle `Friday 9th October`, Montserrat 400, 38 px, `#8c8f99`, centred, baseline 210.
   * Bottom-left panel: rounded rect x 40..640, y 1120..1620, radius 24, fill `rgba(10,11,16,0.72)`, blurred 36 px once at init so routes passing under its edge fade instead of snapping.
   * Clock `5:32 am` in MontserratTnum 800, 108 px, white, left x 70, baseline 1275 (MontserratTnum is the same file with `font-feature-settings: "tnum"` so the digits keep one pitch).
   * `203 buses running` Montserrat 600 32 px `#ffe066`, baseline 1342.
   * Sparkline x 70..610, y 1380..1500: 35-minute centred moving average of `hist`, filled with a vertical gradient of the accent from 50% to 3% alpha, 3 px accent stroke, drawn only up to the current time, no head marker, a 1 px `#3a3b43` floor line at y 1500.
   * Axis labels `4:30 am` at both ends, Montserrat 500 20 px `#9a9da6`, baseline 1530.
   * Attribution, Inter 400 18 px `#6f737d`, baselines 1562 / 1585 / 1608:
     `Data: Tsukuba City GTFS-JP (CC BY 4.0)`
     `Map: Overture Maps · © OpenStreetMap contributors`
     `Made by Katsuma Onishi`
   * Clock text format: `5:32 am`, `12:07 pm`, `12:45 am`, no leading zero, lowercase am/pm.
9. Timeline: playback progress u in [0, 1] runs over DURATION (48 s = 1440 frames) plus 20 hold frames at the start and 60 at the end. u maps to T through a per-minute weight table: a minute with a bus on the road (or within 30 minutes of one) weighs 1, an empty night minute weighs 0.15, with a linear ramp across the 30-minute margin so the clock never changes speed abruptly. Expose on `window.busmap`: `ready` (Promise), `totalFrames`, `renderFrame(i)` (draws frame i synchronously once ready and returns T), `renderAt(T)`, `frameTime(i)`, `progressAt(T)`, `timeAtProgress(u)`, `config`.
10. Interactive mode (no `?record` in URL): space toggles play at the video's pace, a `<input type=range>` scrubs in progress space, canvas scales to fit the window with CSS. With `?record=1` the canvas is exactly 1080x1920 at the top-left and nothing else is on the page. Query overrides: `?t=hh:mm`, `?frame=N`, `?data=`, `?basemap=`, `?osm=1`, plus trail tuning knobs.

Fonts live in `web/fonts/` as two variable woff2 files with `@font-face` rules in Montserrat.css and Inter.css. Wait for `document.fonts.ready` after loading each face before resolving `busmap.ready`.

Data loading: `fetch('../data/built/basemap.json')` and `fetch('../data/built/network.json')` relative to `web/index.html`; the video script serves the repo root over HTTP so file:// restrictions do not bite.

## v2: city configs, modes, gzip (added for the GTA build)

Everything above still holds for Tsukuba. The second city made three
things configurable instead of hardcoded.

### cities/<id>.json

One file per city, read by both builders (`--city <id>`) and mirrored into
`network.json` meta so the page needs no config of its own.

    {
      "id": "gta", "title": "GTA TRANSIT", "service_date": "2026-10-09",
      "origin": [-79.47, 43.80],                       // lon, lat of the frame centre
      "frame": {"km_vertical": 114, "center_km": [0, 0], "hud_side": "right"},
      "clip": [west, south, east, north],              // basemap clip in degrees
      "gtfs_dir": "data/gta/gtfs", "built_dir": "data/gta/built", "basemap_dir": "data/gta/basemap",
      "gzip": true,                                    // write network.json.gz and basemap.json.gz instead of .json
      "decimals": {"xy": 3, "d": 3},                   // optional output rounding of shape xy and trip d in km (default 3)
      "boundary": null,                                // "tsukuba" keeps the city outline logic, null skips it
      "osm_routes": null,                              // path of an OSM bus-route GeoJSON for the faint optional layer, absent or null skips it
      "feeds": [{"id":"ttc","name":"TTC","publisher":"...","license":"..."}],   // id = zip base name in gtfs_dir
      "optional_feeds": [...],                         // only with --feeds all
      "modes": [{"id":"bus","label":"buses","singular":"bus","route_types":[3,...],"color":[r,g,b],"trail":[r,g,b]}, ...],
      "attribution": ["line 1", "line 2", ...],        // any number of lines, each must fit 540 px at Inter 400 18 px
      "basemap": {"simplify_km": {...}, "min_water_area_km2": 0.05, "min_road_km": 0.08, "water_polys": ["water","lake","pond","reservoir","river"], "water_lines": ["river","canal"], "min_stream_km": 1.0}
    }

`water_polys` lists the Overture water classes kept as polygons (Tsukuba's list
reproduces its pre-v2 rule of everything but pools, wastewater and basins).
A city with `osm_routes` also gives `simplify_km.osm` and `min_osm_km`.
Feeds are processed in zip-name order whatever the config order, so route and
shape indices do not depend on how the feed list is written.

`cities/tsukuba.json` reproduces the current Tsukuba build exactly: with it,
`build_network.py --city tsukuba` must emit the same `routes/shapes/trips/hist`
as today (md5 cd32b16962b0077a56cdaa064210a699 is the pre-v2 file; only meta
and the new per-route `mode` may differ) and `build_basemap.py --city tsukuba`
must emit a byte-identical `data/built/basemap.json` (md5 d58a5eae00cd4270aa9a28ce141a114e).

### network.json additions

    meta.frame        = the config's frame object (km_vertical, center_km, hud_side)
    meta.modes        = the config's modes array, in order
    meta.attribution  = the config's attribution lines
    meta.title        = config title
    meta.hist_by_mode = {"bus": [1800 ints], "streetcar": [...], "rail": [...]}  (same binning as hist)
    routes[i].mode    = mode id, chosen by route_type; a route_type in no mode's list falls into the first mode and is reported in the build summary
    routes[i].feed    = feed id

A trip is kept only if its shape's bounding box touches the 9:16 frame (frame km_vertical and center_km, plus a 1 km margin, intersected with the clip box; the clip box alone when a config has no frame)
(GO trains to Niagara or Kitchener are cut where they leave; a trip entirely
outside is dropped and not counted in hist). `d` is rounded to 3 decimals
(metres) and `xy` to 3 decimals unless the config's `decimals` says otherwise;
`cities/tsukuba.json` keeps its pre-v2 4 because a rounding change moves every
stroke by a fraction of a pixel and the Tsukuba frames must not change.
Everything else as before.

Service date: `--date` defaults to the config's `service_date`. The summary
prints, per feed, trips on that date, and says plainly when a feed has none
(Burlington's feed starts 2026-11-01).

Gzip: when the config says `gzip: true`, the builders write
`<built_dir>/network.json.gz` and `<built_dir>/basemap.json.gz` (gzip level
6, mtime 0 so a no-change rebuild is byte-identical) and no plain file. The page fetches the `.gz` name when
`?city=<id>` names a config with gzip, and inflates with
`new DecompressionStream('gzip')`. The static server must serve `.gz` files
as `application/gzip` without a Content-Encoding header.

### Renderer additions (web/app.js)

* `?city=<id>` picks `../data/<built_dir>/…`; without it the page behaves exactly as now (Tsukuba). The built dir and gzip flag per city are a small table at the top of app.js: `{tsukuba: {dir: '../data/built', gz: false}, gta: {dir: '../data/gta/built', gz: true}}`.
* Frame (`KM_VERTICAL`, `CENTER_KM`) comes from `meta.frame` when present, else CONFIG.
* Title and attribution come from meta when present.
* Modes: static route lines, trails and dot halos take the colour of the route's mode (`meta.modes[...]`); the halo is the mode's `trail` colour under the white 3 px core, and with one mode it stays white as today. With one mode everything renders as today. Ribbon trails keep one Path2D per (mode, band) and stroke per mode.
* HUD with more than one mode: the count line reads `2,058 vehicles running` (thousands separator) and a second line below it, Montserrat 500 24 px `#9a9da6`, reads `1,842 buses · 120 streetcars · 96 trains` at baseline 1374 in mode order using each mode's `label` (singular form when the count is 1); its size is fixed once at init so the widest line the day can produce (every mode at its `hist_by_mode` peak) fits 540 px, and the page warns on the console when it had to shrink. The sparkline, axis labels and attribution shift down by 30 px when that line is present. With one mode the HUD is unchanged (`24 buses running`).
* Attribution: draw every line in `meta.attribution` at 23 px pitch; the panel bottom is the last baseline plus 12 px. The panel top is `meta.frame.hud_top` (default 1120) and every HUD y moves with it, so a city can put the block on its water (the GTA uses 1200).
* `hud_side: "right"` moves the whole panel block right by 400 px (panel x 440..1040, text x 470, sparkline x 470..1010); text stays left-aligned inside the panel.
* Performance target: a GTA frame at the morning peak (roughly 3,000 running vehicles, 60k trips in the file) in under 400 ms including raster in headless Chromium. Trips are sorted by start time, so stop scanning once `t[0] > T`.
* Large frames (`CONFIG.LARGE_FRAME_KM` = 60 km and up) take `CONFIG.LARGE_FRAME`: 12-minute trails, `TRAIL_ALPHA` 0.35, 3 px core, 12 px shoulder at 0.35 on the 5 freshest bands, `TRAIL_BLEND` and `ROUTE_BLEND` `bounded`, `TRAIL_LAYER_ALPHA` 0.65, `ROUTE_ALPHA` 0.2, `BUS_HALO_ALPHA` 0.12, `BUS_HALO_R` 8, `BUS_CORE_R` 2.2. Bounded blending: each mode's trails are stroked with normal alpha into their own layer (so one mode never exceeds its own trail colour), the layer lands on the frame with `lighter` at `TRAIL_LAYER_ALPHA`; the dormant network is unioned per mode at full alpha in a layer and composited once at `ROUTE_ALPHA`, so thirty overlapping shapes are as dim as one. Frames under 60 km keep the additive `add` blend and the values above, so Tsukuba is unchanged. A query knob given explicitly is pinned against the profile; `?knob=0` is honoured.

### Video script

`--city <id>` appends `city=<id>` to the page query and defaults the output to `out/<id>.mp4` (Tsukuba keeps `out/tsukuba-buses.mp4` when no city is given).
