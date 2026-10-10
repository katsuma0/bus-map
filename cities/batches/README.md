# Batches, recipes and locks

A batch is a set of Shorts that share a theme, a month of timetables and a draft
release: `gta` is the ten largest GTA cities, `test` is two squares over Tsukuba
that exercise the whole pipeline on the zips in `data/gtfs/`. Three kinds of file
describe it, and `scripts/make.py` rejects any key it does not know in all three.

```
cities/batches/<batch>.json      what to make: areas, feeds and their licences, modes, cities, theme
cities/recipes/<id>.json         one city: place name, boundary, variants, variety, tuned overrides
cities/locks/<batch>.lock.json   what it was made from: feed sha256s, boundaries, frames, clips, dates
```

The batch and the recipes are written by hand. The lock is written by
`make.py lock`, completed by `make.py fetch` (Overture file hashes) and
`make.py build` (composite dates), reviewed in the batch PR, and read as is by
Actions (`--frozen`). `make.py tune --apply` is the only command that edits a
recipe, and only inside `override` and `variety.card_line`.

## Recipe: `cities/recipes/<id>.json`

```json
{
 "id": "gta-markham",
 "batch": "gta",
 "place": "Markham",
 "boundary": {"name": "Markham", "subtypes": ["locality"], "area_km2": 210.93},
 "center": [-79.299, 43.880],
 "variants": ["day", "rush", "week"],
 "rush": {"frame": null, "auto": true},
 "variety": {"panel_side": "right", "card_line": 0, "zoom": 1.05},
 "override": {"render": {}, "variant_render": {}, "brand_colors": {}, "_why": {}}
}
```

| key | type | required | default | meaning |
|---|---|---|---|---|
| `id` | `^[a-z0-9][a-z0-9-]{1,39}$` | yes | | the file name and the stem of every output |
| `batch` | str | yes | | the batch must list this id in `cities` |
| `area` | str | when the batch has several areas | the only area | |
| `place` | str | yes | | the name on screen, in the title and the count line |
| `boundary` | object, or a list of them | yes | | one division, or several whose union is the boundary (see below) |
| `boundary.name` | str | yes | | Overture `names.primary`, exact |
| `boundary.subtypes` | list of `locality`, `localadmin`, `county` | no | all three | preference order |
| `boundary.area_km2` | number | yes | | census area of the polygon Overture holds: land area in Canada, total area (land and water) in the US, whose locality polygons keep the harbours, rivers and bays inside the city limits (New York 1,211 km2 against 778 of land); the match must be within 20% |
| `boundary.file` | path | no | | a GeoJSON Feature in the repo instead of Overture |
| `modes` | list of batch mode ids | no | every mode | the modes this video shows; the others' trips are left out of the network and every count |
| `center` | [lon, lat] | no | | documentation |
| `frame` | `{km_vertical, center_km}` | no | fitted to the boundary | pins the day frame |
| `variants` | subset of `day`, `rush`, `week` | no | the batch's `variants_default` | a week that is not eligible is dropped |
| `rush.frame` | `{km_vertical, center_km}` or null | no | null | a pinned close-up; must lie inside the day trim box |
| `rush.auto` | bool | no | true | with no pinned frame, find a closer frame automatically |
| `variety.panel_side` | `left`, `right` | no | `left` | breaks a tie when both panel sides hide about as many vehicles |
| `variety.card_line` | 0, 1, 2 | no | 0 | which card text the video opens with |
| `variety.zoom` | 0.5 to 2 | no | 1.0 | multiplies FRAME_ZOOM of the day and week |
| `variety.camera` | `pull-out-east`, `pull-out-north`, `pull-out-west`, `pull-out-south`, `drift-orbit`, `drift-sway`, `off` | no | picked from the id | the slow camera move over the map, the same in every variant; `off` keeps the map still |
| `override.render` | CONFIG keys | no | {} | for every variant; a key that a `defaults.variants.*.render` block sets is an error |
| `override.variant_render` | variant to CONFIG keys | no | {} | per variant, after `override.render` |
| `override.brand_colors` | brand id to `#rrggbb` | no | {} | pins a brand colour (`brandhex=`) |
| `override._why` | `KEY` or `<variant>.KEY` to one line | no | {} | why a tuned value was picked; changes no render key |

The page query of a video is `render=` with `CARD_LINES` from `variety.card_line`
and `CAMERA_PATH` from `variety.camera` (or `CAMERA: false` for `off`), then
`override.render`, then `override.variant_render.<variant>`; for the day and the
week, FRAME_ZOOM is then multiplied by `variety.zoom`. Without `variety.camera`
make.py picks the path so the batch spreads over all six (`make.py camera_paths`):
in the order of the batch's `cities`, each city takes FNV-1a of its id unless
more cities before it already have that path than have the least used one.
Appending a city never changes an earlier city's path; reordering the list or
naming a path in an earlier recipe can change later ones. The amplitudes are
CONFIG keys (`CAMERA_ZOOM`, `CAMERA_DRIFT`, `CAMERA_AMP`) and go in `override`
like any other.

