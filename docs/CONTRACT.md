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

## v4: Shorts (recipes, batches, area stores, network v4, page knobs)

v4 networks carry `"meta": {"schema": 4, ...}` and only reach new code: the
legacy configs above have no `schema`, their scripts are untouched, and the page
takes the v4 path only for a schema 4 file. This section copies the contract
parts of the Shorts spec (revision 2, sections 2, A3, B9 and B10) so the repo
carries the formats its scripts read and write. `scripts/make.py` implements
2.14 and the derivations behind 2.7; `cities/batches/README.md` describes the
recipe, batch and lock files with their validation rules.

Additions made while implementing (they extend, never change, the formats below):

* a fixture-only feed source `{"path": "data/gtfs/<zip>"}`: a zip committed on the batch's branch
  (the `test` batch uses it, so it needs no tag, URL or key);
* the lock may carry `areas.<area>.rules` (the composite rule per feed and day class, next to
  `dates`), `areas.<area>.divisions.bbox`, and `boundaries.<id>.week_eligible: false` with
  `week_why` after a week trim exits 3; the divisions file is
  `cache/overture/<release>/divisions/<region>-<area>.geojson`, one per area (`sha256` null
  when every boundary of the area is a `boundary.file`);
* `<stem>.netmeta.json` also carries `month`, `timeline`, `window`, `modes`, `frames` and
  `trips_total`, and `<stem>.meta.json` also carries `licence_flags` (the CSV column);
* make.py passes each step key to the builders as `--key` (the trim writes it to `meta.build_key`);
* tuning (G) has a 23rd knob, `outside` (`OUTSIDE_DIM`, day pass, arms v - 0.2 and v + 0.2 capped
  at 0.95), and a G4 score `card_cover` for the card knobs: the share of on-screen inside vehicles
  in the rows of the frame-0 card band (card text extent +- 60 px), hard limit 0.4; for it the tune
  tier also writes `vehicles-f<nnnn>.json` for each frame it renders;
* release asset labels read `<file> key:<16 hex>` (GitHub lists the label in place of the file
  name), and a video counts as done only when its MP4, `.json` and `.netmeta.json` all carry its
  render key; `--upload` sends the MP4 last.
* a slow camera over the map layers (B18 below): the CONFIG keys `CAMERA*`, the recipe key
  `variety.camera`, the busmap members `camera`, `cameraAt(u)` and `setCamera(u)`, and a 24th
  tuning knob `camamp` (`CAMERA_AMP`, right after the frame knobs in every pass, arms v x 0.75 and
  v x 1.25 capped at 1.5, with a clip, `motion.jpg` and `camera.jpg` of frames 0, N/4, N/2 and
  3N/4; auto pick b unless an arm strobes less by more than 0.0005).

### 2. Shared interfaces

Types: `str`, `int`, `float`, `bool`, `[x, y]` arrays, `obj`. Unknown keys are an error in recipes,
batch files and locks (make.py validates), and a console warning in the page (as `applyRender`
does today).

#### 2.1 Recipe: `cities/recipes/<id>.json`

```json
{
  "id": "gta-markham",
  "batch": "gta",
  "area": "gta",
  "place": "Markham",
  "boundary": {"name": "Markham", "subtypes": ["locality"], "area_km2": 210.93},
  "center": [-79.299, 43.880],
  "variants": ["day", "rush", "week"],
  "rush": {"frame": null, "auto": true},
  "variety": {"panel_side": "right", "card_line": 0, "zoom": 1.05},
  "override": {
    "render": {"BUS_HALO_R": 12},
    "variant_render": {"day": {"TRAIL_MINUTES": 10}},
    "brand_colors": {},
    "_why": {"BUS_HALO_R": "11 px dots merged on Highway 7; 12 px keeps them round",
             "day.TRAIL_MINUTES": "8 min left gaps on Highway 7 at the peak; 12 min fused Viva and YRT"}
  }
}
```

| key | type | required | default | meaning |
|---|---|---|---|---|
| `id` | str `^[a-z0-9][a-z0-9-]{1,39}$` | yes | | also the output name stem |
| `batch` | str | yes | | `cities/batches/<batch>.json` must list this id |
| `area` | str | no | the batch's only area | required when the batch has several areas |
| `place` | str | yes | | name in title, card and count line ("412 vehicles in Markham") |
| `boundary.name` | str | yes | | Overture `names.primary`, exact |
| `boundary.subtypes` | [str] | no | `["locality", "localadmin", "county"]` | preference order |
| `boundary.area_km2` | float | yes | | census land area; the match must be within 20% |
| `boundary.file` | str | no | | a GeoJSON Feature used instead of Overture (fixtures, or a city Overture lacks) |
| `center` | [lon, lat] | no | boundary bbox centre | documentation and a fit fallback |
| `frame.km_vertical`, `frame.center_km` | float, [float, float] | no | boundary fit (D3.2) | km about the area origin; pins the day frame |
| `variants` | [str] | no | batch `variants_default` | subset of `day`, `rush`, `week`; `week` is dropped when not eligible (A3.6) |
| `rush.frame` | obj or null | no | null | a pinned closer frame for the rush; must lie inside the day trim box |
| `rush.auto` | bool | no | true | with `rush.frame` null, A computes a closer frame (A8.6); false = the day frame |
| `variety.panel_side` | `left` / `right` | no | `left` | preference; the side is chosen by A8.7 and this breaks ties within 10% |
| `variety.card_line` | int 0..2 | no | 0 | index into the card templates; a `cardline` tuning pick writes here |
| `variety.zoom` | float | no | 1.0 | multiplies FRAME_ZOOM (D3.8) |
| `variety.camera` | one of the six B18 paths, or `off` | no | the batch pick (B18, `camera_paths`) | the camera move, the same in every variant; D3.8 passes it as `CAMERA_PATH` (`off`: `CAMERA: false`) |
| `override.render` | obj of CONFIG keys | no | {} | for every variant; never a key that any `defaults.variants.*.render` block sets (validation error) |
| `override.variant_render` | obj variant -> obj | no | {} | per variant, merged after `override.render` |
| `override.brand_colors` | obj brand id -> hex | no | {} | passed as `brandhex=` |
| `override._why` | obj | no | {} | `KEY` or `<variant>.KEY` -> one line from the judge |

#### 2.2 Batch file: `cities/batches/<batch>.json`

```json
{
  "batch": "gta",
  "title": "GTA top 10",
  "theme": "lake",
  "month": "2026-10",
  "overture_release": "2026-09-23.1",
  "render_epoch": 0,
  "areas": [{
    "id": "gta",
    "origin": [-79.47, 43.80],
    "timezone": "America/Toronto",
    "country": "CA",
    "region": "CA-ON",
    "holidays": "CA-ON",
    "area_box": [-80.06, 43.06, -78.58, 44.25],
    "feeds": [
      {"id": "ttc", "name": "TTC", "publisher": "Toronto Transit Commission",
       "licence_id": "ogl-toronto", "licence_text": "Open Government Licence - Toronto",
       "source": {"tag": "feeds/gta-2026-10-09", "path": "gtfs/ttc.zip"}}
    ]
  }],
  "modes": [{"id": "bus", "label": "buses", "singular": "bus", "route_types": [3, 700, 701, 702, 704, 11]}],
  "cities": ["gta-toronto", "gta-mississauga"],
  "variants_default": ["day", "rush", "week"],
  "hashtags": ["#shorts", "#transit", "#gta"],
  "category": "Travel & Events",
  "release": {"tag": "shorts-gta", "name": "Shorts: GTA top 10"}
}
```

| key | type | meaning |
|---|---|---|
| `theme` | `lake` / `ink` / `sodium` / `slate` / `teal` | one theme per batch (decision 5) |
| `month` | `YYYY-MM` | composite month; an area may override it with its own `month` |
| `render_epoch` | int | part of every render key; bump it to force every render (an empty commit does not trigger the workflow because of its `paths` filter) |
| `areas[].origin` | [lon, lat] | projection origin of every network and basemap in the area. One origin per area keeps the equirectangular x scale right (a single US origin at 35N would be about 8% off at both New York and San Antonio) |
| `areas[].timezone`, `country`, `region`, `holidays` | str | `holidays` is a key into `cities/holidays.json`; `country`/`region` select the Overture divisions file |
| `areas[].area_box` | [w, s, e, n] degrees | explicit. Trips whose shape bbox misses it are dropped from the area store. `make.py show <batch>` prints a suggestion (D3.3); adding a city never changes it unless the integrator edits it |
| `areas[].feeds[]` | list | see below |
| `modes` | list | as `cities/gta.json` modes, without colours (the theme supplies them) |
| `cities` | [id] | upload order |

