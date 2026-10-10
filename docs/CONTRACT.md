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
* Attribution: draw every line in `meta.attribution` at 23 px pitch; the panel bottom is the last baseline plus 12 px plus `meta.frame.hud_pad_bottom` (default 0, see v3). The panel top is `meta.frame.hud_top` (default 1120) and every HUD y moves with it, so a city can put the block on its water (the GTA uses 1200).
* `hud_side: "right"` moves the whole panel block right by 400 px (panel x 440..1040, text x 470, sparkline x 470..1010); text stays left-aligned inside the panel.
* Performance target: a GTA frame at the morning peak (roughly 3,000 running vehicles, 60k trips in the file) in under 400 ms including raster in headless Chromium. Trips are sorted by start time, so stop scanning once `t[0] > T`.
* Large frames (`CONFIG.LARGE_FRAME_KM` = 60 km and up) take `CONFIG.LARGE_FRAME`: 12-minute trails, `TRAIL_ALPHA` 0.35, 3 px core, 12 px shoulder at 0.35 on the 5 freshest bands, `TRAIL_BLEND` and `ROUTE_BLEND` `bounded`, `TRAIL_LAYER_ALPHA` 0.65, `ROUTE_ALPHA` 0.2, `BUS_HALO_ALPHA` 0.12, `BUS_HALO_R` 8, `BUS_CORE_R` 2.2. Bounded blending: each mode's trails are stroked with normal alpha into their own layer (so one mode never exceeds its own trail colour), the layer lands on the frame with `lighter` at `TRAIL_LAYER_ALPHA`; the dormant network is unioned per mode at full alpha in a layer and composited once at `ROUTE_ALPHA`, so thirty overlapping shapes are as dim as one. Frames under 60 km keep the additive `add` blend and the values above, so Tsukuba is unchanged. A query knob given explicitly is pinned against the profile; `?knob=0` is honoured.

### Video script

`--city <id>` appends `city=<id>` to the page query and defaults the output to `out/<id>.mp4` (Tsukuba keeps `out/tsukuba-buses.mp4` when no city is given).

## v3: Japan, six videos (Tokyo, Kyoto, Osaka; trains and buses+streetcars)

Everything above still holds. Tsukuba and GTA must render exactly as they
do now (pixel-identical frames for Tsukuba frame 200 against
`/tmp/claude-0/-home-user-bus-map/234234d3-649f-5553-9317-24ec9059d733/scratchpad/render2/frames/frame_00200.png`,
and the GTA network/basemap gz files byte-identical after a rebuild).

### Data situation (why most of this is modelled)

Open timetables in the three cities: Toei only (subway, Arakawa tram,
Nippori-Toneri liner, Toei Bus) plus small ward community buses in Tokyo,
and Daito City's community bus in Osaka. Tokyo Metro, Kyoto City Bus and the
big private bus operators need an ODPT key; JR East and Kyoto's subway are
challenge-only; Osaka Metro, JR West and the private railways publish
nothing. Two open national datasets fill the gaps:

* `data/japan-src/honsu/unkohonsu2026_rosen_kukan.txt` (tab separated, utf-8,
  CC BY 4.0, gtfs-gis.jp, A. Nishizawa): one row per line section with
  `事業者名` (operator), `路線名` (operating line name, e.g. 山手線,
  京浜東北・根岸線), `区間コード`, `起点駅`, `終点駅`, `距離` (m, from geometry),
  `営業キロ`, `順方向運行本数2024` and `逆方向運行本数2024` (weekday trains per
  day in the section's forward and reverse direction; the column name says
  2024 but this is the 2026 edition), `geometry` (WKT LINESTRING, lon lat).
  Sections are cut at stations where trains start or end. Weekday regular
  trains only; trains that need a limited-express or reserved-seat fee are
  excluded. `unkohonsu2026_rosen_eki.txt` has the stations per line with
  points.
