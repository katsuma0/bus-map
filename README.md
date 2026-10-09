# Tsukuba buses, one Friday

A 51 second portrait video of every scheduled bus in Tsukuba city on Friday 9 October 2026, 4:30 am to 4:30 am. Blue lines are the routes, white dots are buses, the glow is where a bus passed in the last 40 minutes. The clock, the running count and the yellow sparkline come straight from the timetables. It is a copy of the Melbourne buses timelapse by Taimoor Sohail, rebuilt for a city with 27 buses at the morning peak instead of 1,200.

The render is `out/tsukuba-buses.mp4`. The same thing runs live in a browser from `web/index.html` with a play button and a scrubber.

## What is in it

Two feeds, both published by Tsukuba City on the GTFS data repository under CC BY 4.0.

| feed | routes | trips on 9 Oct | hours |
| --- | --- | --- | --- |
| つくバス (Tsukubus), 11 shuttle routes | 11 | 283 | 5:50 to 23:10 |
| つくばね号 (Tsukubane-go), Tsukuba district branch bus | 1 | 17 | 7:46 to 18:00 |

That is 300 trips and a peak of 27 buses on the road at 8:05 am. Kantetsu, which runs most of the buses you actually see around Tsukuba Centre, does not publish open timetable data, so its routes are not animated. Their geometry is in `data/osm/bus_routes.geojson` from OpenStreetMap and can be drawn as a faint layer with `?osm=1`, off by default so the lines on screen are the ones the dots run on. Tsuchiura's つちまるバス is downloaded too and comes in with `--feeds all`.

The base map is roads, rail and water from Overture Maps (which is largely OpenStreetMap) for a 36 km tall frame centred a little east of Tsukuba Centre.

Nights are sped up. Tsukuba has no bus between 23:10 and 5:50, so a minute with nothing running gets a quarter of the screen time of a normal minute. The clock still shows the real time.

## Greater Toronto Area

`out/gta.mp4` is the same idea for home: every scheduled bus, streetcar and train in the GTA on Friday 9 October 2026. Buses are blue, streetcars red, trains (TTC subway, GO, UP Express) gold. Where modes cross, the colours add up to white.

| feed | trips on 9 Oct |
| --- | --- |
| TTC (subway, streetcar, bus) | 40,462 |
| MiWay | 5,589 |
| Brampton Transit | 5,312 |
| York Region Transit | 4,798 |
| GO Transit (train and bus) | 1,833 |
| Durham Region Transit | 1,589 |
| Oakville Transit | 1,535 |
| Milton Transit | 540 |
| UP Express | 160 |
| Burlington Transit | 0, its feed starts 1 November |

61,818 trips, 2,880 vehicles on the road at the 5:10 pm peak (2,519 buses, 223 streetcars at 5:22, 151 trains at 8:00 am). The frame is 64 km wide, Oakville to Pickering, Lake Ontario to Bradford, so Oshawa and Burlington's far ends are cut. TTC Line 5 and Line 6 are route type 0 in the feed and count as streetcars.

The feeds come from each agency's own open data URL through `.github/workflows/fetch-gta.yml`, which drops the 85 MB of zips on an orphan branch `data/gta-gtfs` rather than in this history. The built network is `data/gta/built/network.json.gz`, 17 MB, and the page inflates it in the browser. A dense city needs different drawing: trails and the dormant network blend per mode with normal alpha instead of adding, and dots are smaller, otherwise old Toronto burns to a white square. That switches on for any frame 60 km or taller.

## How it is built

```
.github/workflows/fetch-data.yml   pulls the GTFS zips and OSM routes onto the branch (GitHub runners can reach gtfs-data.jp, my sandbox could not)
scripts/fetch_overture.py          reads only the Overture parquet row groups that touch the frame, straight from S3
scripts/build_basemap.py           projects and simplifies them into data/built/basemap.json (5 MB)
scripts/build_network.py           GTFS zips -> data/built/network.json: shapes, per trip stop times and distances, buses per minute
web/app.js                         canvas renderer, render(T) is a pure function of the simulated time
scripts/render_video.mjs           Playwright drives the page frame by frame into ffmpeg
```

To rerun from scratch:

```
pip install pyarrow fsspec aiohttp shapely requests
python3 scripts/fetch_overture.py && python3 scripts/build_basemap.py
python3 scripts/build_network.py            # --date 20261010 for a Saturday, --feeds all for Tsuchiura too
python3 scripts/build_basemap.py --city gta && python3 scripts/build_network.py --city gta
npm install
npm run render:canvas                       # out/tsukuba-buses.mp4, about 4 minutes
npm run render:canvas -- --city gta         # out/gta.mp4, about 9 minutes
npm run serve                               # then open the printed URL for the live version
```


The renderer expects Chromium at `/opt/pw-browsers/chromium`; set `PLAYWRIGHT_CHROMIUM` or edit the path at the top of `scripts/render_video.mjs` if yours is elsewhere. `docs/CONTRACT.md` has the JSON formats and the pixel spec.

## Data notes

GTFS-JP times are whole minutes. When two consecutive stops share a minute the builder retimes that run at constant speed so a bus does not sit still and then jump. One Tsukubus pair is published 2.1 km apart one minute apart, which is 127 km/h; that is in the source and left alone.

Data: Tsukuba City GTFS-JP (CC BY 4.0), Overture Maps Foundation and OpenStreetMap contributors (ODbL). Fonts: Montserrat and Inter (OFL).