Feed entry: `id`, `name`, `publisher`, `licence_id` (exact key into `cities/licences.json`),
`licence_text` (the display string), optional `allow_nc` (a reason string; lets a `no` licence
through `plan` and `meta`, printed in flags and release notes), `source`:

| `source` | meaning | lock records |
|---|---|---|
| `{"tag": "...", "path": "..."}` | a file inside a tag of this repo | commit, sha256, bytes |
| `{"url": "...", "sha256": "..."}` | an immutable URL (MDB `mdb-<id>-<stamp>.zip`, Transitland, producer). Required for zips over 100 MB, which a git data branch cannot hold (London BODS 1,686 MB, Paris 157 and 270 MB, Singapore 336 MB, Tel Aviv 140 MB) | url, sha256, bytes |
| `{"secret": "ODPT_KEY", "url": "...{key}..."}` | a key-protected download; Actions passes the repository secret through `env`, the sandbox reads `$ODPT_KEY` | url without the key, sha256 of the bytes |
| `{"model": "honsu", "inputs": {"tag": "...", "area": "yokohama"}}` | `model_gtfs.py` output built from tagged inputs (Japan); the model PR adds the area | input sha256s, output sha256 |

#### 2.3 Lock: `cities/locks/<batch>.lock.json`

```json
{
  "batch": "gta",
  "locked_at": "2026-10-10T12:00:00Z",
  "areas": {
    "gta": {
      "feeds": {"ttc": {"source": "tag", "tag": "feeds/gta-2026-10-09", "commit": "<40 hex>", "path": "gtfs/ttc.zip",
                        "sha256": "<64 hex>", "bytes": 36417654, "feed_version": "", "valid": ["2026-09-30", "2026-10-31"]}},
      "overture": {"release": "2026-09-23.1", "bbox": [-80.12, 43.01, -78.52, 44.30],
                   "data_tag": "overture/gta-2026-09-23.1", "files": {"segments": "<sha256>", "water": "<sha256>"}},
      "divisions": {"key": "CA-ON", "sha256": "<sha256>"},
      "dates": {"day": {"ttc": ["2026-10-01", "..."]}, "week": {"ttc": {"mon": ["2026-10-05", "..."]}}}
    }
  },
  "boundaries": {"gta-markham": {"id": "<division_area id>", "division_id": "<id>", "subtype": "locality", "area_km2": 211.4,
                                 "frame": {"km_vertical": 49.0, "center_km": [15.7, 6.0]}, "clip": [-79.53, 43.55, -79.02, 44.16]}},
  "tools": {"python": "3.13.16", "chromium": "1194", "ffmpeg": "6.1.1-3ubuntu5"}
}
```

`dates` (with the composite rule per class, A3.7) is written after the first area build, for review
in the PR. `--frozen` (the default on Actions) fails on changed feed bytes, changed dates, or an
installed package version that differs from `requirements.txt`. `tools` is informational: a
different Python patch, Chromium or ffmpeg build prints a warning only (networks and basemaps
depend on the pinned packages, not on those; D-8 compares the outputs themselves).

#### 2.4 Brands: `cities/brands.json`

```json
{
  "version": 1,
  "agencies": [
    {"id": "ttc", "feeds": ["ttc"], "agency_names": ["TTC"], "label": "TTC", "color": "#ed1c24",
     "verified": true, "source": "ttc.zip routes.txt: ED1C24 on 146 bus and 11 streetcar routes",
     "lines": "rail", "alt": null, "rules": []},
    {"id": "brampton", "feeds": ["brampton"], "agency_names": ["Brampton Transit"], "label": "Brampton",
     "color": "#0067b1", "verified": false, "source": "no route_color in brampton.zip; livery blue, approximate",
     "lines": "none", "alt": null,
     "rules": [{"id": "zum", "label": "Züm", "short": "^5\\d\\d$", "color": "#e31837", "verified": false,
                "source": "Züm red, approximate"}]}
  ]
}
```

| key | type | meaning |
|---|---|---|
| `feeds` | [feed id] | the entry applies to routes of these feeds |
| `agency_names` | [str] or absent | if present, also match `agency_name`; absent = every agency of the feed |
| `color` | `#rrggbb` | raw brand colour; the renderer moves it into the theme envelope (B5) |
| `verified` | bool | true only when the hex is the dominant informative `route_color` of the agency in its own GTFS (A10 warns otherwise); false = from the livery, checked by a person before the batch PR merges |
| `lines` | `none` / `rail` / `all` | which routes keep their own informative `color_raw` as a line brand (rail = any mode other than `bus`) |
| `alt` | `#rrggbb` or null | second candidate of the distinctness ladder (B6) |
| `rules[]` | list | `short` (regex on `route_short_name`) or `route_ids` ([str]) to a sub-brand |

#### 2.5 Holidays: `cities/holidays.json` (A owns)

```json
{"CA-ON": {"2026-10-12": "Thanksgiving", "2026-12-25": "Christmas Day", "2026-12-28": "Boxing Day (observed)"},
 "CA": {"2026-10-12": "Thanksgiving"}}
```

Later batches add their country or region keys (section 10).

#### 2.6 Licences and copy templates (D owns)