* `data/japan-src/n07/n07_11_26.zip` and `n07_11_27.zip`: MLIT 国土数値情報
  バスルート N07, 2011 edition (survey around July 2010), GML (JPGIS 2.1),
  licence non-commercial. Per route feature: `N07_001` bus class code (1
  private route bus, 2 public route bus, 3 community bus, 4 demand bus, 5
  other), `N07_002` operator, `N07_003` route (系統), `N07_004` weekday
  trips per day in one direction (average of the two, real), `N07_005`
  Saturday, `N07_006` Sunday,
  `N07_007` remarks; geometry is a gml:Curve whose posList is **lat lon**
  order. The 2022 edition (`n07_<pref>.zip`) has no trip counts and is not used.

### scripts/model_gtfs.py --area tokyo|kyoto|osaka|all

Reads `cities/japan_model.json` and writes into the area's `gtfs_dir`:

1. Copies of each `real_feeds` zip from `data/japan-src/gtfs/`.
2. For each `shape_real_feeds` entry, a copy of that feed named `out` with a
   `shapes.txt` added and `trips.shape_id` filled: each GTFS route (matched
   by `route_long_name` or `route_short_name` through `route_names`) gets
   one shape per direction built by chaining that operator/line's sections
   from the counts file, oriented to run from the trip's first stop to its
   last stop and trimmed to the stretch between them. Toei's train feed has
   no shapes, and straight station-to-station lines look wrong on a map.
