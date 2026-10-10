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
| Durham Region Transit | 817, the rest runs east of the frame |
| Oakville Transit | 1,535 |
| Milton Transit | 540 |
| UP Express | 160 |
| Burlington Transit | 0, its feed starts 1 November |

61,046 trips once the ones that never enter the frame are dropped, and 2,848 vehicles on the road at the 5:10 pm peak (about 2,490 buses, 214 streetcars, 150 trains). The frame is 64 km wide, Oakville to Pickering, Lake Ontario to Bradford, so Oshawa and Burlington's far ends are cut. TTC Line 5 and Line 6 are route type 0 in the feed and count as streetcars.

The feeds come from each agency's own open data URL through `.github/workflows/fetch-gta.yml`, which drops the 85 MB of zips on an orphan branch `data/gta-gtfs` rather than in this history. The built network is `data/gta/built/network.json.gz`, 17 MB, and the page inflates it in the browser. A dense city needs different drawing: trails and the dormant network blend per mode with normal alpha instead of adding, the dormant lines are drawn once per mode so thirty overlapping Bloor shapes are as dim as one, and the dots are smaller. Otherwise old Toronto burns to a white square. That switches on for any frame 60 km or taller, and the HUD moves down to sit on the lake.

## Tokyo, Kyoto and Osaka

Six more videos, trains and buses separately for each city, because one video with everything in Tokyo is a white rectangle. They are `out/tokyo-trains.mp4`, `out/tokyo-buses.mp4`, `out/kyoto-trains.mp4`, `out/kyoto-buses.mp4`, `out/osaka-trains.mp4` and `out/osaka-buses.mp4`. Trains wear their line colours, so the Yamanote is green, the Chuo Rapid is orange and the Midosuji is red, and the HUD splits the count into JR, the subway and private lines. Streetcars ride along in the bus videos in a colour of their own.

Most of it is modelled, and the videos say so on screen. Japan publishes open timetables for very little, and almost none of it reachable without a developer key.

| video | real timetables | modelled | trips | peak |
| --- | --- | --- | --- | --- |
| tokyo-trains | Toei subway and Nippori-Toneri liner | JR, Tokyo Metro and the private lines | 33,254 | 1,884 at 8:12 (JR 616, Metro 274, Toei 141, private 853) |
| tokyo-buses | Toei bus, Sakura Tram, 9 ward community buses | Setagaya Line | 16,527 | 920 at 8:30 |
| kyoto-trains | none | all of it | 4,708 | 227 at 8:18 |
| kyoto-buses | none | all of it, Randen included | 16,688 | 1,141 at 8:31 |
| osaka-trains | none | all of it | 17,179 | 852 at 8:02 |
| osaka-buses | none | all of it, Hankai included | 34,998 | 1,861 at 8:26 |

The trains come from the 2026 edition of gtfs-gis.jp's 全国鉄道運行本数データ (A. Nishizawa, CC BY 4.0), which counts the regular weekday trains on every section of every line in each direction. `scripts/model_gtfs.py` turns each line into a set of run patterns that add up to those counts, spaces their departures through the day along the shape of Toei's real weekday timetable, and runs them at flat average speeds: 30 km/h underground, 34 on JR and private lines, 27 on the automated guideways and 13 to 19 for streetcars. So how many trains run on each stretch is real and where a given train is at 8:12 is a guess. The modelled Yamanote peaks at 43 trains running at once. Paid limited expresses and the Shinkansen are not in the counts, so they are not on the map.

The buses in Kyoto and Osaka come from 国土数値情報 バスルート (MLIT N07), the 2011 edition, surveyed around 2010. It is the last edition with daily trip counts, so the routes are 2010 routes. Each one gets its weekday trips in each direction at 13 km/h on the same timing profile. Highway coaches and airport limousines are dropped by name and operator, which took a longer list of regexes than I expected. That edition is licensed for non-commercial use only, so those two videos are fine to post and not fine to sell or put in client work.

Tokyo's buses are real but partial. Tokyu, Keio, Odakyu, Seibu, Kokusai Kogyo and the other private companies publish through ODPT behind a developer key, so the west of the frame is quieter than the real thing. A free ODPT key would also replace the modelled Tokyo Metro, JR East and private trains with real timetables; the pipeline does not read one yet.

The line colours are in `cities/line_colors.json`. Most are the operators' own; the notes in that file say which hexes are approximate, which were lightened to read on the dark background, and why Keikyu stays red when its official colour is a sky blue that would merge with the Keihin-Tohoku line 300 m away.

Source data comes in through `.github/workflows/fetch-japan.yml` onto the orphan branch `data/japan-src`, since my sandbox cannot reach gtfs-data.jp, ODPT or MLIT. The rules the modeller follows are written out in `docs/CONTRACT.md` under v3.

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
mkdir -p data/japan-src && git archive origin/data/japan-src | tar -x -C data/japan-src
python3 scripts/model_gtfs.py --area all    # modelled GTFS into data/tokyo|kyoto|osaka/gtfs
python3 scripts/build_basemap.py --city tokyo-trains   # one per city, shared by its two videos
python3 scripts/build_network.py --city tokyo-trains   # and the other five ids
npm run render:canvas -- --city tokyo-trains --out out/tokyo-trains.mp4
npm run serve                               # then open the printed URL for the live version
```


The renderer expects Chromium at `/opt/pw-browsers/chromium`; set `PLAYWRIGHT_CHROMIUM` or edit the path at the top of `scripts/render_video.mjs` if yours is elsewhere. `docs/CONTRACT.md` has the JSON formats and the pixel spec.

## Data notes

GTFS-JP times are whole minutes. When two consecutive stops share a minute the builder retimes that run at constant speed so a bus does not sit still and then jump. One Tsukubus pair is published 2.1 km apart one minute apart, which is 127 km/h; that is in the source and left alone.

Data: Tsukuba City GTFS-JP (CC BY 4.0), Overture Maps Foundation and OpenStreetMap contributors (ODbL). Fonts: Montserrat and Inter (OFL).

Japan data: Toei GTFS and the ward community bus GTFS, Tokyo Metropolitan Government and the wards via ODPT (CC BY 4.0); 全国鉄道運行本数データ 2026, gtfs-gis.jp (CC BY 4.0); 国土数値情報 バスルート N07 2011, MLIT (non-commercial).