`cities/licences.json` maps exact ids to rules; nothing is matched on free text (revision 1 matched
"non-commercial" inside the scouts' "No non-commercial clause found", and missed "for non
commercial use").

```json
{"licences": {
  "ogl-toronto":     {"name": "Open Government Licence - Toronto", "commercial": "yes",
                      "statement": "Contains information licensed under the Open Government Licence - Toronto."},
  "ogl-york":        {"name": "Open Government Licence - York Region", "commercial": "yes",
                      "statement": "Contains information licensed under the Open Government Licence - York Region."},
  "ogl-mississauga": {"name": "Open Government Licence - Mississauga", "commercial": "yes", "statement": "..."},
  "ogl-brampton":    {"name": "Open Government Licence - Brampton", "commercial": "yes", "statement": "..."},
  "ogl-durham":      {"name": "Open Government Licence - Durham", "commercial": "yes", "statement": "..."},
  "ogl-oakville":    {"name": "Open Government Licence - Oakville", "commercial": "yes", "statement": "..."},
  "ogl-burlington":  {"name": "Open Government Licence - Burlington", "commercial": "yes", "statement": "..."},
  "ogl-milton":      {"name": "Open Government Licence - Milton", "commercial": "yes", "statement": "..."},
  "metrolinx-open-data": {"name": "Metrolinx Open Data Licence", "commercial": "check",
                          "note": "read the Metrolinx open data terms before monetising"},
  "cc-by-4.0": {"commercial": "yes"}, "cc-by-sa-4.0": {"commercial": "yes"}, "cc0-1.0": {"commercial": "yes"},
  "odbl-1.0": {"commercial": "yes"}, "dl-de-by-2.0": {"commercial": "yes"}, "etalab-2.0": {"commercial": "yes"},
  "cc-by-nc-4.0": {"commercial": "no"}, "cc-by-nc-sa-4.0": {"commercial": "no"},
  "unknown": {"commercial": "check", "note": "licence not confirmed"}
 },
 "map": {"id": "overture-osm", "text": "Overture Maps Foundation (CDLA Permissive 2.0), © OpenStreetMap contributors (ODbL)", "commercial": "yes"}}
```

Each `ogl-*` statement follows the Toronto one with its own licence name ("..." above). An unknown `licence_id` is a validation
error. `commercial: no` fails `plan` and `meta` unless the feed has `allow_nc`; `check` goes into
`flags`, the CSV's `licence_flags` and the release notes.

`cities/templates/shorts_en.json` holds every visible and metadata string (D5, B10). Placeholders
filled by B in the page: `{place}`, `{modes_singular}`, `{modes_plural}`, `{peak_time}`,
`{peak_count}` (the variant's peak, 2.9), `{trips}`, `{month}`. D adds in metadata `{agencies}`,
`{dates_sentence}`, `{credits}`, `{author}`, `{hashtags}`, `{seconds}`, `{year}` (of `{month}`), `{peak_day}`,
and `{timetable_month}` and `{timetable_year}`: the batch month, which differs from `{month}` when a
major fallback feed sets the label (Burlington: November), for sentences about the timetables.

#### 2.7 Derived configs (D writes, A reads)

All carry `"schema": 4`. Legacy configs have no `schema` and never reach v4 code (they are read
only by the untouched legacy scripts).

**Area config** `build/areas/<area>/area.<tl>.json`:

| key | type | example | meaning |
|---|---|---|---|
| `schema`, `kind` | | `4`, `"area"` | |
| `id` | str | `"gta"` | area id |
| `origin` | [lon, lat] | `[-79.47, 43.8]` | from the area |
| `area_box` | [w, s, e, n] | from the area | explicit, never derived from the city set |
| `gtfs_dir` | str | `"cache/feeds/gta"` | zips named `<feed id>.zip` |
| `feeds` | list | area feeds plus `sha256` | |
| `modes` | list | batch modes | |
| `timeline` | obj | see below | |
| `stop_times_chunk` | int | 2000000 | rows per pandas chunk (A4) |

`timeline`: `{"kind": "day" | "week", "month": "2026-10", "timezone": "America/Toronto",
"holidays": {"2026-10-12": "Thanksgiving"}, "drop_pct": 8, "min_week_dates": 2, "fallback_days":
28, "guard": {"low": 0.90, "high": 1.03, "min_dates": 3}}`.

**City config** `build/<id>/city.<tl>.json`:

| key | type | example | meaning |
|---|---|---|---|
| `schema`, `kind` | | `4`, `"city"` | |
| `id`, `batch`, `area`, `place` | str | `"gta-markham"`, `"gta"`, `"gta"`, `"Markham"` | |
| `title` | str | `"MARKHAM"` | uppercase place |
| `origin` | [lon, lat] | area origin | |
| `frame` | obj | `{"km_vertical": 49.0, "center_km": [15.7, 6.0]}` | the day frame (also the week frame) |
| `trim_scale` | float | 1.25 | trim box = frame box scaled by this, plus 1 km |
| `boundary` | obj | `{"file": "build/gta-markham/boundary.geojson", "name": "Markham", "simplify_km": 0.02, "mask_km": 0.025}` | |
| `brands` | str | `"cities/brands.json"` | |
| `group_by` | obj | `{"field": "agency-auto", "min_share": 0.03, "max_groups": 3}` | |
| `credit_template`, `credit_fallback` | str | `"Data: {agencies} · Map: Overture, OSM"`, `"Data: {n} transit agencies · Map: Overture, OSM"` | on-screen credit; at most 2 lines at 22 px in 504 px (B9) |
| `preset`, `theme` | str, obj | `"shorts"`, `{"batch": "lake"}` | copied to meta |
| `render` | obj | `{}` | copied to meta.render (base values only) |
| `variants` | obj | 2.9 | `start: "am_peak"` and the rush frame are filled by A |
| `rush` | obj | `{"auto": true, "zoom": [1.4, 2.2], "share": 0.6}` | A8.6 |
| `fit_box` | [x0, y0, x1, y1] px | `[50, 390, 870, 1300]` | the box an automatic rush frame fits the city into (A8.6), from `defaults.json` |
| `panel` | obj | `{"preferred": "right", "tie": 0.10, "rect": [60, 1140, 620, 1500]}` | A8.7 |
| `card` | obj | `{"title": "MARKHAM", "templates": {...}}` | copied |
| `major_share` | float | 0.05 | a feed is major in this city at this share of inside vehicle-minutes |

**Basemap config** `build/<id>/basemap.config.json`: exactly the dict that the unmodified
`build_basemap.build(city)` reads: `id`, `origin`, `clip`, `basemap_dir`, `basemap`,
`gzip: true`, `boundary: null`, `built_dir: "build/<id>"`, no `basemap_city`, plus `"schema": 4`
(ignored by `build`). Output `build/<id>/basemap.json.gz`.

#### 2.8 Area store: `build/areas/<area>/<tl>/` (A writes and reads)

Internal, never loaded by the page. A directory of `.npy` files read with
`np.load(path, mmap_mode="r")`, so a trim touches only the rows it needs and peak memory stays
small on 16 GB runners even for Paris or New York, plus `meta.json` and `stamp.json`.

| file | dtype, shape | content |
|---|---|---|
| `meta.json` | JSON | `schema`, `area`, `origin`, `timeline` (with per-feed `dates`, `excluded`, `rule` per day class), `feeds[]` stats (A6), `routes[]` `{id, short, long, color, color_raw, type, feed, mode, agency}`, `day_classes` (`["wd"]` or `["mon", ..., "sun"]`), `n_dates` per feed and day class |
| `shape_off.npy` | int64 (S+1) | offsets into the shape arrays |
| `shape_xy.npy` | int32 (2 x points) | x, y interleaved, metres (= km rounded to 3 decimals, times 1000) |
| `shape_cum.npy` | int32 (points) | cumulative length in 0.1 m (= km rounded to 4 decimals, times 10000) |
| `trip_off.npy` | int64 (N+1) | offsets into the stop arrays |
| `trip_t.npy` | int32 | seconds |
| `trip_d.npy` | int32 | metres along the shape |
| `trip_r.npy`, `trip_s.npy`, `trip_feed.npy` | int32 (N) | route, shape, feed index |
| `trip_sha.npy` | uint8 (N x 20) | class signature sha1 |
| `trip_mult.npy` | int16 (N) | multiplicity: the most members of the class running on any one selected date (A3.4) |
| `trip_dates.npy` | int16 (N x K) | per day class k: selected dates of k on which at least one member runs |
| `trip_runs.npy` | int32 (N x K) | per day class k: member runs summed over the selected dates of k (for the mean counts) |
| `trip_draw.npy` | uint8 (N) | bit k set when the class is drawn in class k (A3.5, A3.7) |

Trips are sorted by `(t[0], "<feed>:<route_id>", sha1 hex)`: string keys, never area-level
indices, so the order of the trips of one city does not depend on which other cities or feeds are
in the area. Integers divided by 1000 (or 10000) give exactly the floats that
`np.round(km, 3)` (or 4) gives, so the network JSON is byte-identical to building from floats.
Every class with any member running on any selected date is stored (drawn or not), because the mean
counts need them.

#### 2.9 Network JSON v4 (A writes, B reads, D reads meta)

Legacy keys keep their meaning. New or changed keys:

```
meta.schema            4
meta.kind              "city"
meta.id, batch, area, place    "gta-markham", "gta", "gta", "Markham"
meta.title             "MARKHAM"
meta.subtitle          variants[<first>].label
meta.service_date      "2026-10" (the basis month; legacy key kept as a string)
meta.origin            area origin
meta.day_start/day_end window of the first variant (seconds; day_end may exceed 86400)
meta.frame             {"km_vertical", "center_km"}
meta.trim              {"scale": 1.25, "box_km": [x0, y0, x1, y1]}
meta.modes             batch modes with "color"/"trail" omitted (the theme supplies them)
meta.attribution       [credit]   (one line, for legacy readers)
meta.credit            "Data: YRT, GO, TTC · Map: Overture, OSM"
meta.build_key         sha256 of the trim step key (D2)
meta.feeds[]           {"id","name","publisher","licence_id","licence_text","version","sha256","month_used",
                        "dates": [...] (day) | {"mon": [...], ..., "sun": [...]} (week),
                        "rule": "half" | "median-date" (day) | {"mon": "half", ...} (week),
                        "median_date": "2026-10-20" | {"tue": "2026-10-20", ...} (only where rule is median-date),
                        "excluded": [{"date","why": "holiday"|"dst"|"low"|"no service","trips","median","name"}],
                        "classes","drawn","mean_trips_per_day",
                        "inside_vehicle_minutes","inside_share","major": bool}
meta.trips_total       trips in this file (copies counted)
meta.timeline          {"kind": "day"|"week", "period": 86400|604800, "basis": "average-weekday"|"average-week",
                        "month": "2026-10", "month_label": "October", "fallback_feeds": ["burlington"]}
meta.hist_period       1440 (day) | 10080 (week), minutes
meta.am_peak           {"count": 431, "time": 28860}   first argmax of raw hist in minutes 300..630 (week: Monday)
meta.pm_peak           {"count", "time"}               same, 870..1170
meta.hist_by_mode      {mode: [hist_period floats]}
meta.groups            [{"id": "yrt", "label": "YRT", "brand": "yrt", "share": 0.77}, ..., {"id": "other", "label": "other", "brand": null, "share": 0.02}]
meta.hist_by_group     {group: [hist_period floats]}
meta.boundary          {"name", "rings": [[x,y,...]], "holes": [[x,y,...]], "area_km2", "bbox_km": [x0,y0,x1,y1],
                        "source": "Overture 2026-09-23.1 division_area <id>",
                        "mask": {"cell_km": 0.025, "x0", "y0", "nx", "ny", "rle": [...]}}
meta.panel             {"side": "right", "inside_under": {"left": 31, "right": 12}, "why": "fewer inside vehicles under the panel"}
meta.brands            [{"id": "yrt", "label": "YRT", "hex": "0058a9", "kind": "agency"|"rule"|"line"|"gtfs"|"mode",
                         "entry": "yrt", "rail": false, "share": 0.81, "verified": false, "alt": null, "source": "..."}]
meta.color_by          "brand"
meta.preset            "shorts"
meta.theme             {"batch": "lake"}
meta.render            {...} base values from the city config
meta.variants          {"day": V, "rush": V}  (day network)  |  {"week": V}  (week network)
meta.card              {"title": "MARKHAM", "templates": {"day": [[line0, line1], [..], [..]], "rush": [...], "week": [...]}}
routes[i].agency, type, color_raw   agency_name, route_type int, route_color as in routes.txt ('' when blank)
routes[i].brand        index into meta.brands
routes[i].group        group id
trips[i]               {"r","s","t","d"} plus "w" (7-bit mask, bit 0 = Monday) in week files
hist                   [hist_period floats, 2 decimals]: mean vehicles running inside the boundary,
                       index = minute of the period (day: 0 = 00:00; week: 0 = Monday 00:00)
```

`hist`, `hist_by_mode` and `hist_by_group` are the mean running counts over each feed's selected
dates (decision 2), always, including where the drawn set follows the median-date rule. Every number
on screen and in the metadata is read from them (B9, B10, D5), so the count line, the peak label,
the card and the description cannot disagree.

Variant object `V`:

```json
{"start": 28860, "end": 115260, "label": "An average October weekday",
 "frame": null,
 "peak": {"count": 431, "time": 28860},
 "render": {"DURATION_FRAMES": 1500, "HOLD_START": 0, "HOLD_END": 0, "LOOP": "wrap",
            "TIME_WARP_MODE": "activity", "SPARK_SMOOTH_MIN": 35}}
```

`V.peak` = first argmax of the raw `hist` over the minutes of `[start, end)` (wrapped with the
period), count rounded. `V.frame` for the rush is the pinned or the automatic frame (A8.6), else
null.

Defaults per variant (D writes them from `cities/defaults.json`):

| variant | start, end | frames | render block |
|---|---|---|---|
| `day` | `am_peak.time`, start + 86400 | 1500 (50 s) | `LOOP "wrap"`, `TIME_WARP_MODE "activity"`, `SPARK_SMOOTH_MIN 35` |
| `rush` | 23400 (06:30), 34200 (09:30) | 750 (25 s) | `LOOP "xfade"`, `TIME_WARP_MODE "linear"`, `SPARK_SMOOTH_MIN 15`, `TRAIL_MINUTES 8` (A8.6 rescales it for a closer frame) |
| `week` | Monday `am_peak.time`, start + 604800 | 1800 (60 s) | `LOOP "wrap"`, `TIME_WARP_MODE "activity-daily"`, `TIME_WARP_FLOOR 0.3`, `SPARK_SMOOTH_MIN 60`, `TRAIL_MINUTES 30`, `BUS_HALO_ALPHA 0.1`, `BUS_CORE_R 0`, `WEEKEND_BAND true`, `CLOCK_ROUND 60` |

All three set `DURATION_FRAMES` to the frame count and `HOLD_START`/`HOLD_END` to 0.

#### 2.10 Page query and CONFIG keys (B implements, C and D pass)

New CONFIG keys, each declared in `CONFIG` with the default below (so `applyRender` type-checks
them). The default is today's behaviour; the Shorts values come from the preset (B3).

| CONFIG key | type | default | shorts preset | query |
|---|---|---|---|---|
| `PRESET` | str | `''` | | `preset` |
| `HUD_LAYOUT` | `'panel'`, `'shorts'` | `'panel'` | `'shorts'` | `layout` |
| `THEME` | `''`, `lake`, `ink`, `sodium`, `slate`, `teal` | `''` (today's COLORS) | from `meta.theme.batch` | `theme` |
| `VARIANT` | str | `''` | first key of `meta.variants` | `variant` |
| `COLOR_BY` | `''`, `'brand'` | `''` (use meta.color_by) | `'brand'` | `colorby` |
| `BRAND_MIN_DE` | float | 0.08 | | |
| `TIME_WARP_MODE` | `'empty'`, `'activity'`, `'activity-daily'`, `'linear'` | `'empty'` | `'activity'` | `warp` |
| `TIME_WARP_GAMMA` | float | 1 | 1 | `warpgamma` |
| `TIME_WARP_FLOOR` | float | 0.15 | 0.15 | `warpfloor` |
| `TIME_WARP_SMOOTH_MIN` | int | 60 | 60 | |
| `LOOP` | `'none'`, `'wrap'`, `'xfade'` | `'none'` | per variant | `loop` |
| `CLOCK_ROUND` | int minutes, 0 = auto | 0 | | `clockround` |
| `CARD` | bool | false | true | `card` (0/1) |
| `CARD_HOLD` / `CARD_FADE_OUT` / `CARD_FADE_IN` | int frames | 27 / 18 / 30 | | |
| `CARD_SCRIM` | float | 0.25 | | `cardscrim` |
| `CARD_BAND` | float | 0.85 | | `cardband` |
| `CARD_CENTER_Y` | int px | 620 | | `cardy` |
| `CARD_TITLE_MAX` | int px | 132 | | `cardsize` |
| `CARD_LINES` | int | 0 | from `variety.card_line` | `cardline` |
| `PEAK_MARKER` | bool | false | true | `peak` |
| `MODE_CHIPS` | bool | false | true | `chips` |
| `OUTSIDE_DIM` | float 0..1 | 0 | 0.55 | `outside` |
| `CITY_LINE_W` / `CITY_LINE_ALPHA` | float | 2 / 0.8 | | |
| `PANEL_ALPHA` | float | 1 | 1 | `panelalpha` |
| `PANEL_SIDE` | `''`, `'left'`, `'right'` | `''` (= `meta.panel.side`, else left) | | `panelside` |
| `TITLE_SIZE` | int px | 64 | | |
| `PANEL_TOP` | int px | 0 (auto, B9) | | |
| `FONT_SET` | `'classic'`, `'extended'` | `'classic'` | `'extended'` | `fonts` |
| `FRAME_ZOOM` | float | 1 | | `zoom` |
| `FRAME_DX_KM` / `FRAME_DY_KM` | float | 0 / 0 | | `cx` / `cy` |
| `BASE_ROADS_GAIN` / `BASE_WATER_GAIN` | float | 1 / 1 | | `roads` / `water` |
| `WEEKEND_BAND` | bool | false | | |
| `CAMERA` | bool | false | true | `camera` (0/1) |
| `CAMERA_PATH` | `'auto'` or a B18 path | `'auto'` (FNV-1a of `meta.id`) | from `variety.camera` (D3.8) | `campath` |
| `CAMERA_ZOOM` | float 0..0.5 | 0.08 | | `camzoom` |
| `CAMERA_DRIFT` | float 0..0.2, share of the frame width | 0.03 | | `camdrift` |
| `CAMERA_AMP` | float 0..3 | 1 | | `camamp` |
| `CAMERA_MAX_SPEED` | float, frame widths a second | 0.006 | | `camspeed` |
| `CAMERA_BOUND_MIN` | float 0..1, the city line's factor at least | 0.7 | | `cambound` |
| `CAMERA_BASE` | `'cache'`, `'vector'` | `'cache'` | | `cambase` |

New query names for existing keys: `dotcore` -> `BUS_CORE_R` (0 allowed: halo only), `halor` ->
`BUS_HALO_R`, `haloalpha` -> `BUS_HALO_ALPHA`, `layeralpha` -> `TRAIL_LAYER_ALPHA`, `smooth` ->
`SPARK_SMOOTH_MIN`. Existing ones stay (`trailmin`, `trailalpha`, `corew`, `shoulderw`,
`shoulder`, `shoulderbands`, `routealpha`, `trailscale`, `trailstep`, `trailbands`, `simplify`,
`trailmode`, `osm`).

New non-CONFIG query parameters:

| query | value | effect |
|---|---|---|
| `render` | URI-encoded JSON object of CONFIG keys | applied after `meta.render` and the variant block, before pinned knobs; carries D3.8's merge |
| `brandhex` | `id:rrggbb;id:rrggbb` | replaces `meta.brands[].hex` before normalisation; carries `override.brand_colors` |
| `hud` | `left`, `right` (today), `none`, `notext` | `none` skips HUD and card; `notext` draws every scrim, band and panel backdrop but no text |
| `safe` | `1` | draws the safe zone and the YouTube overlay zones (judge only) |
| `data`, `basemap` | URLs (today) | `../build/<id>/day/network.json.gz` etc. |

Precedence, lowest first: CONFIG default, LARGE_FRAME profile (frames of 60 km and up, as today),
preset (`web/presets/<name>.json` `render`), theme tokens, `meta.theme` keys, `meta.render`,
`meta.variants[v].render`, `render=` query JSON, single query knobs (pinned, as today).

#### 2.11 busmap API additions (B implements; C and D call)

Every existing member stays. Added:

| member | type | meaning |
|---|---|---|
| `variant` | str | active variant, `''` for legacy |
| `window` | `{start, end}` | seconds |
| `safe` | `{x0: 60, y0: 240, x1: 880, y1: 1500}` | |
| `hudBoxes()` | `[{name, x0, y0, x1, y1, color, size, font}]` | text boxes of the last drawn frame (B9, B10); names `title`, `subtitle`, `weekday`, `clock`, `count`, `count2`, `chips`, `axis`, `credit`, `credit2`, `peak`, `card_title`, `card_title2`, `card_line0`, `card_line0b`, `card_line1`, `card_line1b`; `size` in px |
| `lastVehicles` | Float32Array | `[x, y, inside, ...]` screen positions of vehicles running at the last render; `inside` from the A mask (B11) |
| `setHud(mode)` | fn | `'full'`, `'notext'`, `'none'`, without reload |
| `setCard(on)` | fn | turns the card on or off without reload |
| `cardAlpha(i)` | fn | card alpha at frame i |
| `stillTimes()` | fn | `{am, noon, pm, late, night}` absolute seconds inside the window (B15) |
| `brandMap` | `[{id, hex, trail, line, how, placed}]` | result of B6 (`how` = ladder step) |
| `countAt(T)` | fn | `{total, byGroup}` as drawn by the HUD at T (B9) |
| `chips` | `{ids, size, gap, width, merged, tried: [{n, size, gap, w}]}` or null | the chips fit (B9): parts shown, size, the width limit, groups folded into `other`, and every width tried in order |
| `camera` | `{path, zoom, drift, scale, amp, bound, box, keep, peak_speed, pivot, base: {w, h, k, x0, y0}}` or null | B18: the path, the zoom amplitude, the drift in px, the speed cap's factor, `CAMERA_AMP`, the city line's factor, the line's fitted bbox and keep rect (`[x0, y0, x1, y1]` px; `keep` null when the frame crops the line), the fastest on-screen motion in frame widths a second, the pivot, and the cached base (px, scale, base-px origin) |
| `cameraAt(u)` | fn | `{zoom, e, f}`: screen = zoom x base px + (e, f) at phase u; frame i of N is u = i / N; identity when off |
| `setCamera(u)` | fn | pins the phase `renderAt` draws the camera at; `null` follows T again (`renderFrame` always uses i / N) |

#### 2.12 render_video.mjs additions (C implements; D calls)

```
--data PATH            repo-relative network file; adds data=../PATH
--basemap PATH         repo-relative basemap file; adds basemap=../PATH
--variant NAME         adds variant=NAME
--render-json JSON     adds render=<encodeURIComponent(JSON)>
--brandhex STR         adds brandhex=STR
--tier T               stills | preview | final | tune  (absent = today's behaviour exactly)
--out PATH             tiers: the MP4 (final, preview); default out/shorts/<batch>/<name>.mp4
--name STEM            output stem, default from --out
--out-dir DIR          stills/tune output directory
--times LIST           stills/tune: comma list of H:MM (26:30 allowed), seconds, or "none"
--frames LIST          stills/tune: comma list of frame indices, "last" allowed (card checks)
--sheet                stills: also write <out-dir>/<name>.sheet.jpg (at most 1568 px on the long edge)
--clip-at T            tune: clip starts at the frame whose time is T (default busmap.stillTimes().am)
--clip-frames N        tune: default 90
--roundtrip            tune: also write the 720p VP9 round trips (C5)
--capture MODE         screenshot | canvas | raw | webcodecs (C3)
--jobs N               raw only, default min(3, CPUs - 1)
--min-kbps N           final: default 8000
--crf-ladder LIST      final: default 18,16,14,12,10
--key HEX              final: the render key (D2), written into the sidecar
--keep-frames LIST     final: also save the lossless canvas PNG of these frames ("0,300,last") into --review-dir
--review-dir DIR       final: review files (C6)
--keep-intermediate    final: keep the lossless intermediate
--dry-run              print the page URL and the final ffmpeg arguments, render nothing
```

#### 2.13 Sidecar, network metadata, metadata, CSV

Sidecar `<stem>.json` next to every MP4 (C): `{"name", "variant", "tier", "frames", "fps": 30,
"width": 1080, "height": 1920, "duration_s", "crf", "kbps", "bitrate_floor_met", "capture",
"ms_per_frame", "query", "network_sha256", "basemap_sha256", "code_sha256": {"app.js",
"index.html", "color.js", "themes.json", "presets/shorts.json", "render_video.mjs"}, "git_commit",
"chromium", "ffmpeg", "runner", "started_at", "key"}`.

Network metadata `<stem>.netmeta.json` (D, from the network meta, before rendering):
`{"id", "variant", "place", "title", "label", "month_label", "peak", "am_peak", "pm_peak", "feeds"
(id, name, publisher, licence_id, licence_text, dates, rule, excluded, inside_share, major),
"modes_present" (the B9 rule: modes with at least 0.5 inside vehicles in some minute of the
window), "groups", "credit", "seconds", "build_key"}`. `make.py meta` needs only this, the
batch, the recipe and the templates, so metadata can be rebuilt without the network or a re-render.

Metadata `<stem>.meta.json` (D): `{"file", "title", "description", "tags", "hashtags", "category",
"credits": [str], "licences": [{"feed", "licence_id", "commercial", "note", "allow_nc"}],
"flags": [str], "publish_order", "meta_key"}`.

CSV `out/shorts/<batch>/<batch>-youtube.csv`, UTF-8 with BOM, RFC 4180 quoting, columns
`publish_order,file,title,description,tags,category,made_for_kids,visibility,licence_flags`
(`made_for_kids` = `no`, `visibility` = `private`).

#### 2.14 make.py CLI (D implements)

```
python3 scripts/make.py lock <batch> [--refresh]
python3 scripts/make.py fetch <batch> [--area A] [--feeds-only | --overture-only] [--push-data-tag] [--frozen]
python3 scripts/make.py build <batch> [--area A] [--city ID] [--area-only] [--timeline day|week] [--frozen] [--no-upstream]
python3 scripts/make.py stills <id> [--variant V] [--extra-query Q]
python3 scripts/make.py preview <id> --variant V
python3 scripts/make.py render <id> --variant V [--tier final] [--no-upstream] [--review-dir DIR]
python3 scripts/make.py tune <id> [--variant V] [--knob K | --next] [--pick K=a|b|c --why TEXT] [--auto] [--apply]
python3 scripts/make.py meta <batch> [--only ID-VARIANT] [--from-netmeta DIR] [--no-upstream] [--allow-nc]
python3 scripts/make.py plan --from-branch batch/<batch> --github-output FILE
python3 scripts/make.py release <batch> --upload ID-VARIANT | --publish       # Actions only (GITHUB_TOKEN)
python3 scripts/make.py review-push <batch> --from DIR                        # Actions only
python3 scripts/make.py check-legacy [--record]
python3 scripts/make.py show <batch> | <id>                                   # derived configs, keys, area_box suggestion
```

Every subprocess is `sys.executable -I scripts/<tool>.py ...` or `node scripts/render_video.mjs
...` with an argument list, never a shell string, and with `PYTHONHASHSEED=0` in its environment.
`--no-upstream` never runs a step other than the one asked for: it checks
`build/<id>/manifest.json` (and the area stamp) and fails on a missing input or a key mismatch.
`--allow-nc` exists for local experiments only; Actions reads `allow_nc` from the batch file.

### A3. Composite dates and trip classes (`scripts/composite.py`)

Functions (pure, unit-tested):

```python
def service_dates(feed) -> dict[str, set[datetime.date]]       # calendar + calendar_dates, the rule of bn.active_services
def trips_per_date(feed, svc) -> collections.Counter            # trips per date; a frequencies.txt template counts its expanded runs
def select_dates(counts, timeline, tz, max_t1) -> dict          # A3.1 to A3.3, per feed and day class
def read_frame(feed, name, columns, key, keep, chunksize)       # A4
def read_stop_times(feed, trip_ids, chunksize) -> StopTimes     # A4; the arrays build_feed builds (lines 511 to 520)
def trip_classes(feed, svc, sel, st) -> list[TripClass]         # A3.4, A3.5
def apply_guard(classes, sel, counts, guard) -> dict            # A3.7
```

**A3.1 Candidates.** For each feed and each day class (`wd` for a day timeline; `mon` .. `sun`
for a week timeline), the dates of that class in the month that lie in the feed's validity (first
to last date with any service). `wd` = Monday to Friday.

**A3.2 Exclusions,** in this order, each recorded in `excluded` with its reason:

1. `holiday`: the date is in `timeline.holidays`, with its name.
2. `no service`: 0 trips on the date.
3. `dst`: the UTC offset in the feed's `agency_timezone` (first agency; else `timeline.timezone`)
   differs between local `d 00:00` and `d 00:00 + H`, with `H = max(30, ceil(max_t1 / 3600))` hours
   and `max_t1` the feed's latest stop time (TTC 30:59 gives 31 h). For the GTA this drops Saturday
   2026-10-31 (the change is at 02:00 on 11-01) and Sunday 2026-11-01; no October weekday.
4. `low`: trips on the date `< (1 - drop_pct / 100) x median` (drop_pct 8), the median over the
   class's dates still in after steps 1 to 3. One pass. Every median in A3 is a lower median (for
   an even count, the lower of the two middle values), so all tests stay in integers.

**A3.3 Fallback.** If a class has no date left for a feed, the feed uses the dates of that class in
`[first valid date, first valid date + fallback_days - 1]` (28 days) with the same exclusions, and
`month_used` becomes that window's month (Burlington: 2026-11-02 to 11-27, 20 weekdays,
`month_used` `2026-11`). If that is empty too, the feed contributes nothing and the summary says so.