3. `model_rail.zip` (every counted line touching `bbox` except
   `exclude_operators` and `streetcar_lines`) and `model_tram.zip` (only
   `streetcar_lines`). Model, deterministic, no randomness:
   * Per (operator, line): order the sections into chains (a section follows
     another when its 起点駅 equals the previous 終点駅; branches start new
     chains), and orient each WKT so it runs 起点駅 to 終点駅 (compare the
     ends against the station points). A closed chain (its last 終点駅 is
     its first 起点駅) of fewer than 3 sections has each section split at
     the vertex nearest half its length into two legs with the section's
     counts, so no trip stops twice in a row at one stop: the one-section
     ディズニーリゾートライン would otherwise start and end every trip at the
     same stop and its trains would never move. The split point is a timing
     stop named "<起点駅>～<終点駅> 中間点", not a station.
   * Per chain and direction, decompose the per-section counts into service
     patterns by levels: for L = 1..max count, every maximal run of
     consecutive sections with count >= L is one train running that run end
     to end. Merge equal runs into (pattern, number of trains).
   * Departure profile: the per-minute count of first departures of the
     weekday (2026-10-16) trips in `profiles.rail` (Toei's real train GTFS),
     smoothed over 15 minutes. A pattern of running time d may only use the
     profile up to 25:15 - d: the profile is cut there and the pattern's n
     trains take the quantiles of what is left, so the whole day keeps the
     profile's shape and nothing is squeezed into the evening.
   * Departure times, in rounds per chain and direction. With one speed and
     no dwell, two patterns timed apart keep their gap along all the track
     they share, so patterns that share track are timed together. Each
     round takes the leg with the most trains among the patterns not yet
     timed, and the patterns running over it. Each train gets an ideal time
     at that leg: its pattern's cut-profile quantile (k + 0.5)/n,
     k = 0..n-1, plus the running time from the pattern's origin to the
     leg. In the first round the trains, ordered by ideal time, enter the
     leg at the times where the round's summed expected arrivals there
     reach i + phase, i = 0..N-1, with phase in [0,1) from a stable hash of
     (operator, line, chain, direction, leg) so lines do not all depart
     together. In later rounds the trains already timed through the leg
     stay, and the new trains, in the same order, share each gap between
     two of them evenly (before the first and after the last they keep
     their ideal time). The origin departure is the time at the leg minus
     the running time to it, held at the profile's first minute (04:53) or
     at 25:15 - d where it would fall outside, so every departure is at or
     after 04:53 and every arrival at or before 25:15. A pattern alone in
     a round with no earlier trains on its leg thus leaves at its
     cut-profile quantiles (k + phase)/n.
   * Running time: section length / speed, speed by kind: subway or metro
     operator 30 km/h, JR and private railways 34 km/h, monorail and
     automated guideway 27 km/h, streetcar 13 km/h (the Arakawa line's
     average) unless `streetcar_speeds` in `cities/japan_model.json` gives
     the line its own ({"事業者名:路線名": km/h}: 東急電鉄:世田谷線 17,
     京福電気鉄道:嵐山本線 and 北野線 19, 阪堺電気軌道:阪堺線 and 上町線 16).
     Stops at every section end with times proportional to distance (no
     dwell). The shape is the chain's concatenated section geometry.
   * GTFS output: agency per operator (agency_name = 事業者名), route per
     line (route_long_name = 路線名, route_color from `cities/line_colors.json`
     keyed "事業者名:路線名", falling back to a per-operator colour, then grey),
     route_type 0 for streetcars, 1 for subway/metro operators, 2 for
     everything else; one weekday service id with calendar Mon-Fri
     20260101-20271231; feed_info.feed_publisher_name mentions the model.
4. `model_bus.zip` from the N07 2010 file when the area has a `bus` block:
   one route per (operator, 系統). N07_004 is one direction's weekday trips
   (the mean of the two directions: N07's Toei routes match one direction
   of Toei's own GTFS, overnight coaches read 1.0 and many values end in
   .5), so each direction of the curve gets N07_004 rounded (minimum 1)
   trips; 999.9 (unknown) gives 1 trip each way. Departures at the
   quantiles (k + phase)/n of the weekday profile of `profiles.bus` (Toei
   Bus real GTFS), cut at 25:15 minus the running time as for trains, with
   phase from a stable hash of (operator, 系統, feature, direction);
   13 km/h. Dropped, in this order: demand buses (class 4), operators in
   `exclude_operators`, features not touching `bbox`, routes longer than
   60 km, highway coaches and airport buses, features with N07_004 = 0.
   N07 files are cut at the prefecture line, so a long-distance coach
   shows up as a short in-prefecture leg that passes the length test; it
   is dropped when its 系統 matches `highway_route_pattern` or its operator
   matches `highway_operator_pattern` (Python `re.search` on the whole
   N07_003 or N07_002 string, so `^京都交通（株）$` drops that one operator
   and keeps 京阪京都交通（株）).
   route_color "ffffff", route_type 3, agency per operator.
5. A summary per area: lines and sections used, trains per weekday, peak
   trains running and when, buses likewise with the features dropped for
   each reason above, and a list of counted lines in the bbox that got no
   colour from line_colors.json. Unless `--no-verify`, the written zips are
   then read back and checked: trips per section and direction equal the
   count, shape ends lie within 50 m of the first and last stops, trip ids
   are unique, no trip stops twice in a row at one stop, and every trip of
   a shaped feed has a shape with its stops within 150 m. Any failure ends
   the run with a non-zero exit.

### cities/line_colors.json

`{"operators": {"JR東日本": "#...", ...}, "lines": {"JR東日本:山手線": "#80C241", ...}}`
with the official line colours: JR East operating lines (Yamanote, Keihin-
Tohoku, Chuo rapid, Chuo-Sobu local, Saikyo, Shonan-Shinjuku, Tokaido,
Yokosuka/Sobu rapid, Joban, Keiyo, Musashino, Nambu, Yokohama, Tsurumi, ...),
Tokyo Metro, Toei (for the shaped feed's route_color check), Tokyu per line,
Odakyu, Keio, Seibu, Tobu, Keisei, Keikyu, Sotetsu, TX, Rinkai, Yurikamome,
Tokyo Monorail, Tama Monorail, Yokohama Municipal; JR West (Osaka Loop,
JR Kyoto/Kobe line = 東海道線, Hanwa, Yamatoji = 関西線, Gakkentoshi = 片町線,
JR Tozai, Osaka Higashi, Sakurajima, Fukuchiyama, Nara, Kosei, Sagano =
山陰線, ...), Osaka Metro (all nine), Kita-Osaka Kyuko, Kyoto Municipal
Subway, Hankyu, Hanshin, Keihan, Nankai, Kintetsu, Eiden, Randen, Hankai,
Osaka Monorail. Colours must be readable on #07080c (lighten very dark
official colours such as Hibiya grey or Hankyu maroon toward luminance 45%
and say so in a `notes` key).

### City config additions (cities/<video>.json)

    "basemap_city": "tokyo"          // build_basemap writes data/tokyo/built/basemap.json.gz once; every video of the area uses it
    "subtitle": "A modelled weekday"   // shown under the title instead of the date when present
    "include_route_types": [1, 2, 12]     // routes of other types are dropped before anything else (Toei's feed carries both the subway and the tram)
    "color_by": "route"              // static lines, trails and halos take routes[].color; default "mode"
    "group_by": {"field": "agency", "groups": [{"id","label","match": [agency names]}], "default": {"id","label"}}
    "theme": {"accent": "#9ad04a"}   // HUD count, sparkline
    "render": {...}                  // optional CONFIG overrides passed through meta.render
    "frame": {..., "hud_pad_bottom": 36}  // extra panel under the last attribution baseline, default 0
    feeds[].modelled: true           // carried to meta.feeds and routes[].modelled

### network.json additions

    meta.subtitle (from config when present), meta.theme, meta.render, meta.color_by,
    meta.groups = [{"id","label"}] in config order then default,
    meta.hist_by_group = {"jr": [1800], ...}, routes[i].group, routes[i].agency, routes[i].modelled.

`routes[].color` keeps the GTFS route_color (lowercase hex, no #) as today.

### build_basemap.py

With `basemap_city`, write `data/<basemap_city>/built/basemap.json.gz` once
per area. Both videos of an area use the same `origin` (the area origin) and
frame themselves with `frame.center_km`, so the basemap coordinates are
valid for both and the renderer needs no offset. Build the three area
basemaps from `data/<area>/basemap/*.geojson` (Overture; the extraction may
still be running, wait for the file
`/tmp/claude-0/-home-user-bus-map/234234d3-649f-5553-9317-24ec9059d733/scratchpad/ov_japan.done`).
The bus route attributes of N07 2011 are easiest to read from the bundled
shapefile (`.dbf`, cp932) rather than the GML.

### Renderer additions (web/app.js)

* `CITIES` gains the six ids: network `../data/<id>/built/network.json.gz`,
  basemap `../data/<area>/built/basemap.json.gz`.
* `meta.render` overrides CONFIG after the LARGE_FRAME profile and before
  query knobs; `meta.theme.accent` overrides COLORS.accent.
* `color_by: "route"`: the dormant network is drawn per route colour
  (union per colour at full alpha into one layer, composited once at
  ROUTE_ALPHA, so overlaps do not compound); trails are stroked per
  (route colour, band) with normal `source-over` into one layer that lands
  on the frame with `source-over` at TRAIL_LAYER_ALPHA (line colours stay
  true where lines share track); halos use the route colour under a white
  core. Quantise colours to the distinct route colours, not per route.
* HUD with groups: count line `1,204 trains running` (mode label of the
  single mode), breakdown line from `hist_by_group` in group order:
  `412 JR · 380 Metro · 120 Toei · 292 private`, with the same fit-once and
  tabular-digit rules as the mode breakdown. Groups win over modes when both
  exist.
* Subtitle from meta.subtitle.
* `meta.frame.hud_pad_bottom` (default 0) adds that many px to the panel
  under the last attribution baseline. The blur thins the panel's bottom
  edge, so over a dense map the last line needs it; the six Japan videos
  use 36.
* `render.STREETCAR_ON_TOP` (default false): under bounded blending the
  `streetcar` mode's layer lands after every other mode's layer with
  `source-over` instead of `lighter`, so a tram sharing a street with buses
  keeps its colour instead of adding up to white. The three Japan bus
  videos set it; Tsukuba and the GTA leave it off.
* Performance: under 400 ms per frame at the peak for every video.

### Video script

`--city tokyo-trains` etc. as today; outputs `out/<id>.mp4`.