## Batch: `cities/batches/<batch>.json`

| key | type | meaning |
|---|---|---|
| `batch` | str | the file name |
| `title` | str | for people; not in any key |
| `theme` | `lake`, `ink`, `sodium`, `slate`, `teal` | one per batch |
| `month` | `YYYY-MM` | the composite month; an area may set its own `month` |
| `overture_release` | str | Overture release for base maps and boundaries |
| `render_epoch` | int | part of every render key: bump it to render everything again |
| `review_videos` | list of `<id>-<variant>` | videos whose frames 0, 300 and N - 1 go to `review/<batch>` (default: the first city's) |
| `areas` | list | see below |
| `modes` | list of `{id, label, singular, route_types, routes?}` | colours come from the theme; `routes` moves named routes into the mode (see below) |
| `cities` | list of recipe ids | upload order |
| `variants_default` | list | for recipes without `variants` |
| `hashtags` | list of `#word` | `#shorts` always comes first in the metadata |
| `category` | str | YouTube category |
| `release` | `{tag, name}` | the draft release that collects the MP4s and the CSV |

Area: `id`, `origin` (the projection origin of every file in the area),
`timezone`, `country`, `region`, `holidays` (a key of `cities/holidays.json`),
`area_box` (`[w, s, e, n]`, explicit; `make.py show <batch>` suggests one and
`build` fails when a city's trim box is outside it), `feeds`, optional `month`.

Feed: `id`, `name`, `publisher`, `licence_id` (an exact key of
`cities/licences.json`), `licence_text`, optional `allow_nc` (a reason that lets a
non-commercial licence through), and `source`, one of:

| source | lock records | use |
|---|---|---|
| `{"tag": "...", "path": "..."}` | commit, sha256, bytes | a zip on a data branch, pinned by a tag (`feeds/gta-2026-10-09`) |
| `{"url": "...", "sha256": "..."}` | url, sha256, bytes | an immutable URL; needed for zips over 100 MB |
| `{"secret": "ODPT_KEY", "url": "...{key}..."}` | url without the key, sha256 | a key-protected download |
| `{"model": "honsu", "inputs": {...}}` | input and output sha256s | modelled Japan feeds (arrives with the Japan model PR) |
| `{"path": "data/gtfs/<zip>"}` | sha256, bytes | a zip committed on this branch; for the `test` batch |

Licences are matched on `licence_id` only. `commercial: no` stops `plan` and
`meta` unless the feed has `allow_nc`; `check` is printed in the release notes and
the CSV's `licence_flags`.

## Lock: `cities/locks/<batch>.lock.json`

```json
{
 "batch": "gta",
 "locked_at": "2026-10-10T12:00:00Z",
 "areas": {"gta": {
   "feeds": {"ttc": {"source": "tag", "tag": "feeds/gta-2026-10-09", "commit": "...", "path": "gtfs/ttc.zip",
                     "sha256": "...", "bytes": 36417654, "feed_version": "", "valid": ["2026-09-30", "2026-10-31"]}},
   "overture": {"release": "2026-09-23.1", "bbox": [-80.12, 43.01, -78.52, 44.30],
                "data_tag": "overture/gta-2026-09-23.1", "files": {"segments": "...", "water": "..."}},
   "divisions": {"key": "CA-ON-gta", "sha256": "...", "bbox": [-80.13, 43.01, -78.51, 44.30]},
   "dates": {"day": {"ttc": ["2026-10-01", "..."]}, "week": {"ttc": {"mon": ["2026-10-05", "..."]}}},
   "rules": {"day": {"ttc": "half"}, "week": {"go": {"tue": "median-date 2026-10-20", "...": "..."}}}}},
 "boundaries": {"gta-markham": {"id": "...", "division_id": "...", "name": "Markham", "subtype": "locality",
                                "area_km2": 211.4, "frame": {"km_vertical": 49.0, "center_km": [15.7, 6.0]},
                                "clip": [-79.53, 43.55, -79.02, 44.16]}},
 "tools": {"python": "3.13.16", "chromium": "1194", "ffmpeg": "6.1.1-3ubuntu5"}
}
```

* `frame` is fitted to the boundary's bounding box (it lands in the box
  x 50 to 870, y 390 to 1300 of the 1080 x 1920 frame) unless the recipe pins one;
  `clip` is the trim box plus 2 km, rounded outward to 0.01 degree; the Overture
  `bbox` is the union of the area's clips plus 5 km.
* The divisions file is fetched once per area, before any frame exists, over the
  area box plus 5 km: `cache/overture/<release>/divisions/<region>-<area>.geojson`.
  When every boundary of the area is a `boundary.file`, nothing is fetched and the
  lock records `"sha256": null`.
* A union boundary's entry has `"subtype": "union"`, the divisions' names joined
  with ` + ` and a `parts` list with each division's id, division_id, name, subtype
  and area.
* `dates` and `rules` are written by the first area build and are part of every
  render key of the area; `files` is written by the first `make.py fetch`.
* `data_tag` is `overture/<area>-<release>` until a lock moves the extract `bbox`
  within the release: that lock adds 8 hex of the new bbox, so the tag pushed for
  the old extract keeps its bytes.
* `boundaries.<id>.week_eligible: false` and `week_why` are written when the week
  trim finds the city not eligible; `plan` then skips that week.
* `--frozen` (Actions) fails on changed feed bytes, changed dates, a changed frame
  or an installed package that differs from `requirements.txt`; a different
  Python patch, Chromium or ffmpeg only warns.
* `make.py lock` keeps `locked_at` when nothing else changed, so relocking an
  unchanged batch leaves the file as it is. `--refresh` fetches the divisions and
  URL feeds again and keeps nothing from the old lock.

## Splitting a city by mode

A city with buses, streetcars and trains reads better as two videos. Give each
recipe the modes it shows and the same boundary; they share the area build, the
boundary, the frame and the clip:

```json
{"id": "gta-toronto-trains", "place": "Toronto", "boundary": {"name": "Toronto", "subtypes": ["county"], "area_km2": 631.1},
 "modes": ["rail"], ...}
{"id": "gta-toronto-buses", "place": "Toronto", "boundary": {"name": "Toronto", "subtypes": ["county"], "area_km2": 631.1},
 "modes": ["bus", "streetcar"], ...}
```

Titles, cards, descriptions and tags follow: "Every train in Toronto in 24 hours",
"Every bus and streetcar in Toronto in 24 hours"; the trains card says "Busiest at
8:17 am with 168 trains" where the buses and streetcars one says "with 1,561
vehicles", and the trains video is tagged "train map", not "bus map".

A feed can give a route the wrong route_type for this split: ttc.zip gives Line 5
Eglinton and Line 6 Finch West (route_id and route_short_name `5` and `6`)
route_type 0, the streetcars' type. A batch mode's `routes` moves them:

```json
{"id": "rail", "label": "trains", "singular": "train", "route_types": [1, 2, 100, 109, 400],
 "routes": [{"feed": "ttc", "short": "^[56]$", "why": "Line 5 and Line 6 are light rail with route_type 0"}]}
```

A rule needs `feed` and `short` (a regex on `route_short_name`) or `route_id` (a
regex on `route_id`), or both; `why` is for people. The first mode whose rule
matches wins over every route_type. The area build prints the routes each rule moved.

A boundary can join several divisions, for a video of a whole region:

```json
{"id": "gta-trains", "place": "the GTA", "modes": ["rail"],
 "boundary": [{"name": "Toronto", "subtypes": ["county"], "area_km2": 631.1},
              {"name": "Peel Region", "subtypes": ["county"], "area_km2": 1246.9},
              {"name": "York Region", "subtypes": ["county"], "area_km2": 1762.1},
              {"name": "Durham Region", "subtypes": ["county"], "area_km2": 2523.8},
              {"name": "Halton Region", "subtypes": ["county"], "area_km2": 964.0}]}
```

Each division is matched as a single boundary is, and `fetch_boundary.py union`
joins them; the outline, the dimming and the counts use the union, the lock
records its `parts`, and the description lists the divisions where a city's says
"the Toronto city limits". `place` is written as a sentence reads it ("412 trains
in the GTA"). Fitted, a region's frame is far larger than a city's (the GTA's would
be 325.5 km tall), so a region video can pin a closer `frame` that shows only part
of it. The trim box then grows to hold the whole boundary, so every vehicle inside
is still drawn and counted, while the clip follows the frame. The grown trim box
must fit the area's `area_box`; `make.py show <batch>` suggests one that does.

## Making a batch

```
python3 scripts/make.py lock gta          # feeds, boundaries, frames, clips
python3 scripts/make.py fetch gta         # cache/ matches the lock
python3 scripts/make.py build gta         # area stores, trims, base maps; writes the dates into the lock
python3 scripts/make.py show gta          # derived configs, keys, area_box suggestion
python3 scripts/make.py stills gta-markham
python3 scripts/make.py tune gta-markham --next
python3 scripts/make.py meta gta          # titles, descriptions and the CSV, checked locally
```

Commit the batch file, the recipes, the lock and `cities/brands.json` on
`batch/<name>` and push: `.github/workflows/render.yml` renders what changed and
collects it in the draft release.