**A3.4 Signature classes.** Inputs: every trip whose service runs on any selected date of any
class (frequency templates: every expanded run). Signature, joined with `\x1f`, hashed with sha1:

```
route_short_name | route_long_name | route_type      (from routes.txt; never route_id, GO versions it)
direction_id
shape_id
stop_id sequence                 (by stop_sequence, joined with '|')
arrival_time sequence            (raw strings)
departure_time sequence          (raw strings)
frequency offset                 ('' or '@<seconds>' for expanded runs)
```

For each class c and selected date x: `runs_c(x)` = members of c running on x. Then
`D_c = {x : runs_c(x) >= 1}`, the **multiplicity** `mult_c = max_x runs_c(x)` (Brampton and
Oakville each have 2 classes with `mult` 2 on every weekday), and per day class k
`dates_k(c) = |D_c ∩ W_k|`, `runs_k(c) = sum over x in W_k of runs_c(x)`. The representative is
the member running on the earliest selected date, ties by the smallest `trip_id`; its stop times,
shape and route are what gets built. Every set or dict that is iterated is iterated in sorted order
(no string-hash order anywhere).

**A3.5 Half rule.** With `W_k` the selected dates of class k for that feed, class c is drawn in k
when `2 x dates_k(c) >= |W_k|` (integers; decision 2's "at least half"), and then drawn
`mult_c` times. Day file: k = `wd`. Week file: `draw` bit k per class, the trip is in the network
when any bit is set, and its `w` mask is the `draw` bits.

**A3.6 Week eligibility** (per city, at the week trim): every feed with `major: true` in the city's
day network (inside share at least `major_share` 0.05) must have at least `min_week_dates` (2)
dates in every day class from the month itself (a fallback month does not count). Otherwise exit 3
with the reasons. GTA: Burlington is not eligible (Burlington Transit is major there and has no
October dates); the other nine are (E3).

**A3.7 Guard (two-sided).** For each feed and day class k, after A3.5: `ratio = (sum of mult_c over
drawn classes) / median over W_k of trips_per_date`. When `ratio < guard.low` (0.90), `ratio >
guard.high` (1.03) or `|W_k| < guard.min_dates` (3), the feed's drawn set in k becomes the
**median-date rule**: the trips of the selected date whose count is the lower median of `W_k`
(for an even count the lower of the two middle values; ties: the earliest date), each class with
`runs_c` on that date as its copy count. `rule` becomes `"median-date"` and `median_date` names
the date in `meta.feeds[]`; the summary warns. Only the drawn set changes: `hist` stays the mean
over all of `W_k` (A9). On the GTA data this rule takes, from 0.1.4: GO Sat (Oct 24), GO Sun (Oct
18), GO Tue (Oct 20, ratio 1.039), DRT Tue (Oct 20) and Wed (Oct 21, ratio 1.999: DRT's two
timetables tie on every class), UP Mon and Milton Mon (2 dates each: Oct 19). Every other class
keeps the half rule.

MUST (A-4): for every feed and every day class of both timelines, the drawn count over the median
is within 0.97 to 1.03, and the date lists and rules equal E3.

### B9. Shorts layout (`HUD_LAYOUT 'shorts'`)

Safe zone: x 60..880, y 240..1500; everything below is inside it. Left-aligned text throughout. On
a 6.1-inch phone one frame pixel is about 0.06 mm and Shorts are often served at 720 x 1280, so the
sizes below are minimums, never shrunk further. `dx` = 0 for the left panel, 260 for the right one
(panel x 320..880, text x 348..852). Font names are the `extended` families (B13).

| element | font | colour | x | baseline y | fit |
|---|---|---|---|---|---|
| title scrim | | `scrim` | | | sprite 1080 x 460 at y 0: alpha stops 0: S, 0.55: S, 0.80: 0.55 S, 1: 0; S = `TITLE_SCRIM` |
| title (`meta.title`) | MontserratX 700, `TITLE_SIZE` 64, letter-spacing 0.12 em | title | 72 | 316 | width <= 796: shrink by 2 down to 48 |
| subtitle (variant label) | MontserratX 400 36 | subtitle | 72 | 370 | 36 down to 32 |
| panel | backdrop, radius 24, `blur(28px)` once at init | `panel` x `PANEL_ALPHA` (alpha capped 0.95) | 60+dx..620+dx | | y `PANEL_TOP`..1500 |
| weekday (week only) | MontserratX 800, fitted once on `WEDNESDAY` from 80 down to 64 (68 with the repo fonts) | clock | 88+dx | clock row - 50 | |
| clock | day and rush: MontserratXTnum 800 88; week: MontserratXTnum 600 40 | clock | 88+dx | 1232 (day, rush), 1238 (week) | |
| count | MontserratXTnum 600 40 | accent | 88+dx | 1282 | fitted once on the widest text down to 36; then split (below) |
| chips | MontserratXTnum 500 26, dots r 9 | breakdown | 88+dx | 1320 | chips rule below |
| sparkline | area + 3 px accent stroke | accent | 88+dx..592+dx | y 1334..1386, floor line 1386.5 (1 px `floor`) | curve height `y1 - y0 - 6` |
| peak marker | dot r 5; label MontserratXTnum 600 26 `peak 3,951` | accent | right of the dot (left when it would pass 592+dx) | dot y + 9, inside 1353..1386 | |
| axis | MontserratX 500 26 | axis | 88+dx left, 592+dx right-aligned | 1416 | |
| credit (`meta.credit`) | InterX 400 22 | credit | 88+dx | 1450 and 1476 | wraps at a space into at most 2 lines of 504 px; never cut |

* **Panel top.** `PANEL_TOP` 0 means auto: the cap top of the first row minus 28 px: 1140 for day
  and rush, 1110 for the week (weekday cap top 1138), 44 px higher again when the count splits.
* **Count line**: `${withCommas(n)} ${noun} in ${meta.place}` with `n = round(histRaw(T / 60))`;
  noun = the mode label (singular when n is 1) when only one mode has at least 0.5 inside
  vehicles in some minute of `[start, end)` (the minutes `V.peak` is taken over), else
  `vehicles` / `vehicle`. Fit once on the text at `V.peak.count`: 40 px, down to 36; if it still
  exceeds 504 px, split before ` in `: `count` (`12,345 vehicles`) and `count2` (`in Richmond
  Hill`) 44 px apart, and the rows above move up 44 px. The count never exceeds the peak label,
  because both read `hist` and the label is its maximum over the window.
* **Chips** (`MODE_CHIPS`): groups from `meta.groups` in order (else modes). Each part is a dot r 9
  centred at `(x + 9, baseline - 9)` in the group's colour (B6), then the text `412 YRT` from
  `x + 26`; 20 px between parts (8 px in the last fit step). The numbers are the group means at T, rounded by largest
  remainder so they add up to the count line's n. Fit once at each group's maximum over the window,
  before the colours: all groups at 26 px, then at 24 px, then at 24 px with 8 px gaps; if none
  fits, the smallest shown group joins `other` and the shorter list tries the same three again,
  down to 1 group plus other. Never below 24. A group folded into `other` is not placed (B6): its brands are drawn in the foreign
  colour like `other`, so every trail colour on the map has its chip.
* **Clock.** `CLOCK_ROUND` 0 (auto): the time is floored to 5 minutes while `rate(m)` exceeds 2
  minutes per frame (nights of the day video), else to the minute; the week block sets 60 (`8 am`).
* **Axis**: day: `clockText(W0)` left and `clockText(W1)` right (the same clock time) plus a 1 px
  floor-colour tick at midnight (y 1386..1394) with `midnight` centred under it when its centre is
  at least 120 px from both ends. Rush: `6:30 am` / `9:30 am`, ticks at 7, 8, 9 am. Week: day letters
  `M T W T F S S` centred on each calendar day's visible span (none under 30 px), the current day's
  letter in MontserratX 700 accent; 1 px ticks at each midnight; with `WEEKEND_BAND` an accent
  rectangle at 0.07 alpha from x(Saturday 00:00) to x(Monday 00:00), y 1334..1386, under the area.
* **Weekday** (week): `['MONDAY', ..., 'SUNDAY'][floor(T / 86400) % 7]`, T counted from the
  composite Monday 00:00. It is the big line of the week panel; the clock is the small line.
* **Peak marker** (`PEAK_MARKER`): at `V.peak.time` on the drawn curve, shown once T has passed it;
  label `peak ${withCommas(V.peak.count)}`.
* **Asserts.** After layout, every box above and every card box (B10) is checked against x 60..880,
  y 240..1500 with `measureText` widths, and every text size against its minimum (title 48, subtitle
  32, weekday 64, clock 40, count 36, chips 24, peak, axis 26, credit 22, card line 0 40, card line 1
  30); any failure is a `console.error`, which fails a render (C). `hudBoxes()` reports `size`.
* `?safe=1`: translucent `rgba(255,0,0,0.18)` boxes over y 0..240, y 1500..1920 and x 880..1080 for
  y 240..1500, plus a 1 px outline of the safe zone. Never set by make.py renders.

### B10. Card and loop frame mapping

Card text from `meta.card.templates[VARIANT][CARD_LINES]`, a pair `[line0, line1]`, formatted with
`{place}`, `{modes_singular}` (`bus and train`, `bus, streetcar and train`: modes with at least
0.5 inside vehicles in some minute of `[start, end)`, the count noun's rule), `{modes_plural}`,
`{peak_time}` (`clockText(V.peak.time)`), `{peak_count}` (`V.peak.count` with commas), `{trips}`,
`{month}` (`meta.timeline.month_label`). An unknown placeholder is a `console.error`.

Layout per variant at init, left-aligned at x 72, width limit 796, block centred on `CARD_CENTER_Y`
(620: above the city centre, which D3.2 puts at y 845, so the busiest part of the map stays visible
under the card):

* Title `meta.card.title`: MontserratX 800, letter-spacing 0.04 em, S = the largest even size <=
  `CARD_TITLE_MAX` (132) and >= 72 whose width fits. If S is under 100 and the title has a space,
  split at the space that minimises the longer line and refit (`RICHMOND / HILL` 124 px). Baselines
  `B + 0.80 S + k x 0.98 S`.
* Accent rule: x 72..168, 6 px tall, top at the last title baseline + 30.
* Line 0: InterX 500 44 px, title colour, baseline = rule top + 64; wraps at a space into at most two
  lines 54 px apart (most templates need two: 792 to 1,088 px at 44 px); 40 px only if two lines are
  not enough. No break falls inside `{place}` (`Richmond / Hill` under the title reads as two
  names) unless no 44 or 40 px wrap can keep it whole.
* Line 1: InterX 400 32 px, title colour at 0.85 alpha, baseline = last line-0 baseline + 52; 32
  down to 30, then wraps, also keeping `{place}` whole when it can.
* Block height h = last baseline + 12 - B; `B = round(CARD_CENTER_Y - h / 2)`, clamped so the block
  stays inside y 400..1100.
* Scrim: the whole frame in `scrim` at `CARD_SCRIM x a` (0.25), so the moving map stays the hook;
  plus a full-width band from y `B - 60` to `B + h + 60` at `CARD_BAND x a` (0.85) with 48 px linear
  feathers at both edges. The card text is measured for contrast against this background (G4).

Card alpha for frame i of N = `totalFrames`, `smooth(x) = x x x x (3 - 2x)`:

```
a_out(i) = i < CARD_HOLD ? 1 : 1 - smooth(min(1, (i - CARD_HOLD) / CARD_FADE_OUT))       // 1 to frame 26, 0 from frame 45
a_in(i)  = LOOP == 'wrap' ? smooth(clamp((i - (N - 1 - CARD_FADE_IN)) / CARD_FADE_IN, 0, 1)) : 0   // 1 at N - 1
a(i)     = max(a_out(i), a_in(i))
```

The HUD (title scrim, title, subtitle, panel and everything in it, peak marker) is drawn at alpha
`1 - a(i)`; the map, trails, dots and boundary overlay are always full.

Frame to time:

* `LOOP 'wrap'` (day, week): `T(i) = timeAtProgress(i / N)`. Frame N - 1 is one step before
  `W0 + P`, which looks exactly like `W0`, and both ends carry the full card, so YouTube's loop from
  the last frame to frame 0 is one ordinary step.
* `LOOP 'xfade'` (rush): `T(i) = timeAtProgress(i / N)`; the live frame carries no card at the end
  (`a_in` = 0); a snapshot of frame 0 (card included) is drawn over it at alpha
  `smooth(clamp((i - (N - CARD_FADE_IN)) / CARD_FADE_IN, 0, 1))`, which reaches 1 at the virtual
  frame N, so N - 1 to 0 is one step of the cross-fade, with no held frame and no doubled card. The
  snapshot is rendered lazily into an offscreen canvas, so `renderFrameV4(i)` stays a pure function
  of i.
* `LOOP 'none'`: today's `frameTime`.

`renderAtV4(T)` stays pure and draws the HUD at full alpha with no card (stills); only
`renderFrameV4(i)` applies the card and the snapshot.

### B18. Camera

A slow drone move over the map layers: the base map, the dormant network, the trails, the dots,
the outside dimming and the city line. The title scrim, the HUD panel and everything in it, the
card and the `?safe=1` overlay are drawn after it at identity and stay put in the safe zone. It is
off by default (`CAMERA` false), so legacy files and any v4 load without the shorts preset draw
exactly what they did; the shorts preset turns it on.

**Phase.** The camera is a function of the loop phase only: `u = i / N` for frame i
(`N = totalFrames`), reduced modulo 1, so the virtual frame N is frame 0 and the day and week loops
stay exact; the rush's cross-fade snapshot is frame 0 at u = 0, and the live frames before it run
up to u = 1, so both images in the fade have nearly the same camera. A still at T (`renderAt`) uses
`u = progressAt(T)`, the phase the video shows T at, unless `setCamera(u)` pins one.

**Path.** With pivot `c = (460, 845)` (the fit box centre: D3.2 puts the boundary bbox centre and
A8.6 the rush vehicles there, below the card), zoom amplitude Z and drift R px, at
`th = 2 pi u`:

```
z(u)    = 1 + Z (1 + cos th) / 2              push-in at frame 0, the fitted frame at u = 1/2
screen  = c + z (base - c - R D(u))           base: the fitted frame's px
```

| path | D(u), x east, y south | zoom, drift share |
|---|---|---|
| `pull-out-east` | `((1 - cos th) / 2, 0.4 sin th / 2)` | 1, 1 |
| `pull-out-north` | `(0.4 sin th / 2, -(1 - cos th) / 2)` | 1, 1 |
| `pull-out-west` | `(-(1 - cos th) / 2, -0.4 sin th / 2)` | 1, 1 |
| `pull-out-south` | `(-0.4 sin th / 2, (1 - cos th) / 2)` | 1, 1 |
| `drift-orbit` | `(sin th / 2, -0.4 cos th)`, an ellipse round the core, clockwise | 0.5, 1.5 |
| `drift-sway` | `(sin th / 2, sin 2th / 4)`, a figure of eight across the core | 0.5, 1.5 |

The share multiplies `CAMERA_ZOOM` and `CAMERA_DRIFT` for that path. With one share for all, every
upload made the same push-in and pull-out in step and only the drift's direction told them apart;
the drifts push in half as far and travel half as far again, so they read as pans (the city core
travels 56 to 63 px over the loop, against 31 to 35 px on a pull-out) and the pull-outs as
pull-outs. Every path but the orbit starts on the pivot, so frame 0 is the fitted frame pushed in
about the city core (the orbit's core sits 0.4 R z, under 2% of the width, off it). The zoom eases in and out
(zero zoom speed at u = 0 and 1/2) while the sideways part keeps moving, so the motion never stops,
never runs at constant speed and is smooth to every derivative. `CAMERA_PATH 'auto'` picks
`CAMERA_NAMES[fnv1a(meta.id) % 6]` in the order of the table; `make.py camera_path()` is the same
function. D3.8 passes the recipe's `variety.camera`, or else make.py's batch pick, as `CAMERA_PATH`,
the same for day, rush and week. The batch pick (`make.py camera_paths()`) walks the batch's
`cities` in order: each city keeps its `camera_path(id)` unless more cities before it have that path
than have the least used one, and then takes the next least used path in table order; a recipe's
own `variety.camera` is kept and counted. The id hash alone put three of the ten GTA cities on the
sway and none on `pull-out-east`; the batch pick uses all six, none more than twice (Markham moves
to `pull-out-east`, Oakville to `drift-orbit`). Appending a city never moves one before it;
reordering the list, or naming a path in an earlier recipe, can move later ones. A recipe its batch
does not list keeps `camera_path(id)`.

**Amplitudes.** `Z0 = CAMERA_ZOOM` (0.08) and `R0 = CAMERA_DRIFT x 1080` (3% of the width), each
times the path's share. Both are scaled by one factor s <= 1, found by bisection, until the fastest
point of the frame (the step of the four corners between sampled phases; the step is affine in the
point, so the corners bound it) moves at most `CAMERA_MAX_SPEED` (0.6%) of the frame width a
second over `N / 30` s; then both are multiplied by `CAMERA_AMP` and by the city line's factor
(below). How fast a path moves at full amplitude depends on its shape, so s, and with it the move,
differs by path (drift as a share of the width; `CAMERA_AMP` 1, before the city line's factor):

| path | day, 50 s: s, zoom, drift | week, 60 s | rush, 25 s |
|---|---|---|---|
| `pull-out-east` | 0.882, 7.05%, 2.65% | 1, 8%, 3% | 0.437, 3.49%, 1.31% |
| `pull-out-north` | 0.937, 7.50%, 2.81% | 1, 8%, 3% | 0.465, 3.72%, 1.40% |
| `pull-out-west` | 0.950, 7.60%, 2.85% | 1, 8%, 3% | 0.470, 3.76%, 1.41% |
| `pull-out-south` | 0.816, 6.53%, 2.45% | 0.982, 7.86%, 2.95% | 0.405, 3.24%, 1.21% |
| `drift-orbit` | 1, 4%, 4.5% | 1, 4%, 4.5% | 0.592, 2.37%, 2.66% |
| `drift-sway` | 1, 4%, 4.5% | 1, 4%, 4.5% | 0.541, 2.16%, 2.44% |

Where s is under 1 the speed cap is the binding limit and the fastest point moves at exactly 0.6%
of the width a second, 0.22 px a frame; where s is 1 the path moves slower than that. So every
variant stays at or under the same super slow speed, and the rush, half the day's length, keeps
about half the day's move. The camera is off only when both amplitudes end at zero: a zero
`CAMERA_ZOOM` alone leaves a drift with no push-in.

**City line.** The push-in about the pivot and the drift both carry the city line outward, and the
speed cap does not know the city: one that fills the fit box width (D3.2, x 50..870) would reach
x 19..901 at frame 0, off the frame with `FRAME_ZOOM` over 1 (Markham's west tip went to x -4.6,
off screen for 11 s) or under the action buttons beyond x 880 (Toronto's east end, x 901). So when
the fitted frame shows the whole line (the bbox of `meta.boundary.rings` inside the frame), both
amplitudes are scaled by one more factor, found by bisection over the same sampled phases, until the
line's bbox stays inside the keep rect at every phase: each side at the looser of the safe zone and
the fitted bbox, plus 10 px, and never past the frame edge. The factor never goes under
`CAMERA_BOUND_MIN` (0.7, `cambound`). Above that floor the camera takes the line at most 10 px past
the safe zone, or past where the fitted frame already has it; the slack is the fit box's own (it
reaches 10 px past the safe zone's left edge), and without it a city that fills the fit box width
could not move at all. The factor is 1 when the whole move fits. For the four GTA cities that fill
the width (Toronto, Vaughan, Burlington, Markham) the keep rect alone gave about 0.3, a push-in of
2.3 to 2.5% and a drift of 0.9% that nobody notices. A visible move beats keeping a wide city's
whole outline in frame at every phase, so their factor stops at the floor: the day videos push in by
4.9 to 5.3% and drift 1.9 to 2.0% (the week 5.6% and 2.1%), and frame 0 is still the push-in. Only
at the floor may the line leave the keep rect, and on those day videos it does by 11 to 12 px. It
never leaves the frame (Markham's west tip comes closest, at x 7.9 on frame 0) and goes at most
26 px past x 880, under the action buttons, on Markham's frame 0, whose fitted line already reaches
x 885 (Toronto 11 px at u 0.94, Vaughan 11 px at u 0.22, Burlington 8 px at u 0.24). The text is
drawn at identity and does not move. Across the GTA day videos the zoom thus runs from 4% (the
drifts) through 4.9 to 5.3% (the wide cities) to 6.5% (Brampton and Oshawa, whose whole move
fits). A frame that crops the line (the rush close-ups) has no keep rect, and only the speed cap
applies. `busmap.camera` reports the factor (`bound`), the fitted bbox (`box`) and the rect
(`keep`).

**Sharpness.** The base map and the dormant network are drawn once into a cache that covers the
union over the loop of the base rectangle on screen (plus 3 px), at `2 (1 + Z)` times the fitted
scale with widths and dashes scaled along (so zoom 1 shows today's base), its origin placed so that
at frame 0 its pixel grid lands on the frame's. Each frame draws it scaled down by `z / (2 (1 + Z))`
with `imageSmoothingQuality 'medium'` (mipmapped), never up. Against the base drawn afresh as
vectors under the same camera (`CAMERA_BASE 'vector'`, the reference only) on Toronto's base this
keeps about 91% of the edge energy and flickers a third as much frame to frame at the fastest phase
(vector anti-aliasing of 1 px roads at a fresh subpixel offset each frame is the larger shimmer);
a 1x cache with bilinear filtering kept 83 to 90% and flickered more. On whole frames (trails, dots
and outline included) the test measures 98% of the vector render's edge energy, about 30% less
flicker and 50 dB. Trails, dots, the outside dimming
and the city line are drawn each frame under the camera: ribbons are stroked through the layer
transform with widths divided by z, dots are drawn at the camera's image of their position at their
own size, and the dimming is the cache rectangle with the rings cut out (even-odd) plus the rings
stroked at `CITY_LINE_W / z`, so every line keeps its width in screen px.

**Counting.** Trips are sampled in base px as before; only drawing goes through the camera.
`lastVehicles` reports screen positions (where the dots are), and each inside flag reads the mask
at the vehicle's own km position, which is the screen position mapped back through the camera.
`hist`, the count line, the chips, the peak and the card numbers do not change.

**Cost.** Toronto day from the am peak (B17's frame, `tests/web/v4_perf.mjs`): 240 ms a frame with
the camera off, 283 ms with it on, about 43 ms of it the mipmapped base draw; the limit is 450.

**Tests.** `tests/web/v4_camera.mjs`: the path from the id, the loop seam, the speed cap and its
smoothness over every frame, the amplitudes per variant, the core at frame 0, the headroom, the city
line's keep rect at every frame (the fixture's frame on all six paths, a frame with room, the
cropped rush, and Markham and Toronto when built), and where the floor binds the factor at
`CAMERA_BOUND_MIN` with the line at most 30 px past its rect (inside it with `cambound=0`), the
HUD and the counts with and without the
camera, all six paths, `CAMERA_ZOOM` 0 and the sprite, scaled and bounded trail modes, and the
cache against `CAMERA_BASE 'vector'`. `tests/web/v4_knobs.mjs` moves each `CAMERA*`
knob; `tests/test_make.py` and `tests/test_tune.py` cover `variety.camera`, `camera_path`, the batch
pick and the
`camamp` knob.
