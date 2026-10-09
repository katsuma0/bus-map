/* Transit day renderer. See docs/CONTRACT.md, "Visual spec" and "v2".
 *
 * render(T) is a pure function of simulated time T (seconds since midnight,
 * may exceed 86400). The only state kept between frames is the cached static
 * base map, the glow sprites, the HUD layout and the sparkline geometry, all
 * built once in init(). Playwright drives renderFrame(i) in any order through
 * window.busmap.
 *
 * Everything city specific (frame, modes, title, attribution) travels in
 * network.json meta; the page only needs to know where each city's files are.
 */
(() => {
'use strict';

// Built data per city, relative to web/index.html. gz: the builders wrote
// network.json.gz and basemap.json.gz instead of plain JSON.
const CITIES = {
  tsukuba: { dir: '../data/built', gz: false },
  gta: { dir: '../data/gta/built', gz: true },
};

const CONFIG = {
  WIDTH: 1080,
  HEIGHT: 1920,
  FPS: 30,
  // Map scale: the frame height spans this many km, centred on the data
  // origin plus CENTER_KM (km east, km north). meta.frame overrides both.
  KM_VERTICAL: 36,
  CENTER_KM: [-2.2, 0],
  // Which side the clock panel sits on; meta.frame.hud_side overrides.
  HUD_SIDE: 'left',
  // Timeline: the service day plays over DURATION_FRAMES, with still frames
  // held at both ends so the opening and closing states register.
  DURATION_FRAMES: 1440,
  HOLD_START: 20,
  HOLD_END: 60,
  // Time warp: minutes with no vehicle on the road (and none within
  // TIME_WARP_MARGIN_MIN) get TIME_WARP_EMPTY of the screen time a normal
  // minute gets. Tsukuba has no night service, so without this a quarter of
  // the video is an empty map. The clock and the sparkline stay honest; the
  // night just goes by faster.
  TIME_WARP: true,
  TIME_WARP_EMPTY: 0.15,
  TIME_WARP_MARGIN_MIN: 30,
  // Trails: one sprite stamp every TRAIL_STEP_S seconds of simulated time
  // over the last TRAIL_MINUTES, fading from TRAIL_ALPHA to 0.
  TRAIL_MINUTES: 25,
  TRAIL_STEP_S: 15,
  TRAIL_RADIUS: 7,
  TRAIL_ALPHA: 0.7,
  // How trails reach the canvas. 'sprite' stamps one glow sprite per
  // TRAIL_STEP_S sample, the literal look from the contract, but software
  // raster in headless Chromium costs about 3 us per drawImage, so 300 buses
  // (48k stamps) take well over 100 ms. 'ribbon' instead cuts the trail into
  // TRAIL_BANDS age bands, appends every vehicle's stretch for a band to one
  // shared Path2D per (mode, band) built from the simplified route geometry,
  // and strokes each band once for the core and once wider for the soft
  // shoulder: about 32 stroke calls per mode per frame however many vehicles
  // are running.
  TRAIL_MODE: 'ribbon',
  TRAIL_BANDS: 16,
  TRAIL_SIMPLIFY_PX: 1.5,
  // Ribbon widths in px and the shoulder's alpha relative to the core's. The
  // shoulder pass is pure pixel cost, so it only goes on the freshest
  // TRAIL_SHOULDER_BANDS bands, where the trail is bright enough to show it.
  TRAIL_CORE_W: 6,
  TRAIL_SHOULDER_W: 22,
  TRAIL_SHOULDER_ALPHA: 0.6,
  TRAIL_SHOULDER_BANDS: 7,
  // Sparkline smoothing window in minutes (per-minute counts are spiky).
  SPARK_SMOOTH_MIN: 35,
  // Trail layer resolution relative to the frame. Below 1 the trails go to a
  // smaller canvas that is scaled up; the glow is soft so nothing is lost and
  // the per-frame fill cost drops with the square of the factor.
  TRAIL_SCALE: 1,
  BUS_CORE_R: 3,
  BUS_HALO_R: 11,
  BUS_HALO_ALPHA: 0.35,
  // The static network stays dim so the glow is earned by trails and the
  // map actually goes dark at night.
  ROUTE_ALPHA: 0.13,
  ROUTE_WIDTH: 1.8,
  OSM_ROUTES: false,
  OSM_ROUTES_ALPHA: 0.10,
  BASEMAP_URL: '../data/built/basemap.json',
  NETWORK_URL: '../data/built/network.json',
  // Inflate the fetched files with DecompressionStream. Set by the city table
  // or ?gz=1; a URL ending in .gz is inflated regardless.
  GZIP: false,
  // Frames this tall or taller take the LARGE_FRAME trail values. The
  // numbers above were measured on Tsukuba's 36 km frame: 53 px per km and
  // 27 buses at the peak. The GTA frame is 114 km, 17 px per km, with 2,900
  // vehicles whose headways are shorter than a 25 minute trail, so at those
  // widths and alpha the city fuses into one white mass and the peak frame
  // costs 440 ms. Shorter, thinner, dimmer trails keep the corridors legible
  // and bring that frame to about 280 ms. A query knob still wins over these.
  LARGE_FRAME_KM: 60,
  LARGE_FRAME: {
    TRAIL_MINUTES: 12,
    TRAIL_ALPHA: 0.5,
    TRAIL_CORE_W: 4,
    TRAIL_SHOULDER_W: 9,
    TRAIL_SHOULDER_BANDS: 5,
    TRAIL_BLEND: 'bounded',
    ROUTE_BLEND: 'bounded',
    ROUTE_ALPHA: 0.3,
    // Two thousand halos in the old city add up even when the trails do
    // not, so the dots get smaller and dimmer too.
    BUS_HALO_ALPHA: 0.12,
    BUS_HALO_R: 8,
    BUS_CORE_R: 2.2,
  },
  // 'add' strokes every trail and route straight onto the frame with
  // 'lighter', so a corridor with twelve buses in twelve minutes goes white.
  // That is the look for a sparse network. 'bounded' strokes each mode's
  // trails with normal alpha into its own layer, so one mode never exceeds
  // its own colour, and only then adds the layers together: streetcar red and
  // subway gold stay legible across the densest grid, and crossings of
  // different modes still go white.
  TRAIL_BLEND: 'add',
  ROUTE_BLEND: 'add',
  COLORS: {
    bg: '#07080c',
    water: '#1c1f27',
    waterLine: '#242831',
    minor: '#23252b',
    major: '#2e3138',
    rail: '#2a2d35',
    boundary: '#2b2e36',
    route: '#4864de',
    // Colours of the implicit single mode when meta.modes is absent.
    routeRGB: [72, 100, 222],
    // Red close to green so stacked trails and halos add up to white, not cyan.
    trailRGB: [120, 140, 255],
    title: '#ffffff',
    subtitle: '#8c8f99',
    panel: 'rgba(10,11,16,0.72)',
    clock: '#ffffff',
    accent: '#ffe066',
    breakdown: '#9a9da6',
    axis: '#9a9da6',
    credit: '#6f737d',
  },
  // Used when meta.attribution is absent (the pre-v2 Tsukuba file).
  ATTRIBUTION: [
    'Data: Tsukuba City GTFS-JP (CC BY 4.0)',
    'Map: Overture Maps · © OpenStreetMap contributors',
    'Made by Katsuma Onishi',
  ],
};

const W = CONFIG.WIDTH;
const H = CONFIG.HEIGHT;
const params = new URLSearchParams(location.search);
const RECORD = params.has('record') && params.get('record') !== '0';
// A bad ?city is reported through the LOADING status rather than thrown here,
// so the page still exposes window.busmap and the video script sees the error.
let startupError = null;
if (params.has('city')) {
  const city = CITIES[params.get('city')];
  if (!city) {
    startupError = new Error(`unknown city "${params.get('city')}"; known: ${Object.keys(CITIES).join(', ')}`);
  } else {
    const ext = city.gz ? '.json.gz' : '.json';
    CONFIG.BASEMAP_URL = `${city.dir}/basemap${ext}`;
    CONFIG.NETWORK_URL = `${city.dir}/network${ext}`;
    CONFIG.GZIP = city.gz;
  }
}
if (params.has('data')) CONFIG.NETWORK_URL = params.get('data');
if (params.has('basemap')) CONFIG.BASEMAP_URL = params.get('basemap');
if (params.has('gz')) CONFIG.GZIP = params.get('gz') !== '0';
if (params.has('osm')) CONFIG.OSM_ROUTES = params.get('osm') !== '0';
if (params.has('trailmode')) CONFIG.TRAIL_MODE = params.get('trailmode');
// Numeric trail knobs for experiments: query name, CONFIG key, value when the
// query is not a number. A knob given here is pinned against LARGE_FRAME.
const KNOBS = [
  ['trailscale', 'TRAIL_SCALE', 1],
  ['trailstep', 'TRAIL_STEP_S', 15],
  ['trailmin', 'TRAIL_MINUTES', 25],
  ['trailbands', 'TRAIL_BANDS', 16],
  ['shoulder', 'TRAIL_SHOULDER_ALPHA', 0],
  ['shoulderbands', 'TRAIL_SHOULDER_BANDS', 0],
  ['simplify', 'TRAIL_SIMPLIFY_PX', 1],
  ['trailalpha', 'TRAIL_ALPHA', 0.35],
  ['routealpha', 'ROUTE_ALPHA', 0.1],
  ['corew', 'TRAIL_CORE_W', 5],
  ['shoulderw', 'TRAIL_SHOULDER_W', 13],
];
const pinned = new Set();
for (const [param, key, fallback] of KNOBS) {
  if (!params.has(param)) continue;
  CONFIG[key] = Number(params.get(param)) || fallback;
  pinned.add(key);
}
// Applied after meta so a test can put the panel on either side of any city.
const HUD_OVERRIDE = params.get('hud');

const canvas = document.getElementById('frame');
canvas.width = W;
canvas.height = H;
const ctx = canvas.getContext('2d', { alpha: false });

// Projection: km east/north of origin to canvas pixels, y flipped. Fixed by
// setFrame() once meta is known, before anything is projected.
let SCALE = 0;
let OX = 0;
let OY = 0;

// Static caches, filled by init().
let meta = null;
let shapes = null;
let trips = null;
let hist = null;
// Modes: meta.modes, or one implicit bus mode in the pre-v2 colours. Each
// gets its own trail colour string, sprites and ribbon paths.
let modes = null;
let tripMode = null;       // Uint8Array, mode index per trip
let shapeMode = null;      // Uint8Array, mode index per shape (from its trips)
let tripT0 = null;         // Float64Array, departure per trip
let tripT1 = null;         // Float64Array, arrival per trip
let tripsSorted = false;   // by departure, which lets renderAt stop scanning early
let runningByMode = null;  // Int32Array, refilled each frame
let baseCanvas = null;
let trailCanvas = null;
let trailCtx = null;
let trailSprites = null;   // [mode][age step], alpha baked in
let trailHalf = 0;
let busSprites = null;     // [mode]
let busHalf = 0;
let spark = null;
let hud = null;            // panel geometry, see buildHudLayout
let totalFrames = 0;
let trailWindow = 0;       // seconds of trail kept behind each vehicle
let trailSteps = 0;
// Scratch buffers for one trip's trail samples, overwritten per trip.
let sampleX = null;
let sampleY = null;
let sampleD = null;
// Per shape: simplified screen-space polyline {x, y, cum} for ribbon trails.
let ribbonShapes = null;
let bandPaths = null;      // [mode][band] Path2D, rebuilt each frame

// ---------------------------------------------------------------- helpers

function fetchJSON(url) {
  const gz = CONFIG.GZIP || /\.gz$/i.test(url.split(/[?#]/)[0]);
  return fetch(url).then((r) => {
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    // The static server hands .gz files over as application/gzip with no
    // Content-Encoding, so the browser leaves inflating to us.
    if (gz) return new Response(r.body.pipeThrough(new DecompressionStream('gzip'))).json();
    return r.json();
  });
}

function rgba(rgb, a) {
  return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`;
}

function withCommas(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function clockText(T) {
  const s = ((T % 86400) + 86400) % 86400;
  let h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ap = h < 12 ? 'am' : 'pm';
  h %= 12;
  if (h === 0) h = 12;
  return `${h}:${String(m).padStart(2, '0')} ${ap}`;
}

function frameTime(i) {
  const run = Math.min(Math.max(i - CONFIG.HOLD_START, 0), CONFIG.DURATION_FRAMES);
  return timeAtProgress(run / CONFIG.DURATION_FRAMES);
}

// Largest index in [0, hi] with arr[idx] <= v, or 0 when there is none. arr is
// non-decreasing. This is the same index the backwards walks in sampleTrail
// reach, found in log time for the first sample of a long shape.
function lastLE(arr, v, hi) {
  let lo = 0;
  if (arr[lo] > v) return 0;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] <= v) lo = mid; else hi = mid;
  }
  return arr[hi] <= v ? hi : lo;
}

// Playback progress u in [0, 1] maps to simulated time through a per-minute
// weight table, so quiet stretches of the day take less screen time.
let warp = null;
function buildWarp() {
  const m0 = Math.floor(meta.day_start / 60);
  const m1 = Math.ceil(meta.day_end / 60);
  const n = m1 - m0;
  const w = new Float64Array(n);
  const margin = CONFIG.TIME_WARP_MARGIN_MIN;
  for (let i = 0; i < n; i++) {
    if (!CONFIG.TIME_WARP) { w[i] = 1; continue; }
    // Distance in minutes to the nearest minute with a vehicle on the road;
    // the weight ramps down across the margin so the clock never jumps speed.
    let dist = margin + 1;
    for (let k = 0; k <= margin && dist > margin; k++) {
      if (hist[m0 + i - k] > 0 || hist[m0 + i + k] > 0) dist = k;
    }
    const e = CONFIG.TIME_WARP_EMPTY;
    w[i] = dist > margin ? e : e + (1 - e) * (1 - dist / margin);
  }
  const cum = new Float64Array(n + 1);
  for (let i = 0; i < n; i++) cum[i + 1] = cum[i] + w[i];
  warp = { m0, n, w, cum, total: cum[n] };
}

function timeAtProgress(u) {
  u = Math.min(1, Math.max(0, u));
  const target = u * warp.total;
  let lo = 0, hi = warp.n;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (warp.cum[mid] <= target) lo = mid; else hi = mid;
  }
  const frac = warp.w[lo] > 0 ? (target - warp.cum[lo]) / warp.w[lo] : 0;
  const T = (warp.m0 + lo + Math.min(1, frac)) * 60;
  return Math.min(meta.day_end, Math.max(meta.day_start, T));
}

function progressAt(T) {
  const minutes = T / 60 - warp.m0;
  const i = Math.min(warp.n - 1, Math.max(0, Math.floor(minutes)));
  const u = (warp.cum[i] + (minutes - i) * warp.w[i]) / warp.total;
  return Math.min(1, Math.max(0, u));
}

// A flat [x,y,x,y,...] polyline, skipped when its bounding box misses the frame.
function tracePolyline(g, xy) {
  const n = xy.length;
  let minx = Infinity, maxx = -Infinity, miny = Infinity, maxy = -Infinity;
  for (let i = 0; i < n; i += 2) {
    const x = OX + xy[i] * SCALE;
    const y = OY - xy[i + 1] * SCALE;
    if (x < minx) minx = x;
    if (x > maxx) maxx = x;
    if (y < miny) miny = y;
    if (y > maxy) maxy = y;
  }
  if (maxx < -4 || minx > W + 4 || maxy < -4 || miny > H + 4) return false;
  g.moveTo(OX + xy[0] * SCALE, OY - xy[1] * SCALE);
  for (let i = 2; i < n; i += 2) g.lineTo(OX + xy[i] * SCALE, OY - xy[i + 1] * SCALE);
  return true;
}

// Many polylines in one stroke call. Chunked so no single path gets huge.
function strokeMany(g, list, chunk = 3000) {
  for (let s = 0; s < list.length; s += chunk) {
    g.beginPath();
    const end = Math.min(list.length, s + chunk);
    for (let i = s; i < end; i++) tracePolyline(g, list[i]);
    g.stroke();
  }
}

function makeRadialSprite(radius, stops) {
  const size = Math.ceil(radius * 2) + 2;
  const c = document.createElement('canvas');
  c.width = size;
  c.height = size;
  const g = c.getContext('2d');
  const mid = size / 2;
  const grad = g.createRadialGradient(mid, mid, 0, mid, mid, radius);
  for (const [pos, color] of stops) grad.addColorStop(pos, color);
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  return c;
}

// ------------------------------------------------------------- init

function setFrame(frame) {
  if (frame) {
    if (Number.isFinite(frame.km_vertical) && frame.km_vertical > 0) CONFIG.KM_VERTICAL = frame.km_vertical;
    if (Array.isArray(frame.center_km) && frame.center_km.length === 2) CONFIG.CENTER_KM = frame.center_km;
    if (frame.hud_side === 'left' || frame.hud_side === 'right') CONFIG.HUD_SIDE = frame.hud_side;
  }
  if (HUD_OVERRIDE === 'left' || HUD_OVERRIDE === 'right') CONFIG.HUD_SIDE = HUD_OVERRIDE;
  SCALE = H / CONFIG.KM_VERTICAL;
  OX = W / 2 - CONFIG.CENTER_KM[0] * SCALE;
  OY = H / 2 + CONFIG.CENTER_KM[1] * SCALE;
  if (CONFIG.KM_VERTICAL >= CONFIG.LARGE_FRAME_KM) {
    for (const [key, value] of Object.entries(CONFIG.LARGE_FRAME)) {
      if (!pinned.has(key)) CONFIG[key] = value;
    }
  }
}

function buildModes(network) {
  const C = CONFIG.COLORS;
  const list = Array.isArray(meta.modes) && meta.modes.length ? meta.modes : null;
  modes = list
    ? list.map((m) => ({
      id: m.id,
      label: m.label || m.id,
      singular: m.singular || m.label || m.id,
      color: m.color || C.routeRGB,
      trail: m.trail || C.trailRGB,
    }))
    : [{ id: 'bus', label: 'buses', singular: 'bus', color: C.routeRGB, trail: C.trailRGB }];
  for (const m of modes) m.trailCss = rgba(m.trail, 1);
  const index = new Map(modes.map((m, i) => [m.id, i]));
  // A route whose mode is unknown (or absent, as in pre-v2 files) is the first mode.
  const routeMode = network.routes.map((r) => index.get(r.mode) || 0);
  tripMode = new Uint8Array(trips.length);
  shapeMode = new Uint8Array(shapes.length);
  tripT0 = new Float64Array(trips.length);
  tripT1 = new Float64Array(trips.length);
  runningByMode = new Int32Array(modes.length);
  tripsSorted = true;
  for (let n = 0; n < trips.length; n++) {
    const trip = trips[n];
    const m = routeMode[trip.r] || 0;
    tripMode[n] = m;
    shapeMode[trip.s] = m;
    tripT0[n] = trip.t[0];
    tripT1[n] = trip.t[trip.t.length - 1];
    if (n > 0 && tripT0[n] < tripT0[n - 1]) tripsSorted = false;
  }
}

function buildBase(basemap, network) {
  const C = CONFIG.COLORS;
  const c = document.createElement('canvas');
  c.width = W;
  c.height = H;
  const g = c.getContext('2d', { alpha: false });
  g.fillStyle = C.bg;
  g.fillRect(0, 0, W, H);
  g.lineCap = 'round';
  g.lineJoin = 'round';

  g.fillStyle = C.water;
  g.beginPath();
  for (const ring of basemap.water.poly) {
    if (tracePolyline(g, ring)) g.closePath();
  }
  g.fill();

  g.strokeStyle = C.waterLine;
  const byClass = { river: [], canal: [], stream: [] };
  for (const l of basemap.water.line) (byClass[l.c] || byClass.stream).push(l.xy);
  g.lineWidth = 2;
  strokeMany(g, byClass.river);
  g.lineWidth = 1.4;
  strokeMany(g, byClass.canal);
  g.lineWidth = 1;
  strokeMany(g, byClass.stream);

  g.lineWidth = 1;
  g.strokeStyle = C.minor;
  strokeMany(g, basemap.roads.minor);
  g.lineWidth = 1.6;
  g.strokeStyle = C.major;
  strokeMany(g, basemap.roads.major);
  g.setLineDash([7, 5]);
  g.lineWidth = 1.2;
  g.strokeStyle = C.rail;
  strokeMany(g, basemap.roads.rail);
  g.setLineDash([]);

  g.globalAlpha = 0.4;
  g.lineWidth = 1;
  g.strokeStyle = C.boundary;
  strokeMany(g, basemap.boundary || []);
  g.globalAlpha = 1;

  // Route network. Each shape is its own stroke so overlaps add up under
  // 'lighter'; one combined path would union them and flatten the corridors.
  g.globalCompositeOperation = 'lighter';
  g.lineWidth = CONFIG.ROUTE_WIDTH;
  if (CONFIG.OSM_ROUTES && basemap.osm_routes) {
    g.strokeStyle = rgba(C.routeRGB, CONFIG.OSM_ROUTES_ALPHA);
    g.lineWidth = 1.5;
    for (const r of basemap.osm_routes) {
      g.beginPath();
      if (tracePolyline(g, r.xy)) g.stroke();
    }
    g.lineWidth = CONFIG.ROUTE_WIDTH;
  }
  if (CONFIG.ROUTE_BLEND === 'bounded') {
    // One pass per mode, later modes on top, so a subway line under a bus
    // corridor still shows its own colour instead of adding up to white.
    g.globalCompositeOperation = 'source-over';
    for (let m = 0; m < modes.length; m++) {
      g.strokeStyle = rgba(modes[m].color, CONFIG.ROUTE_ALPHA);
      for (let i = 0; i < network.shapes.length; i++) {
        if (shapeMode[i] !== m) continue;
        g.beginPath();
        if (tracePolyline(g, network.shapes[i].xy)) g.stroke();
      }
    }
    return c;
  }
  // Shapes keep file order; only the colour changes with the mode.
  let cur = -1;
  for (let i = 0; i < network.shapes.length; i++) {
    const m = shapeMode[i];
    if (m !== cur) {
      g.strokeStyle = rgba(modes[m].color, CONFIG.ROUTE_ALPHA);
      cur = m;
    }
    g.beginPath();
    if (tracePolyline(g, network.shapes[i].xy)) g.stroke();
  }
  g.globalCompositeOperation = 'source-over';
  return c;
}

function buildSprites() {
  const ts = CONFIG.TRAIL_SCALE;
  trailWindow = CONFIG.TRAIL_MINUTES * 60;
  trailSteps = Math.round(trailWindow / CONFIG.TRAIL_STEP_S);
  trailSprites = modes.map((mode) => {
    const list = [];
    for (let k = 0; k <= trailSteps; k++) {
      const a = CONFIG.TRAIL_ALPHA * (1 - k / trailSteps);
      list.push(makeRadialSprite(CONFIG.TRAIL_RADIUS * ts, [
        [0, rgba(mode.trail, a)],
        [0.35, rgba(mode.trail, a * 0.6)],
        [0.7, rgba(mode.trail, a * 0.18)],
        [1, rgba(mode.trail, 0)],
      ]));
    }
    return list;
  });
  trailHalf = trailSprites[0][0].width / 2;
  sampleX = new Float64Array(trailSteps + 1);
  sampleY = new Float64Array(trailSteps + 1);
  sampleD = new Float64Array(trailSteps + 1);

  // The halo is white when there is one mode (the look the contract measures)
  // and takes the mode's trail colour when there are several, so a station
  // with trains and streetcars reads as two kinds of vehicle.
  busSprites = modes.map((mode) => {
    const halo = modes.length > 1 ? mode.trail : [255, 255, 255];
    const sprite = makeRadialSprite(CONFIG.BUS_HALO_R, [
      [0, rgba(halo, CONFIG.BUS_HALO_ALPHA)],
      [0.35, rgba(halo, CONFIG.BUS_HALO_ALPHA * 0.45)],
      [0.7, rgba(halo, CONFIG.BUS_HALO_ALPHA * 0.12)],
      [1, rgba(halo, 0)],
    ]);
    const g = sprite.getContext('2d');
    g.fillStyle = '#ffffff';
    g.beginPath();
    g.arc(sprite.width / 2, sprite.width / 2, CONFIG.BUS_CORE_R, 0, Math.PI * 2);
    g.fill();
    return sprite;
  });
  busHalf = busSprites[0].width / 2;

  if (ts !== 1) {
    trailCanvas = document.createElement('canvas');
    trailCanvas.width = Math.round(W * ts);
    trailCanvas.height = Math.round(H * ts);
    trailCtx = trailCanvas.getContext('2d');
  }
}

// ImageBitmaps draw faster than canvas sources in Chromium, so swap them in
// when available; the canvas versions stay as a fallback.
async function promoteSprites() {
  if (typeof createImageBitmap !== 'function') return;
  try {
    const trails = await Promise.all(trailSprites.map((list) => Promise.all(list.map((c) => createImageBitmap(c)))));
    const buses = await Promise.all(busSprites.map((c) => createImageBitmap(c)));
    trailSprites = trails;
    busSprites = buses;
  } catch (e) {
    // keep canvases
  }
}

// Douglas-Peucker in screen space. Stroke cost here is per segment, and GTFS
// shapes carry a vertex every 40 m or so, most of them on straight road, so
// dropping anything within TRAIL_SIMPLIFY_PX of the chord cuts the vertex
// count by about 6x with no visible change under a 5 px line.
function simplifyShape(shape) {
  const xy = shape.xy, cum = shape.cum;
  const n = xy.length / 2;
  const px = new Float64Array(n), py = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    px[i] = OX + xy[2 * i] * SCALE;
    py[i] = OY - xy[2 * i + 1] * SCALE;
  }
  const keep = new Uint8Array(n);
  keep[0] = 1;
  keep[n - 1] = 1;
  const tol = CONFIG.TRAIL_SIMPLIFY_PX;
  const stack = [0, n - 1];
  while (stack.length) {
    const b = stack.pop(), a = stack.pop();
    if (b - a < 2) continue;
    const ax = px[a], ay = py[a], dx = px[b] - ax, dy = py[b] - ay;
    const len = Math.hypot(dx, dy);
    let best = -1, bi = -1;
    for (let i = a + 1; i < b; i++) {
      const dist = len === 0
        ? Math.hypot(px[i] - ax, py[i] - ay)
        : Math.abs(dx * (ay - py[i]) - (ax - px[i]) * dy) / len;
      if (dist > best) { best = dist; bi = i; }
    }
    if (best > tol) {
      keep[bi] = 1;
      stack.push(a, bi, bi, b);
    }
  }
  let m = 0;
  for (let i = 0; i < n; i++) m += keep[i];
  const x = new Float64Array(m), y = new Float64Array(m), c = new Float64Array(m);
  for (let i = 0, j = 0; i < n; i++) {
    if (!keep[i]) continue;
    x[j] = px[i];
    y[j] = py[i];
    c[j] = cum[i];
    j++;
  }
  return { x, y, cum: c };
}

function buildRibbons() {
  ribbonShapes = shapes.map(simplifyShape);
  bandPaths = modes.map(() => []);
}

// Panel geometry. The pre-v2 numbers are the left-hand, one-mode, three-line
// case; everything else is an offset from them.
function buildHudLayout() {
  const dx = CONFIG.HUD_SIDE === 'right' ? 400 : 0;
  const multi = modes.length > 1;
  // The mode breakdown line sits under the count and pushes the chart down.
  const shift = multi ? 30 : 0;
  const lines = Array.isArray(meta.attribution) && meta.attribution.length ? meta.attribution : CONFIG.ATTRIBUTION;
  const attrY = 1562 + shift;
  const lastBaseline = attrY + (lines.length - 1) * 23;
  hud = {
    dx,
    multi,
    lines,
    panelX: 40 + dx,
    panelY: 1120,
    panelW: 600,
    // 12 px under the last attribution baseline: with the three Tsukuba lines
    // that is the 1620 the frames were measured with.
    panelH: lastBaseline + 12 - 1120,
    textX: 70 + dx,
    clockY: 1275,
    countY: 1342,
    breakdownY: 1374,
    breakdownFont: '500 24px Montserrat',
    sparkX0: 70 + dx,
    sparkX1: 610 + dx,
    sparkY0: 1380 + shift,
    sparkY1: 1500 + shift,
    axisY: 1530 + shift,
    attrY,
  };
  const maxTextW = 540;
  if (multi) {
    // Size the breakdown once for the widest line the day can produce (each
    // mode at its own peak), so the text never jitters between frames.
    const byMode = meta.hist_by_mode || {};
    const widest = modes.map((m) => {
      const h = byMode[m.id];
      const peak = Array.isArray(h) && h.length ? Math.max(...h) : (meta.peak && meta.peak.count) || 0;
      return `${withCommas(peak)} ${peak === 1 ? m.singular : m.label}`;
    }).join(' · ');
    for (let size = 24; size >= 14; size--) {
      hud.breakdownFont = `500 ${size}px Montserrat`;
      ctx.font = hud.breakdownFont;
      if (ctx.measureText(widest).width <= maxTextW) break;
    }
    if (hud.breakdownFont !== '500 24px Montserrat') console.warn(`breakdown line "${widest}" needs ${hud.breakdownFont} to fit ${maxTextW} px`);
  }
  ctx.font = '400 18px Inter';
  for (const line of lines) {
    const w = ctx.measureText(line).width;
    if (w > maxTextW) console.warn(`attribution line "${line}" is ${Math.round(w)} px, wider than the ${maxTextW} px panel text width`);
  }
}

function buildSparkline() {
  const x0 = hud.sparkX0, x1 = hud.sparkX1, y0 = hud.sparkY0, y1 = hud.sparkY1;
  const mStart = meta.day_start / 60;
  const mEnd = meta.day_end / 60;
  // Centred moving average: the count label stays exact, only the curve is
  // smoothed, like the reference's rounded profile.
  const half = Math.floor((CONFIG.SPARK_SMOOTH_MIN || 1) / 2);
  const smooth = new Float64Array(hist.length);
  for (let m = 0; m < hist.length; m++) {
    let sum = 0, n = 0;
    for (let k = m - half; k <= m + half; k++) {
      if (k < 0 || k >= hist.length) continue;
      sum += hist[k];
      n++;
    }
    smooth[m] = n ? sum / n : 0;
  }
  let peak = 0;
  for (let m = Math.floor(mStart); m <= Math.ceil(mEnd) && m < smooth.length; m++) {
    if (smooth[m] > peak) peak = smooth[m];
  }
  if (peak <= 0) peak = 1;
  const xs = [], ys = [];
  for (let m = Math.ceil(mStart); m <= Math.floor(mEnd); m++) {
    xs.push(x0 + ((m - mStart) / (mEnd - mStart)) * (x1 - x0));
    ys.push(y1 - ((smooth[m] || 0) / peak) * (y1 - y0 - 10));
  }
  spark = { x0, x1, y0, y1, mStart, mEnd, peak, xs, ys, smooth };
}

// Smoothed vehicle count at minute-resolution T, interpolated so the
// sparkline head sits on the curve.
function histAt(minutes) {
  const sm = spark.smooth;
  const i = Math.floor(minutes);
  const f = minutes - i;
  const a = sm[i] || 0;
  const b = sm[i + 1] === undefined ? a : sm[i + 1];
  return a + (b - a) * f;
}

async function init() {
  if (startupError) throw startupError;
  const [basemap, network] = await Promise.all([
    fetchJSON(CONFIG.BASEMAP_URL),
    fetchJSON(CONFIG.NETWORK_URL),
  ]);
  meta = network.meta;
  shapes = network.shapes;
  trips = network.trips;
  hist = network.hist;
  totalFrames = CONFIG.HOLD_START + CONFIG.DURATION_FRAMES + CONFIG.HOLD_END;
  setFrame(meta.frame);
  buildWarp();
  buildModes(network);

  // Canvas text only triggers a font load on first use, so request every
  // face explicitly before waiting on document.fonts.ready.
  const faces = [
    '600 58px Montserrat', '400 38px Montserrat', '800 108px MontserratTnum',
    '600 32px Montserrat', '500 24px Montserrat', '500 20px Montserrat', '400 18px Inter',
  ];
  await Promise.all(faces.map((f) => document.fonts.load(f).catch(() => null)));
  await document.fonts.ready;

  baseCanvas = buildBase(basemap, network);
  buildSprites();
  await promoteSprites();
  buildRibbons();
  buildHudLayout();
  buildSparkline();
  buildHudStatics();
  return { basemap, network };
}

// ------------------------------------------------------------- per frame

// Walks a trip backwards from T and fills sampleX/sampleY/sampleD with the
// vehicle position (and km along the shape) at each of `count` samples spaced
// `step` seconds apart, where the trip was under way. Returns the inclusive
// sample range [first, last] packed as first * 1024 + last, or -1 when
// nothing is on the road. Indices into t[] and cum[] only move backwards
// because d and cum are non-decreasing in time; the first sample finds them
// by bisection so a 2,000-vertex GO train shape is not walked end to end.
function sampleTrail(trip, T, ts, step, count) {
  const t = trip.t, d = trip.d;
  const shape = shapes[trip.s];
  const cum = shape.cum, xy = shape.xy;
  const tEnd = t[t.length - 1];
  const t0 = t[0];
  let i = -1;
  let k = -1;
  let first = -1;
  let last = -1;
  for (let s = 0; s < count; s++) {
    let tt = T - s * step;
    if (tt < t0) {
      // Clamp the final sample to the departure so a ribbon band reaches the
      // first stop instead of stopping one step short of it.
      if (last < 0 || tt + step <= t0) break;
      tt = t0;
    }
    if (tt > tEnd) {
      // Pin the newest sample to the arrival so a finished trip's trail rests
      // at the terminus instead of creeping as the sample grid slides.
      if (tt - step > tEnd) continue;
      tt = tEnd;
    }
    if (i < 0) i = lastLE(t, tt, t.length - 2);
    else while (i > 0 && t[i] > tt) i--;
    let dist;
    const dt = t[i + 1] - t[i];
    if (dt <= 0) dist = d[i];
    else dist = d[i] + ((tt - t[i]) / dt) * (d[i + 1] - d[i]);
    if (k < 0) k = lastLE(cum, dist, cum.length - 2);
    else while (k > 0 && cum[k] > dist) k--;
    const seg = cum[k + 1] - cum[k];
    const f = seg > 0 ? Math.min(1, Math.max(0, (dist - cum[k]) / seg)) : 0;
    sampleX[s] = (OX + (xy[2 * k] + (xy[2 * k + 2] - xy[2 * k]) * f) * SCALE) * ts;
    sampleY[s] = (OY - (xy[2 * k + 1] + (xy[2 * k + 3] - xy[2 * k + 1]) * f) * SCALE) * ts;
    sampleD[s] = dist;
    if (first < 0) first = s;
    last = s;
    if (tt === t0) break;
  }
  return first < 0 ? -1 : first * 1024 + last;
}

// Appends the stretch of a simplified shape between km dA and dB (dA < dB)
// to a Path2D, interpolating both ends so bands meet exactly.
function appendStretch(path, rs, dA, dB) {
  const cum = rs.cum, x = rs.x, y = rs.y;
  const n = cum.length;
  let k = 0, hi = n - 1;
  // first segment k with cum[k] <= dA < cum[k+1]
  while (hi - k > 1) {
    const mid = (k + hi) >> 1;
    if (cum[mid] <= dA) k = mid; else hi = mid;
  }
  let seg = cum[k + 1] - cum[k];
  let f = seg > 0 ? Math.min(1, Math.max(0, (dA - cum[k]) / seg)) : 0;
  path.moveTo(x[k] + (x[k + 1] - x[k]) * f, y[k] + (y[k + 1] - y[k]) * f);
  k++;
  while (k < n - 1 && cum[k] < dB) {
    path.lineTo(x[k], y[k]);
    k++;
  }
  seg = cum[k] - cum[k - 1];
  f = seg > 0 ? Math.min(1, (dB - cum[k - 1]) / seg) : 1;
  path.lineTo(x[k - 1] + (x[k] - x[k - 1]) * f, y[k - 1] + (y[k] - y[k - 1]) * f);
}

// Ribbon mode: one sample per band boundary, then each band's stretch goes
// into the shared path for that (mode, band).
function addRibbon(trip, paths, first, last) {
  const rs = ribbonShapes[trip.s];
  if (rs.cum.length < 2) return;
  for (let b = first; b < last; b++) {
    const dA = sampleD[b + 1], dB = sampleD[b];
    if (dB - dA < 1e-4) continue;
    appendStretch(paths[b], rs, dA, dB);
  }
}

// Offscreen canvas per mode for bounded trail blending, made on first use.
const modeLayers = [];
function modeLayer(m) {
  if (!modeLayers[m]) {
    const c = document.createElement('canvas');
    c.width = W;
    c.height = H;
    modeLayers[m] = c.getContext('2d');
  }
  return modeLayers[m];
}

function strokeRibbons(layer, ts, onlyMode) {
  const bands = CONFIG.TRAIL_BANDS;
  layer.lineCap = 'butt';
  layer.lineJoin = 'round';
  // Paths are in frame pixels; a scaled layer maps them with a transform so
  // the line widths scale along.
  if (ts !== 1) layer.setTransform(ts, 0, 0, ts, 0, 0);
  for (let m = 0; m < modes.length; m++) {
    if (onlyMode !== undefined && m !== onlyMode) continue;
    const paths = bandPaths[m];
    layer.strokeStyle = modes[m].trailCss;
    for (let b = 0; b < bands; b++) {
      const path = paths[b];
      if (!path) continue;
      const a = CONFIG.TRAIL_ALPHA * (1 - (b + 0.5) / bands);
      if (CONFIG.TRAIL_SHOULDER_ALPHA > 0 && b < CONFIG.TRAIL_SHOULDER_BANDS) {
        layer.globalAlpha = a * CONFIG.TRAIL_SHOULDER_ALPHA;
        layer.lineWidth = CONFIG.TRAIL_SHOULDER_W;
        layer.stroke(path);
      }
      layer.globalAlpha = a;
      layer.lineWidth = CONFIG.TRAIL_CORE_W;
      layer.stroke(path);
    }
  }
  if (ts !== 1) layer.setTransform(1, 0, 0, 1, 0, 0);
  layer.globalAlpha = 1;
}

// Sprite mode: integer positions, because a fractional drawImage goes through
// bilinear resampling and costs twice as much in software raster.
function stampTrailSprites(layer, sprites, first, last) {
  const half = trailHalf;
  const w = layer.canvas.width + half, h = layer.canvas.height + half;
  for (let s = first; s <= last; s++) {
    const x = sampleX[s], y = sampleY[s];
    if (x < -half || x > w || y < -half || y > h) continue;
    layer.drawImage(sprites[s], Math.round(x - half), Math.round(y - half));
  }
}

function drawHUD(T, running) {
  const C = CONFIG.COLORS;
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  ctx.textBaseline = 'alphabetic';

  // Title with tracking. Chromium adds the spacing after every glyph
  // including the last, so nudge by half a space to keep it optically centred.
  const spacing = 58 * 0.20;
  ctx.fillStyle = C.title;
  ctx.font = '600 58px Montserrat';
  ctx.textAlign = 'center';
  if ('letterSpacing' in ctx) {
    ctx.letterSpacing = `${spacing}px`;
    ctx.fillText(meta.title || 'TSUKUBA BUSES', W / 2 + spacing / 2, 150);
    ctx.letterSpacing = '0px';
  } else {
    const text = meta.title || 'TSUKUBA BUSES';
    const widths = [...text].map((ch) => ctx.measureText(ch).width);
    const total = widths.reduce((a, b) => a + b, 0) + spacing * (text.length - 1);
    let x = W / 2 - total / 2;
    ctx.textAlign = 'left';
    [...text].forEach((ch, idx) => {
      ctx.fillText(ch, x, 150);
      x += widths[idx] + spacing;
    });
  }
  ctx.textAlign = 'center';
  ctx.fillStyle = C.subtitle;
  ctx.font = '400 38px Montserrat';
  ctx.fillText(meta.subtitle || '', W / 2, 210);

  // Panel, feathered like the reference so routes passing under its edge
  // fade instead of snapping.
  ctx.drawImage(panelSprite, 0, 0);

  const tx = hud.textX;
  ctx.textAlign = 'left';
  ctx.fillStyle = C.clock;
  ctx.font = '800 108px MontserratTnum';
  ctx.fillText(clockText(T), tx, hud.clockY);

  ctx.fillStyle = C.accent;
  ctx.font = '600 32px Montserrat';
  if (hud.multi) {
    ctx.fillText(`${withCommas(running)} ${running === 1 ? 'vehicle' : 'vehicles'} running`, tx, hud.countY);
    ctx.fillStyle = C.breakdown;
    ctx.font = hud.breakdownFont;
    const parts = [];
    for (let m = 0; m < modes.length; m++) {
      const n = runningByMode[m];
      parts.push(`${withCommas(n)} ${n === 1 ? modes[m].singular : modes[m].label}`);
    }
    ctx.fillText(parts.join(' · '), tx, hud.breakdownY);
  } else {
    const mode = modes[0];
    ctx.fillText(`${withCommas(running)} ${running === 1 ? mode.singular : mode.label} running`, tx, hud.countY);
  }

  // Sparkline up to the current time.
  const sp = spark;
  const minutes = T / 60;
  const frac = Math.min(1, Math.max(0, (minutes - sp.mStart) / (sp.mEnd - sp.mStart)));
  const xCur = sp.x0 + frac * (sp.x1 - sp.x0);
  const yCur = sp.y1 - (Math.min(histAt(minutes), sp.peak) / sp.peak) * (sp.y1 - sp.y0 - 10);
  const lastIdx = Math.floor(minutes) - Math.ceil(sp.mStart);
  ctx.beginPath();
  ctx.moveTo(sp.x0, sp.y1);
  ctx.lineTo(sp.x0, sp.y1 - (Math.min(histAt(sp.mStart), sp.peak) / sp.peak) * (sp.y1 - sp.y0 - 10));
  for (let j = 0; j <= lastIdx && j < sp.xs.length; j++) ctx.lineTo(sp.xs[j], sp.ys[j]);
  ctx.lineTo(xCur, yCur);
  ctx.lineTo(xCur, sp.y1);
  ctx.closePath();
  ctx.fillStyle = sparkFill;
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(sp.x0, sp.y1 - (Math.min(histAt(sp.mStart), sp.peak) / sp.peak) * (sp.y1 - sp.y0 - 10));
  for (let j = 0; j <= lastIdx && j < sp.xs.length; j++) ctx.lineTo(sp.xs[j], sp.ys[j]);
  ctx.lineTo(xCur, yCur);
  ctx.strokeStyle = C.accent;
  ctx.lineWidth = 3;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.stroke();
  // Chart floor.
  ctx.beginPath();
  ctx.moveTo(sp.x0, sp.y1 + 0.5);
  ctx.lineTo(sp.x1, sp.y1 + 0.5);
  ctx.strokeStyle = '#3a3b43';
  ctx.lineWidth = 1;
  ctx.stroke();

  ctx.fillStyle = C.axis;
  ctx.font = '500 20px Montserrat';
  ctx.textAlign = 'left';
  ctx.fillText(clockText(meta.day_start), sp.x0, hud.axisY);
  ctx.textAlign = 'right';
  ctx.fillText(clockText(meta.day_end), sp.x1, hud.axisY);

  ctx.fillStyle = C.credit;
  ctx.font = '400 18px Inter';
  ctx.textAlign = 'left';
  for (let i = 0; i < hud.lines.length; i++) {
    ctx.fillText(hud.lines[i], tx, hud.attrY + i * 23);
  }
}

// The panel backdrop and the sparkline gradient are static, so build them
// once; a Gaussian blur per frame would cost more than the whole map.
let panelSprite = null;
let sparkFill = null;
function buildHudStatics() {
  panelSprite = document.createElement('canvas');
  panelSprite.width = W;
  panelSprite.height = H;
  const g = panelSprite.getContext('2d');
  g.filter = 'blur(36px)';
  g.fillStyle = CONFIG.COLORS.panel;
  g.beginPath();
  g.roundRect(hud.panelX, hud.panelY, hud.panelW, hud.panelH, 24);
  g.fill();
  g.filter = 'none';
  const grad = ctx.createLinearGradient(0, spark.y0, 0, spark.y1);
  grad.addColorStop(0, rgba([255, 224, 102], 0.5));
  grad.addColorStop(1, rgba([255, 224, 102], 0.03));
  sparkFill = grad;
}

function renderAt(T) {
  if (!baseCanvas) throw new Error('busmap not ready');
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  ctx.drawImage(baseCanvas, 0, 0);

  const bounded = CONFIG.TRAIL_BLEND === 'bounded' && CONFIG.TRAIL_MODE !== 'sprite';
  const ts = bounded ? 1 : CONFIG.TRAIL_SCALE;
  const layer = ts === 1 ? ctx : trailCtx;
  if (ts !== 1) {
    layer.globalCompositeOperation = 'source-over';
    layer.clearRect(0, 0, trailCanvas.width, trailCanvas.height);
  }
  layer.globalCompositeOperation = 'lighter';

  const buses = [];
  const horizon = T - trailWindow;
  const sprite = CONFIG.TRAIL_MODE === 'sprite';
  const step = sprite ? CONFIG.TRAIL_STEP_S : trailWindow / CONFIG.TRAIL_BANDS;
  const count = sprite ? trailSteps + 1 : CONFIG.TRAIL_BANDS + 1;
  if (!sprite) {
    for (let m = 0; m < modes.length; m++) {
      for (let b = 0; b < CONFIG.TRAIL_BANDS; b++) bandPaths[m][b] = new Path2D();
    }
  }
  runningByMode.fill(0);
  let running = 0;
  for (let n = 0; n < trips.length; n++) {
    // Trips are sorted by departure, so nothing after the first future trip
    // can be on the road. Finished trips keep fading for the trail window.
    if (tripT0[n] > T) {
      if (tripsSorted) break;
      continue;
    }
    if (tripT1[n] < horizon) continue;
    const trip = trips[n];
    const range = sampleTrail(trip, T, ts, step, count);
    if (range < 0) continue;
    const first = Math.floor(range / 1024);
    const last = range % 1024;
    const m = tripMode[n];
    if (first === 0 && T <= tripT1[n]) {
      running++;
      runningByMode[m]++;
      buses.push(sampleX[0] / ts, sampleY[0] / ts, m);
    }
    if (sprite) stampTrailSprites(layer, trailSprites[m], first, last);
    else addRibbon(trip, bandPaths[m], first, last);
  }
  if (!sprite && CONFIG.TRAIL_BLEND === 'bounded') {
    // Each mode composes with itself under normal alpha in its own layer;
    // the layers then add onto the frame.
    for (let m = 0; m < modes.length; m++) {
      const ml = modeLayer(m);
      ml.globalCompositeOperation = 'source-over';
      ml.clearRect(0, 0, W, H);
      strokeRibbons(ml, 1, m);
      ctx.globalCompositeOperation = 'lighter';
      ctx.drawImage(ml.canvas, 0, 0);
    }
  } else {
    if (!sprite) strokeRibbons(layer, ts);
    if (ts !== 1) {
      ctx.globalCompositeOperation = 'lighter';
      ctx.drawImage(trailCanvas, 0, 0, W, H);
    }
  }

  ctx.globalCompositeOperation = 'lighter';
  for (let b = 0; b < buses.length; b += 3) {
    const x = buses[b], y = buses[b + 1];
    if (x < -busHalf || x > W + busHalf || y < -busHalf || y > H + busHalf) continue;
    ctx.drawImage(busSprites[buses[b + 2]], x - busHalf, y - busHalf);
  }

  drawHUD(T, running);
  return running;
}

function renderFrame(i) {
  const idx = Math.min(Math.max(Math.floor(i), 0), totalFrames - 1);
  const T = frameTime(idx);
  renderAt(T);
  return T;
}

// ------------------------------------------------------------- page glue

const ready = init().then(() => {
  const status = document.getElementById('status');
  if (status) status.remove();
  return true;
}).catch((err) => {
  const status = document.getElementById('status');
  if (status) status.textContent = `FAILED: ${err.message}`;
  console.error(err);
  throw err;
});

const busmap = {
  ready,
  get totalFrames() { return totalFrames; },
  renderFrame,
  renderAt,
  config: CONFIG,
  canvas,
  get meta() { return meta; },
  get hist() { return hist; },
  get modes() { return modes; },
  get runningByMode() { return runningByMode; },
  get hud() { return hud; },
  clockText,
  frameTime: (i) => frameTime(i),
  progressAt: (T) => progressAt(T),
  timeAtProgress: (u) => timeAtProgress(u),
};
window.busmap = busmap;

function parseTime(v) {
  if (v == null) return null;
  const m = /^(\d{1,2}):(\d{2})$/.exec(v);
  if (m) return Number(m[1]) * 3600 + Number(m[2]) * 60;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

if (RECORD) {
  document.body.classList.add('record');
  for (const id of ['controls', 'status']) {
    const el = document.getElementById(id);
    if (el) el.remove();
  }
  ready.then(() => {
    const t = parseTime(params.get('t'));
    const f = params.get('frame');
    if (t != null) renderAt(t);
    else if (f != null && Number.isFinite(Number(f))) renderFrame(Number(f));
    else renderFrame(0);
  });
} else {
  ready.then(() => {
    const scrub = document.getElementById('scrub');
    const playBtn = document.getElementById('play');
    const timeEl = document.getElementById('time');
    const playSeconds = CONFIG.DURATION_FRAMES / CONFIG.FPS;
    let T = meta.day_start;
    const t0 = parseTime(params.get('t'));
    if (t0 != null) T = Math.min(meta.day_end, Math.max(meta.day_start, t0));
    let playing = false;
    let last = 0;

    function show() {
      const running = renderAt(T);
      scrub.value = String(Math.round(progressAt(T) * 10000));
      timeEl.textContent = `${clockText(T)} · ${withCommas(running)} running`;
    }
    function tick(now) {
      if (!playing) return;
      if (last) {
        const u = progressAt(T) + (now - last) / 1000 / playSeconds;
        T = u >= 1 ? meta.day_start : timeAtProgress(u);
      }
      last = now;
      show();
      requestAnimationFrame(tick);
    }
    function setPlaying(p) {
      playing = p;
      last = 0;
      playBtn.textContent = p ? 'Pause' : 'Play';
      if (p) requestAnimationFrame(tick);
    }
    scrub.addEventListener('input', () => {
      T = timeAtProgress(Number(scrub.value) / 10000);
      show();
    });
    playBtn.addEventListener('click', () => setPlaying(!playing));
    window.addEventListener('keydown', (e) => {
      if (e.code === 'Space') {
        e.preventDefault();
        setPlaying(!playing);
      }
    });
    show();
  });
}

})();
