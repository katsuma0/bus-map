/* Transit day renderer. See docs/CONTRACT.md, "Visual spec", "v2" and "v3".
 *
 * render(T) is a pure function of simulated time T (seconds since midnight,
 * may exceed 86400). The only state kept between frames is the cached static
 * base map, the glow sprites, the HUD layout and the sparkline geometry, all
 * built once in init(). Playwright drives renderFrame(i) in any order through
 * window.busmap.
 *
 * Everything city specific (frame, modes, groups, colours, title, attribution,
 * render tuning) travels in network.json meta; the page only needs to know
 * where each city's files are.
 */
(() => {
'use strict';

// Built data per city, relative to web/index.html. gz: the builders wrote
// network.json.gz and basemap.json.gz instead of plain JSON. basemapDir: where
// the basemap is when it is not next to the network; the trains and buses
// videos of a Japanese area share one area basemap (and one origin).
const CITIES = {
  tsukuba: { dir: '../data/built', gz: false },
  gta: { dir: '../data/gta/built', gz: true },
  'tokyo-trains': { dir: '../data/tokyo-trains/built', basemapDir: '../data/tokyo/built', gz: true },
  'tokyo-buses': { dir: '../data/tokyo-buses/built', basemapDir: '../data/tokyo/built', gz: true },
  'kyoto-trains': { dir: '../data/kyoto-trains/built', basemapDir: '../data/kyoto/built', gz: true },
  'kyoto-buses': { dir: '../data/kyoto-buses/built', basemapDir: '../data/kyoto/built', gz: true },
  'osaka-trains': { dir: '../data/osaka-trains/built', basemapDir: '../data/osaka/built', gz: true },
  'osaka-buses': { dir: '../data/osaka-buses/built', basemapDir: '../data/osaka/built', gz: true },
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
    TRAIL_ALPHA: 0.35,
    TRAIL_CORE_W: 3,
    TRAIL_SHOULDER_W: 12,
    TRAIL_SHOULDER_ALPHA: 0.35,
    TRAIL_SHOULDER_BANDS: 5,
    TRAIL_LAYER_ALPHA: 0.65,
    TRAIL_BLEND: 'bounded',
    ROUTE_BLEND: 'bounded',
    ROUTE_ALPHA: 0.2,
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
  TRAIL_LAYER_ALPHA: 1,
  // A tram that shares kilometres of street with buses adds up to a white
  // corridor under 'bounded', which hides the line. On, the streetcar layer
  // lands last with normal alpha, so it keeps its colour over the buses.
  STREETCAR_ON_TOP: false,
  // Darkening at the top of the frame behind the title, 0 to 1. Off for the
  // sparse maps; a dense bus map needs it or the subtitle drowns.
  TITLE_SCRIM: 0,
  // Shorts keys (schema 4 networks, spec 2.10). Each default is today's
  // behaviour, and only initV4 reads them; they are declared here so that
  // applyRender type-checks them like every other knob.
  PRESET: '',
  HUD_LAYOUT: 'panel',
  THEME: '',
  VARIANT: '',
  COLOR_BY: '',
  BRAND_MIN_DE: 0.08,
  TIME_WARP_MODE: 'empty',
  TIME_WARP_GAMMA: 1,
  TIME_WARP_FLOOR: 0.15,
  TIME_WARP_SMOOTH_MIN: 60,
  LOOP: 'none',
  CLOCK_ROUND: 0,
  CARD: false,
  CARD_HOLD: 27,
  CARD_FADE_OUT: 18,
  CARD_FADE_IN: 30,
  CARD_SCRIM: 0.25,
  CARD_BAND: 0.85,
  CARD_CENTER_Y: 620,
  CARD_TITLE_MAX: 132,
  CARD_LINES: 0,
  PEAK_MARKER: false,
  MODE_CHIPS: false,
  OUTSIDE_DIM: 0,
  CITY_LINE_W: 2,
  CITY_LINE_ALPHA: 0.8,
  PANEL_ALPHA: 1,
  PANEL_SIDE: '',
  TITLE_SIZE: 64,
  PANEL_TOP: 0,
  FONT_SET: 'classic',
  FRAME_ZOOM: 1,
  FRAME_DX_KM: 0,
  FRAME_DY_KM: 0,
  BASE_ROADS_GAIN: 1,
  BASE_WATER_GAIN: 1,
  WEEKEND_BAND: false,
  // Camera (B18): a slow drone move over the map layers only, periodic over
  // the whole video so the loop has no seam. Off unless a preset turns it on.
  // CAMERA_ZOOM is the push-in at frame 0 over the fitted frame, CAMERA_DRIFT
  // the lateral travel as a share of the frame width; both are scaled down
  // together until the fastest point on screen moves at most CAMERA_MAX_SPEED
  // frame widths a second, then multiplied by CAMERA_AMP (the tuning knob).
  CAMERA: false,
  CAMERA_PATH: 'auto',
  CAMERA_ZOOM: 0.08,
  CAMERA_DRIFT: 0.03,
  CAMERA_AMP: 1,
  CAMERA_MAX_SPEED: 0.006,
  // 'cache' draws the base map once, finer than the push-in needs; 'vector'
  // redraws it every frame, which is slow and only the reference the cache is
  // measured against (tests/web/v4_camera.mjs).
  CAMERA_BASE: 'cache',
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
    CONFIG.BASEMAP_URL = `${city.basemapDir || city.dir}/basemap${ext}`;
    CONFIG.NETWORK_URL = `${city.dir}/network${ext}`;
    CONFIG.GZIP = city.gz;
  }
}
if (params.has('data')) CONFIG.NETWORK_URL = params.get('data');
if (params.has('basemap')) CONFIG.BASEMAP_URL = params.get('basemap');
if (params.has('gz')) CONFIG.GZIP = params.get('gz') !== '0';
// A knob given in the query is pinned: neither LARGE_FRAME nor meta.render
// may change it afterwards.
const pinned = new Set();
if (params.has('osm')) {
  CONFIG.OSM_ROUTES = params.get('osm') !== '0';
  pinned.add('OSM_ROUTES');
}
if (params.has('trailmode')) {
  CONFIG.TRAIL_MODE = params.get('trailmode');
  pinned.add('TRAIL_MODE');
}
// Numeric trail knobs for experiments: query name, CONFIG key, value when the
// query is not a number.
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
for (const [param, key, fallback] of KNOBS) {
  if (!params.has(param)) continue;
  const v = Number(params.get(param));
  CONFIG[key] = Number.isFinite(v) ? v : fallback;
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
let routeModes = null;     // mode index per route
let layerOrder = null;     // mode indices in 'bounded' compositing order
let onTopMode = -1;        // mode index landed with source-over, see STREETCAR_ON_TOP
// color_by "route": lines, trails and halos take the route's own colour. The
// layer passes run per distinct colour, not per route, so a city whose five
// hundred routes share sixty line colours costs sixty strokes per band.
let byRoute = false;
let colors = null;         // [{css, rgb, trips}] distinct route colours, in draw order
let tripColor = null;      // Uint16Array, colour index per trip
let colorShapes = null;    // [colour] shape indices for the dormant network
let colorPaths = null;     // [colour * TRAIL_BANDS + band] Path2D or null, rebuilt each frame
let colorLayer = null;     // 2D context the route-coloured trails compose in
// meta.groups: the HUD breakdown counts these (operators) instead of modes.
let groups = null;         // [{id, label, shown}]
let tripGroup = null;      // Uint8Array, group index per trip
let runningByGroup = null; // Int32Array, refilled each frame

// ---------------------------------------------------------------- helpers

function fetchJSON(url) {
  const path = url.split(/[?#]/)[0];
  // A .gz URL always inflates; a plain .json override on a gzip city does not.
  const gz = /\.gz$/i.test(path) || (CONFIG.GZIP && !/\.json$/i.test(path));
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

// Any CSS colour as [r, g, b]: a 2D context normalises what it is given to
// #rrggbb (or rgba() when translucent), which is easy to read back.
const colorProbe = document.createElement('canvas').getContext('2d');
function cssToRGB(css, fallback) {
  if (typeof css !== 'string' || !CSS.supports('color', css)) return fallback;
  colorProbe.fillStyle = css;
  const v = colorProbe.fillStyle;
  if (/^#[0-9a-f]{6}$/i.test(v)) return [1, 3, 5].map((i) => parseInt(v.slice(i, i + 2), 16));
  const m = /^rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(v);
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : fallback;
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

// meta.render tunes a city on top of the LARGE_FRAME profile; a query knob
// still wins. The frame, the canvas size and the data sources are settled
// elsewhere by then, so those keys are refused rather than half applied.
const RENDER_FIXED = new Set(['WIDTH', 'HEIGHT', 'KM_VERTICAL', 'CENTER_KM', 'HUD_SIDE',
  'BASEMAP_URL', 'NETWORK_URL', 'GZIP', 'LARGE_FRAME_KM']);
function applyRender(render) {
  if (!render || typeof render !== 'object') return;
  for (const [key, value] of Object.entries(render)) {
    const cur = CONFIG[key];
    const ok = !RENDER_FIXED.has(key) && Object.hasOwn(CONFIG, key) && (typeof cur === 'number'
      ? typeof value === 'number' && Number.isFinite(value)
      : (typeof cur === 'string' || typeof cur === 'boolean') && typeof value === typeof cur);
    if (!ok) {
      console.warn(`meta.render.${key} = ${JSON.stringify(value)} ignored`);
      continue;
    }
    if (!pinned.has(key)) CONFIG[key] = value;
  }
}

// meta.theme overrides HUD colours by COLORS name ("accent" for the count line
// and the sparkline).
function applyTheme(theme) {
  if (!theme || typeof theme !== 'object') return;
  for (const [key, value] of Object.entries(theme)) {
    if (typeof CONFIG.COLORS[key] === 'string' && typeof value === 'string' && CSS.supports('color', value)) {
      CONFIG.COLORS[key] = value;
    } else {
      console.warn(`meta.theme.${key} = ${JSON.stringify(value)} ignored`);
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
  // Covering the other layers only works if the streetcar layer lands after them.
  const top = CONFIG.STREETCAR_ON_TOP && index.has('streetcar') ? index.get('streetcar') : -1;
  onTopMode = top;
  layerOrder = modes.map((_, i) => i).filter((i) => i !== top);
  if (top >= 0) layerOrder.push(top);
  // A route whose mode is unknown (or absent, as in pre-v2 files) is the first mode.
  const routeMode = network.routes.map((r) => index.get(r.mode) || 0);
  routeModes = routeMode;
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

// color_by "route": one colour class per distinct routes[].color. A route
// without a usable colour takes its mode's line colour, and a shape no trip
// uses is drawn in the first mode's, as it would be without color_by.
function buildColors(network) {
  byRoute = meta.color_by === 'route';
  if (!byRoute) return;
  const toHex = (rgb) => rgb.map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');
  const index = new Map();
  const found = [];
  const classOf = (hex) => {
    if (!index.has(hex)) {
      index.set(hex, found.length);
      found.push({ hex, trips: 0, order: found.length });
    }
    return index.get(hex);
  };
  const routeClass = network.routes.map((r, i) => {
    let hex = typeof r.color === 'string' ? r.color.trim().replace(/^#/, '').toLowerCase() : '';
    if (/^[0-9a-f]{3}$/.test(hex)) hex = hex.replace(/./g, '$&$&');
    if (!/^[0-9a-f]{6}$/.test(hex)) hex = toHex(modes[routeModes[i] || 0].color);
    return classOf(hex);
  });
  const tripClass = new Uint16Array(trips.length);
  const used = new Uint8Array(shapes.length);
  for (let n = 0; n < trips.length; n++) {
    const k = routeClass[trips[n].r] || 0;
    tripClass[n] = k;
    found[k].trips++;
    used[trips[n].s] = 1;
  }
  let fallback = -1;
  for (let i = 0; i < shapes.length; i++) {
    if (!used[i]) { fallback = classOf(toHex(modes[0].color)); break; }
  }
  if (!found.length) classOf(toHex(modes[0].color));
  // Busiest colour last, so where lines share track the corridor shows the
  // line that runs most of its trains; ties keep file order.
  const order = found.slice().sort((a, b) => a.trips - b.trips || a.order - b.order);
  const rank = new Uint16Array(found.length);
  order.forEach((c, i) => { rank[c.order] = i; });
  colors = order.map((c) => {
    const rgb = [0, 2, 4].map((j) => parseInt(c.hex.slice(j, j + 2), 16));
    return { hex: c.hex, css: `#${c.hex}`, rgb, trips: c.trips };
  });
  tripColor = new Uint16Array(trips.length);
  for (let n = 0; n < trips.length; n++) tripColor[n] = rank[tripClass[n]];
  // A shape can carry routes of several colours; it is drawn once in each.
  const seen = new Set();
  colorShapes = colors.map(() => []);
  for (let n = 0; n < trips.length; n++) {
    const key = trips[n].s * colors.length + tripColor[n];
    if (seen.has(key)) continue;
    seen.add(key);
    colorShapes[tripColor[n]].push(trips[n].s);
  }
  if (fallback >= 0) {
    for (let i = 0; i < shapes.length; i++) if (!used[i]) colorShapes[rank[fallback]].push(i);
  }
  for (const list of colorShapes) list.sort((a, b) => a - b);
  if (CONFIG.TRAIL_MODE === 'sprite') {
    console.warn('color_by route draws ribbon trails; trailmode=sprite ignored');
    CONFIG.TRAIL_MODE = 'ribbon';
  }
}

// meta.groups (operators, in config order with the default last) replace the
// modes in the HUD breakdown. A route whose group is unknown counts in the
// default group; a group no trip belongs to is left off the line.
function buildGroups(network) {
  const list = Array.isArray(meta.groups) && meta.groups.length ? meta.groups : null;
  if (!list) return;
  groups = list.map((g) => ({ id: g.id, label: g.label || g.id, shown: false }));
  const index = new Map(groups.map((g, i) => [g.id, i]));
  const fallback = groups.length - 1;
  const routeGroup = network.routes.map((r) => (index.has(r.group) ? index.get(r.group) : fallback));
  tripGroup = new Uint8Array(trips.length);
  for (let n = 0; n < trips.length; n++) {
    const g = routeGroup[trips[n].r];
    tripGroup[n] = g === undefined ? fallback : g;
    groups[tripGroup[n]].shown = true;
  }
  if (!groups.some((g) => g.shown)) for (const g of groups) g.shown = true;
  runningByGroup = new Int32Array(groups.length);
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
  if (basemap.water.holes) {
    g.fillStyle = C.bg;
    g.beginPath();
    for (const ring of basemap.water.holes) {
      if (tracePolyline(g, ring)) g.closePath();
    }
    g.fill();
  }

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
  if (byRoute) {
    // Each line in its own colour: every colour's shapes are unioned at full
    // alpha into one layer, busiest colour on top, and the layer lands once at
    // ROUTE_ALPHA. Where lines share track the top colour shows as it is
    // instead of the stack brightening, and twenty shapes are as dim as one.
    g.globalCompositeOperation = 'source-over';
    const ml = document.createElement('canvas');
    ml.width = W;
    ml.height = H;
    const lg = ml.getContext('2d');
    lg.lineCap = 'round';
    lg.lineJoin = 'round';
    lg.lineWidth = CONFIG.ROUTE_WIDTH;
    for (let k = 0; k < colors.length; k++) {
      lg.strokeStyle = colors[k].css;
      lg.beginPath();
      for (const i of colorShapes[k]) tracePolyline(lg, network.shapes[i].xy);
      lg.stroke();
    }
    g.globalAlpha = CONFIG.ROUTE_ALPHA;
    g.drawImage(ml, 0, 0);
    g.globalAlpha = 1;
    return c;
  }
  if (CONFIG.ROUTE_BLEND === 'bounded') {
    // Each mode's shapes are unioned at full alpha in their own layer, then
    // the layer lands once at ROUTE_ALPHA: thirty overlapping Bloor shapes
    // read exactly as dim as one lone suburban line. Later modes on top so a
    // subway line under a bus corridor keeps its own colour.
    g.globalCompositeOperation = 'source-over';
    for (let m = 0; m < modes.length; m++) {
      const ml = modeLayer(m);
      ml.globalCompositeOperation = 'source-over';
      ml.globalAlpha = 1;
      ml.clearRect(0, 0, W, H);
      ml.lineCap = 'round';
      ml.lineJoin = 'round';
      ml.lineWidth = CONFIG.ROUTE_WIDTH;
      ml.strokeStyle = rgba(modes[m].color, 1);
      ml.beginPath();
      for (let i = 0; i < network.shapes.length; i++) {
        if (shapeMode[i] === m) tracePolyline(ml, network.shapes[i].xy);
      }
      ml.stroke();
      g.globalAlpha = CONFIG.ROUTE_ALPHA;
      g.drawImage(ml.canvas, 0, 0);
      g.globalAlpha = 1;
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
  // with trains and streetcars reads as two kinds of vehicle. With color_by
  // route it is the line's colour, one sprite per colour class.
  const busSprite = (halo) => {
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
  };
  busSprites = byRoute
    ? colors.map((c) => busSprite(c.rgb))
    : modes.map((mode) => busSprite(modes.length > 1 ? mode.trail : [255, 255, 255]));
  busHalf = busSprites[0].width / 2;

  if (byRoute) {
    // Route-coloured trails compose with each other in this layer before it
    // reaches the frame, at TRAIL_SCALE resolution.
    const c = document.createElement('canvas');
    c.width = Math.round(W * ts);
    c.height = Math.round(H * ts);
    colorLayer = c.getContext('2d');
    colorPaths = new Array(colors.length * CONFIG.TRAIL_BANDS).fill(null);
  } else if (ts !== 1) {
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
  // The breakdown line counts groups when the city has them, else modes when
  // there are several; groups win because a trains video is one mode.
  const breakdown = groups ? 'group' : modes.length > 1 ? 'mode' : null;
  const multi = breakdown !== null;
  // The breakdown line sits under the count and pushes the chart down.
  const shift = multi ? 30 : 0;
  const lines = Array.isArray(meta.attribution) && meta.attribution.length ? meta.attribution : CONFIG.ATTRIBUTION;
  // A city can push the whole block down (meta.frame.hud_top) so it sits on
  // water rather than on its downtown.
  const top = (meta.frame && Number(meta.frame.hud_top)) || 1120;
  const dy = top - 1120;
  const attrY = 1562 + shift + dy;
  const lastBaseline = attrY + (lines.length - 1) * 23;
  // The blur thins the panel towards its bottom edge, so over a dense map the
  // last line needs meta.frame.hud_pad_bottom more panel under it to stay legible.
  const padBottom = (meta.frame && Number(meta.frame.hud_pad_bottom)) || 0;
  hud = {
    dx,
    multi,
    breakdown,
    lines,
    panelX: 40 + dx,
    panelY: top,
    panelW: 600,
    // 12 px under the last attribution baseline: with the three Tsukuba lines
    // that is the 1620 the frames were measured with.
    panelH: lastBaseline + 12 + padBottom - top,
    textX: 70 + dx,
    clockY: 1275 + dy,
    countY: 1342 + dy,
    breakdownY: 1374 + dy,
    // Tabular digits on both count lines when the number changes every frame.
    breakdownFont: multi ? '500 24px MontserratTnum' : '500 24px Montserrat',
    countFont: multi ? '600 32px MontserratTnum' : '600 32px Montserrat',
    sparkX0: 70 + dx,
    sparkX1: 610 + dx,
    sparkY0: 1380 + shift + dy,
    sparkY1: 1500 + shift + dy,
    axisY: 1530 + shift + dy,
    attrY,
  };
  const maxTextW = 540;
  if (multi) {
    // Size the breakdown once for the widest line the day can produce (each
    // mode or group at its own peak), so the text never jitters between frames.
    const peakOf = (h) => {
      if (!Array.isArray(h) || !h.length) return (meta.peak && meta.peak.count) || 0;
      let p = 0;
      for (const v of h) if (v > p) p = v;
      return p;
    };
    let widest;
    if (breakdown === 'group') {
      const byGroup = meta.hist_by_group || {};
      widest = groups.filter((g) => g.shown).map((g) => `${withCommas(peakOf(byGroup[g.id]))} ${g.label}`).join(' · ');
    } else {
      const byMode = meta.hist_by_mode || {};
      widest = modes.map((m) => {
        const peak = peakOf(byMode[m.id]);
        return `${withCommas(peak)} ${peak === 1 ? m.singular : m.label}`;
      }).join(' · ');
    }
    for (let size = 24; size >= 14; size--) {
      hud.breakdownFont = `500 ${size}px MontserratTnum`;
      ctx.font = hud.breakdownFont;
      if (ctx.measureText(widest).width <= maxTextW) break;
    }
    if (hud.breakdownFont !== '500 24px MontserratTnum') console.warn(`breakdown line "${widest}" needs ${hud.breakdownFont} to fit ${maxTextW} px`);
  }
  ctx.font = '400 18px Inter';
  for (const line of lines) {
    const w = ctx.measureText(line).width;
    if (w > maxTextW) console.warn(`attribution line "${line}" is ${Math.round(w)} px, wider than the ${maxTextW} px panel text width`);
  }
  // The subtitle keeps 38 px unless it would run off the frame.
  hud.subtitleFont = '400 38px Montserrat';
  const subtitle = meta.subtitle || '';
  for (let size = 38; size >= 24; size--) {
    hud.subtitleFont = `400 ${size}px Montserrat`;
    ctx.font = hud.subtitleFont;
    if (ctx.measureText(subtitle).width <= W - 80) break;
  }
  if (hud.subtitleFont !== '400 38px Montserrat') console.warn(`subtitle "${subtitle}" needs ${hud.subtitleFont} to fit ${W - 80} px`);
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
  if (network.meta && network.meta.schema === 4) return initV4(basemap, network);
  meta = network.meta;
  shapes = network.shapes;
  trips = network.trips;
  hist = network.hist;
  setFrame(meta.frame);
  applyRender(meta.render);
  applyTheme(meta.theme);
  totalFrames = CONFIG.HOLD_START + CONFIG.DURATION_FRAMES + CONFIG.HOLD_END;
  buildWarp();
  buildModes(network);
  buildColors(network);
  buildGroups(network);

  // Canvas text only triggers a font load on first use, so request every
  // face explicitly before waiting on document.fonts.ready.
  const faces = [
    '600 58px Montserrat', '400 38px Montserrat', '800 108px MontserratTnum',
    '600 32px Montserrat', '500 24px Montserrat', '600 32px MontserratTnum', '500 24px MontserratTnum', '500 20px Montserrat', '400 18px Inter',
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

// color_by route: the same, into the shared path for that (colour, band),
// made on first use because most colours are idle in most bands.
function addRibbonColor(trip, k, first, last) {
  const rs = ribbonShapes[trip.s];
  if (rs.cum.length < 2) return;
  const base = k * CONFIG.TRAIL_BANDS;
  for (let b = first; b < last; b++) {
    const dA = sampleD[b + 1], dB = sampleD[b];
    if (dB - dA < 1e-4) continue;
    let path = colorPaths[base + b];
    if (!path) path = colorPaths[base + b] = new Path2D();
    appendStretch(path, rs, dA, dB);
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

// Route-coloured trails with normal alpha: the oldest band goes down first so
// the freshest stretch of any line lies on top, and within a band every
// shoulder goes down before any core so a neighbour's soft edge never tints a
// line's own colour. Same-colour strokes still build up toward that colour,
// never past it, so line colours stay true where lines share track.
function strokeColorRibbons(layer, ts) {
  const bands = CONFIG.TRAIL_BANDS;
  const nc = colors.length;
  layer.lineCap = 'butt';
  layer.lineJoin = 'round';
  if (ts !== 1) layer.setTransform(ts, 0, 0, ts, 0, 0);
  for (let b = bands - 1; b >= 0; b--) {
    const a = CONFIG.TRAIL_ALPHA * (1 - (b + 0.5) / bands);
    if (CONFIG.TRAIL_SHOULDER_ALPHA > 0 && b < CONFIG.TRAIL_SHOULDER_BANDS) {
      layer.globalAlpha = a * CONFIG.TRAIL_SHOULDER_ALPHA;
      layer.lineWidth = CONFIG.TRAIL_SHOULDER_W;
      for (let k = 0; k < nc; k++) {
        const path = colorPaths[k * bands + b];
        if (!path) continue;
        layer.strokeStyle = colors[k].css;
        layer.stroke(path);
      }
    }
    layer.globalAlpha = a;
    layer.lineWidth = CONFIG.TRAIL_CORE_W;
    for (let k = 0; k < nc; k++) {
      const path = colorPaths[k * bands + b];
      if (!path) continue;
      layer.strokeStyle = colors[k].css;
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
  if (titleScrim) ctx.drawImage(titleScrim, 0, 0);

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
  ctx.font = hud.subtitleFont;
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
  ctx.font = hud.countFont;
  if (hud.multi) {
    // One mode split by operator still counts in that mode's own word.
    const one = modes.length === 1 ? modes[0] : null;
    const noun = one ? (running === 1 ? one.singular : one.label) : (running === 1 ? 'vehicle' : 'vehicles');
    ctx.fillText(`${withCommas(running)} ${noun} running`, tx, hud.countY);
    ctx.fillStyle = C.breakdown;
    ctx.font = hud.breakdownFont;
    const parts = [];
    if (hud.breakdown === 'group') {
      for (let k = 0; k < groups.length; k++) {
        if (groups[k].shown) parts.push(`${withCommas(runningByGroup[k])} ${groups[k].label}`);
      }
    } else {
      for (let m = 0; m < modes.length; m++) {
        const n = runningByMode[m];
        parts.push(`${withCommas(n)} ${n === 1 ? modes[m].singular : modes[m].label}`);
      }
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
let titleScrim = null;
let sparkFill = null;
function buildHudStatics() {
  if (CONFIG.TITLE_SCRIM > 0) {
    titleScrim = document.createElement('canvas');
    titleScrim.width = W;
    titleScrim.height = 320;
    const sg = titleScrim.getContext('2d');
    const grad = sg.createLinearGradient(0, 0, 0, 320);
    grad.addColorStop(0, `rgba(7,8,12,${CONFIG.TITLE_SCRIM})`);
    grad.addColorStop(0.62, `rgba(7,8,12,${CONFIG.TITLE_SCRIM * 0.85})`);
    grad.addColorStop(1, 'rgba(7,8,12,0)');
    sg.fillStyle = grad;
    sg.fillRect(0, 0, W, 320);
  }
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
  const accent = cssToRGB(CONFIG.COLORS.accent, [255, 224, 102]);
  const grad = ctx.createLinearGradient(0, spark.y0, 0, spark.y1);
  grad.addColorStop(0, rgba(accent, 0.5));
  grad.addColorStop(1, rgba(accent, 0.03));
  sparkFill = grad;
}

function renderAt(T) {
  if (!baseCanvas) throw new Error('busmap not ready');
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  ctx.drawImage(baseCanvas, 0, 0);

  const bounded = !byRoute && CONFIG.TRAIL_BLEND === 'bounded' && CONFIG.TRAIL_MODE !== 'sprite';
  const ts = bounded ? 1 : CONFIG.TRAIL_SCALE;
  const layer = ts === 1 || byRoute ? ctx : trailCtx;
  if (ts !== 1 && !byRoute) {
    layer.globalCompositeOperation = 'source-over';
    layer.clearRect(0, 0, trailCanvas.width, trailCanvas.height);
  }
  layer.globalCompositeOperation = 'lighter';

  const buses = [];
  const horizon = T - trailWindow;
  const sprite = CONFIG.TRAIL_MODE === 'sprite';
  const step = sprite ? CONFIG.TRAIL_STEP_S : trailWindow / CONFIG.TRAIL_BANDS;
  const count = sprite ? trailSteps + 1 : CONFIG.TRAIL_BANDS + 1;
  if (byRoute) {
    colorPaths.fill(null);
  } else if (!sprite) {
    for (let m = 0; m < modes.length; m++) {
      for (let b = 0; b < CONFIG.TRAIL_BANDS; b++) bandPaths[m][b] = new Path2D();
    }
  }
  runningByMode.fill(0);
  if (runningByGroup) runningByGroup.fill(0);
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
      if (runningByGroup) runningByGroup[tripGroup[n]]++;
      buses.push(sampleX[0] / ts, sampleY[0] / ts, byRoute ? tripColor[n] : m);
    }
    if (byRoute) addRibbonColor(trip, tripColor[n], first, last);
    else if (sprite) stampTrailSprites(layer, trailSprites[m], first, last);
    else addRibbon(trip, bandPaths[m], first, last);
  }
  if (byRoute) {
    // All lines compose in one layer, which lands with normal alpha too, so
    // no corridor turns white however many lines share it.
    colorLayer.globalCompositeOperation = 'source-over';
    colorLayer.globalAlpha = 1;
    colorLayer.clearRect(0, 0, colorLayer.canvas.width, colorLayer.canvas.height);
    strokeColorRibbons(colorLayer, ts);
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = CONFIG.TRAIL_LAYER_ALPHA;
    ctx.drawImage(colorLayer.canvas, 0, 0, W, H);
    ctx.globalAlpha = 1;
  } else if (!sprite && CONFIG.TRAIL_BLEND === 'bounded') {
    // Each mode composes with itself under normal alpha in its own layer;
    // the layers then add onto the frame, except an on-top streetcar layer,
    // which covers what is under it so shared streets keep the tram colour.
    for (const m of layerOrder) {
      const ml = modeLayer(m);
      ml.globalCompositeOperation = 'source-over';
      ml.clearRect(0, 0, W, H);
      strokeRibbons(ml, 1, m);
      ctx.globalCompositeOperation = m === onTopMode ? 'source-over' : 'lighter';
      // The layer saturates to the full trail colour wherever four vehicles
      // overlap; the ceiling keeps that below white once halos land on it.
      ctx.globalAlpha = CONFIG.TRAIL_LAYER_ALPHA;
      ctx.drawImage(ml.canvas, 0, 0);
      ctx.globalAlpha = 1;
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

// ------------------------------------------------------------- v4: Shorts
//
// A network with meta.schema 4 (spec 2.9) leaves init() on the line after the
// fetch and comes here; nothing below runs for the legacy configs. A legacy
// function is called only where it already does exactly what v4 needs, and
// everything else has a V4 copy, so the legacy bodies (and with them the
// legacy frames) stay byte-identical; tests/legacy/bodies.py checks that.

let isV4 = false;
let variantV = null;        // meta.variants[CONFIG.VARIANT]
let periodS = 86400;        // meta.timeline.period, seconds
let histN = 1440;           // meta.hist_period, minutes
let themeEnv = null;        // trail and line envelope of the active theme (B4)
let brandMap = null;        // B6 result, busmap.brandMap
let brandHexQuery = null;   // ?brandhex=, id -> rrggbb
let groupColor = null;      // group id -> chip dot colour
let chipMerged = new Set(); // groups the chips line folds into "other" (B9), drawn foreign (B6)
// Occurrences (B7): one per trip, weekday and period shift that can be on
// screen inside the window, sorted by start time.
let occN = 0;
let occTrip = null;
let occOff = null;
let occT0 = null;
let occT1 = null;
let maxDur = 0;
let sparkSmooth = null;     // hist smoothed by SPARK_SMOOTH_MIN, circular
let shorts = null;          // Shorts HUD geometry and fitted fonts (B9)
let boundaryMask = null;    // decoded meta.boundary.mask (B11)
let boundaryOverlay = null; // dimming outside the boundary plus the city line, one canvas
let cardLayout = null;      // B10 card geometry and text, or null
let cardOn = true;
let hudMode = 'full';       // 'full', 'notext' or 'none' (setHud)
let showSafe = false;
let loopSnapshot = null;    // frame 0, drawn over the end of an xfade loop
let lastBoxes = [];         // hudBoxes() of the last drawn frame
let vehBuf = new Float32Array(3 * 4096);
let vehN = 0;
let shortsStatics = null;   // title scrim, panel backdrop, spark fill, safe zone overlay
let cam = null;             // B18 camera: path, amplitudes, base headroom and boundary paths, or null when off
let camPin = null;          // setCamera(u): the phase stills are drawn at instead of their own

// YouTube Shorts safe zone in frame pixels: nothing on screen leaves it (B9).
const SAFE = { x0: 60, y0: 240, x1: 880, y1: 1500 };
// The panel's text column (B9); the chips that decide the colours are fitted
// into it before the layout exists.
const SHORTS_TEXT_W = 504;
// Smallest size each HUD and card text may take (B9 asserts).
const MIN_SIZE = {
  title: 48, subtitle: 32, weekday: 64, clock: 40, count: 36, count2: 36, chips: 24, peak: 26, axis: 26,
  credit: 22, credit2: 22, card_title: 72, card_title2: 72, card_line0: 40, card_line0b: 40, card_line1: 30,
  card_line1b: 30,
};
const DAY_NAMES = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY'];
const DAY_LETTERS = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];
// Colours of modes without a brand, before the theme envelope moves them (B4).
const MODE_SOURCES = { bus: '4864de', streetcar: 'cd3746', rail: 'cda53c', ferry: '3cbeb4' };
// Tokens a theme adds to CONFIG.COLORS, and their values without a theme.
const V4_COLORS = { cityLine: '#8c8f99', floor: '#3a3b43', dotCore: '#ffffff', foreign: '#8a8d96' };
// The lake envelope stands in when a v4 file is drawn with THEME ''.
const DEFAULT_ENV = { trailL: 0.78, trailCmin: 0.07, trailCmax: 0.15, lineL: 0.58, lineCmax: 0.13, minContrast: 3 };
const V4_ENUMS = {
  HUD_LAYOUT: ['panel', 'shorts'],
  COLOR_BY: ['', 'brand'],
  TIME_WARP_MODE: ['empty', 'activity', 'activity-daily', 'linear'],
  LOOP: ['none', 'wrap', 'xfade'],
  PANEL_SIDE: ['', 'left', 'right'],
  FONT_SET: ['classic', 'extended'],
  CAMERA_PATH: ['auto', 'pull-out-east', 'pull-out-north', 'pull-out-west', 'pull-out-south', 'drift-orbit', 'drift-sway'],
  CAMERA_BASE: ['cache', 'vector'],
};
// Query names of the Shorts knobs (2.10): [query, CONFIG key, kind]. A name
// kind is checked as a file name; 'bool' reads 0 or false as off.
const V4_KNOBS = [
  ['preset', 'PRESET', 'name'], ['layout', 'HUD_LAYOUT', 'enum'], ['theme', 'THEME', 'name'],
  ['variant', 'VARIANT', 'name'], ['colorby', 'COLOR_BY', 'enum'], ['warp', 'TIME_WARP_MODE', 'enum'],
  ['warpgamma', 'TIME_WARP_GAMMA', 'num'], ['warpfloor', 'TIME_WARP_FLOOR', 'num'], ['loop', 'LOOP', 'enum'],
  ['clockround', 'CLOCK_ROUND', 'num'], ['card', 'CARD', 'bool'], ['cardscrim', 'CARD_SCRIM', 'num'],
  ['cardband', 'CARD_BAND', 'num'], ['cardy', 'CARD_CENTER_Y', 'num'], ['cardsize', 'CARD_TITLE_MAX', 'num'],
  ['cardline', 'CARD_LINES', 'num'], ['peak', 'PEAK_MARKER', 'bool'], ['chips', 'MODE_CHIPS', 'bool'],
  ['outside', 'OUTSIDE_DIM', 'num'], ['panelalpha', 'PANEL_ALPHA', 'num'], ['panelside', 'PANEL_SIDE', 'enum'],
  ['fonts', 'FONT_SET', 'enum'], ['zoom', 'FRAME_ZOOM', 'num'], ['cx', 'FRAME_DX_KM', 'num'],
  ['cy', 'FRAME_DY_KM', 'num'], ['roads', 'BASE_ROADS_GAIN', 'num'], ['water', 'BASE_WATER_GAIN', 'num'],
  ['dotcore', 'BUS_CORE_R', 'num'], ['halor', 'BUS_HALO_R', 'num'], ['haloalpha', 'BUS_HALO_ALPHA', 'num'],
  ['layeralpha', 'TRAIL_LAYER_ALPHA', 'num'], ['smooth', 'SPARK_SMOOTH_MIN', 'num'],
  ['camera', 'CAMERA', 'bool'], ['campath', 'CAMERA_PATH', 'enum'], ['camzoom', 'CAMERA_ZOOM', 'num'],
  ['camdrift', 'CAMERA_DRIFT', 'num'], ['camamp', 'CAMERA_AMP', 'num'], ['camspeed', 'CAMERA_MAX_SPEED', 'num'],
  ['cambase', 'CAMERA_BASE', 'enum'],
];
// Brand distinctness ladder (B6): [name, lightness shift, hue rotation in
// degrees]. Lightness comes before hue so a known colour keeps its hue, and
// no step turns more than 30 degrees.
const BRAND_LADDER = [
  ['source', 0, 0], ['alt', 0, 0], ['dL-0.12', -0.12, 0], ['dL+0.10', 0.10, 0],
  ['rot+15', 0, 15], ['rot-15', 0, -15], ['rot+30', 0, 30], ['rot-30', 0, -30],
  ['dL-0.12 rot+15', -0.12, 15], ['dL-0.12 rot-15', -0.12, -15], ['dL-0.12 rot+30', -0.12, 30],
  ['dL-0.12 rot-30', -0.12, -30],
];

const mod = (a, n) => ((a % n) + n) % n;
const clamp01 = (x) => Math.min(1, Math.max(0, x));
const smoothstep = (x) => x * x * (3 - 2 * x);

// Python's round(), which A's builders use for every count in the file: an
// exact half goes to the even neighbour, so 2.5 vehicles read 2 on both sides.
function roundHalfEven(x) {
  const r = Math.round(x);
  return Math.abs(x - Math.trunc(x)) === 0.5 && r % 2 !== 0 ? r - 1 : r;
}

// Query knobs of 2.10, plus hud, safe, brandhex and render; all pinned, so
// nothing later in the precedence chain overrides them (B2 step 1).
function parseV4Query() {
  for (const [param, key, kind] of V4_KNOBS) {
    if (!params.has(param)) continue;
    const raw = params.get(param);
    let v = raw;
    if (kind === 'num') {
      v = Number(raw);
      if (raw.trim() === '' || !Number.isFinite(v)) {
        console.warn(`?${param}=${raw} ignored: not a number`);
        continue;
      }
    } else if (kind === 'bool') {
      v = raw !== '0' && raw !== 'false';
    } else if (kind === 'name') {
      if (!/^[A-Za-z0-9_-]*$/.test(raw)) throw new Error(`?${param}=${raw}: not a plain name`);
    } else if (!V4_ENUMS[key].includes(raw)) {
      console.warn(`?${param}=${raw} ignored; one of ${V4_ENUMS[key].map((e) => JSON.stringify(e)).join(', ')}`);
      continue;
    }
    CONFIG[key] = v;
    pinned.add(key);
  }
  if (HUD_OVERRIDE === 'none' || HUD_OVERRIDE === 'notext') hudMode = HUD_OVERRIDE;
  showSafe = params.get('safe') === '1';
  brandHexQuery = new Map();
  if (params.has('brandhex')) {
    for (const part of params.get('brandhex').split(';')) {
      if (!part.trim()) continue;
      // Brand ids carry colons (brampton:zum, ttc:line:d5c82b): the hex is after the last one.
      const at = part.lastIndexOf(':');
      const id = part.slice(0, at).trim();
      const hex = part.slice(at + 1).trim().replace(/^#/, '').toLowerCase();
      if (at <= 0 || !/^[0-9a-f]{6}$/.test(hex)) throw new Error(`?brandhex: bad entry "${part}", want id:rrggbb`);
      brandHexQuery.set(id, hex);
    }
  }
  if (!params.has('render')) return null;
  let render;
  try {
    render = JSON.parse(params.get('render'));
  } catch (e) {
    throw new Error(`?render is not JSON: ${e.message}`);
  }
  if (!render || typeof render !== 'object' || Array.isArray(render)) throw new Error('?render must be a JSON object of CONFIG keys');
  return render;
}

// A batch theme (web/themes.json): every COLORS token it names, and its
// envelope for brand and mode colours.
function applyThemeTokens(theme) {
  const C = CONFIG.COLORS;
  for (const [key, value] of Object.entries(theme.colors || {})) {
    if (typeof value === 'string' && CSS.supports('color', value)) C[key] = value;
    else console.warn(`theme token ${key} = ${JSON.stringify(value)} ignored`);
  }
  if (theme.env && typeof theme.env === 'object') themeEnv = { ...themeEnv, ...theme.env };
}

// Enum keys arrive through applyRender as any string; an unknown one would
// silently draw something else, so it falls back to its default.
function checkV4Config() {
  const defaults = { HUD_LAYOUT: 'panel', COLOR_BY: '', TIME_WARP_MODE: 'empty', LOOP: 'none', PANEL_SIDE: '', FONT_SET: 'classic',
    CAMERA_PATH: 'auto', CAMERA_BASE: 'cache' };
  for (const [key, allowed] of Object.entries(V4_ENUMS)) {
    if (!allowed.includes(CONFIG[key])) {
      console.warn(`${key} = ${JSON.stringify(CONFIG[key])} is not one of ${allowed.join(', ')}; using ${JSON.stringify(defaults[key])}`);
      CONFIG[key] = defaults[key];
    }
  }
}

// FRAME_ZOOM and the km shifts land after every render block (B2 step 9b).
// setFrame already chose the LARGE_FRAME profile on the unzoomed frame, so a
// zoom arm never switches the trail profile.
function finishFrame() {
  const z = CONFIG.FRAME_ZOOM, dx = CONFIG.FRAME_DX_KM, dy = CONFIG.FRAME_DY_KM;
  if (z === 1 && dx === 0 && dy === 0) return;
  if (!(z > 0)) throw new Error(`FRAME_ZOOM must be positive, got ${z}`);
  CONFIG.KM_VERTICAL /= z;
  CONFIG.CENTER_KM = [CONFIG.CENTER_KM[0] + dx, CONFIG.CENTER_KM[1] + dy];
  SCALE = H / CONFIG.KM_VERTICAL;
  OX = W / 2 - CONFIG.CENTER_KM[0] * SCALE;
  OY = H / 2 + CONFIG.CENTER_KM[1] * SCALE;
}

// ------------------------------------------------------------- v4: camera (B18)
//
// The map layers (base map, dormant network, trails, dots, outside dimming and
// city line) move under a camera that is a pure function of the loop phase
// u = i / N, so frame N is frame 0 and the loop needs no seam of its own. The
// HUD, the card and the scrims are drawn after it at identity. At phase u the
// camera looks at the pivot plus a drift D(u) with zoom z(u):
//
//   z(u) = 1 + Z (1 + cos 2 pi u) / 2         push-in at frame 0, fitted frame at u = 1/2
//   screen = pivot + z (base - pivot - R D(u))
//
// so both ease in and out (zero zoom speed at the turning points) while the
// sideways part keeps moving, and nothing is ever at constant speed.

// 'auto' picks from these by the city id; the order is part of that contract
// (scripts/make.py CAMERA_PATHS).
const CAMERA_NAMES = V4_ENUMS.CAMERA_PATH.slice(1);
// The fit box centre: D3.2 puts the boundary bbox centre there and A8.6 the
// rush vehicles, so the push-in keeps the city core where the fitted frame
// has it, below the card.
const CAMERA_PIVOT = [460, 845];
// The pull-outs bow sideways by this share of the drift, so the way out and
// the way back are the two sides of a thin ellipse rather than one line.
const CAMERA_BOW = 0.4;
// A path's share of CAMERA_ZOOM and CAMERA_DRIFT, [1, 1] when not listed. With
// one share for all, every upload made the same push-in and pull-out in step
// and only the drift's direction told them apart; the drifts push in half as
// far and travel half as far again, so they read as pans and the pull-outs as
// pull-outs. The orbit's core then starts 0.4 x 1.5 x 3% of the width off the
// pivot, under the 2% that frame 0 allows.
const CAMERA_MIX = { 'drift-orbit': [0.5, 1.5], 'drift-sway': [0.5, 1.5] };
// Phases sampled for the speed cap, the city line's cap and the base
// headroom: 1/720 of a 25 s rush is about one frame.
const CAMERA_STEPS = 720;
// How far past the looser of the safe zone and its fitted position the camera
// may carry the city line, in px. D3.2's fit box itself reaches 10 px past the
// safe zone's left edge, and with no slack a city that fills the fit box width
// could not move at all: any push-in about the pivot widens it.
const CAMERA_EDGE_SLACK = 10;
// The cached base is drawn this many times finer than the push-in needs and
// scaled down with mipmaps each frame. Against a fresh vector render per frame
// (Toronto's base) that keeps about 91% of the edge energy while thin roads
// flicker a third as much as under the vector render's own anti-aliasing at
// the fastest phase; a 1x cache with bilinear filtering kept 83 to 90% and
// flickered more.
const CAMERA_SUPERSAMPLE = 2;

// FNV-1a of the id, the same function as make.py camera_path(), so every
// variant of a city takes the same path. make.py starts from it to spread a
// batch (camera_paths) and always passes the path it picked.
function cameraPathFor(id) {
  let h = 2166136261;
  const s = String(id || '');
  for (let k = 0; k < s.length; k++) h = Math.imul(h ^ s.charCodeAt(k), 16777619) >>> 0;
  return CAMERA_NAMES[h % CAMERA_NAMES.length];
}

// D(u) in units of the drift at angle th = 2 pi u, x east and y south. Every
// path but the orbit starts on the pivot, so frame 0 is a plain push-in.
function cameraUnit(path, th) {
  const c = Math.cos(th), s = Math.sin(th);
  const out = (1 - c) / 2, side = CAMERA_BOW * s / 2;
  switch (path) {
    case 'pull-out-east': return [out, side];
    case 'pull-out-west': return [-out, -side];
    case 'pull-out-north': return [side, -out];
    case 'pull-out-south': return [-side, out];
    // An ellipse round the core, clockwise on screen.
    case 'drift-orbit': return [s / 2, -0.4 * c];
    // A figure of eight across the core.
    default: return [s / 2, Math.sin(2 * th) / 4];
  }
}

// The camera at phase u as screen = z * base + (e, f), for zoom amplitude Z
// and drift R in frame px. The phase is reduced first so u = 1 is exactly u = 0.
// start marks frame 0's phase, where the cached base lands on the frame's
// pixel grid; the zoom cannot mark it, since with no zoom every phase has z 1.
function cameraMatrix(path, Z, R, u) {
  const th = 2 * Math.PI * (u - Math.floor(u));
  const z = 1 + Z * (1 + Math.cos(th)) / 2;
  const d = cameraUnit(path, th);
  const [cx, cy] = CAMERA_PIVOT;
  return { z, e: cx - z * (cx + R * d[0]), f: cy - z * (cy + R * d[1]), start: th === 0 };
}

// The fastest on-screen motion over the loop in px per unit of phase. The
// step of a point is affine in the point, so the frame corners bound it.
function cameraPeak(path, Z, R) {
  let peak = 0;
  let prev = cameraMatrix(path, Z, R, 0);
  for (let k = 1; k <= CAMERA_STEPS; k++) {
    const m = cameraMatrix(path, Z, R, k / CAMERA_STEPS);
    for (const [qx, qy] of [[0, 0], [W, 0], [0, H], [W, H]]) {
      const bx = (qx - prev.e) / prev.z, by = (qy - prev.f) / prev.z;
      const d = Math.hypot(m.z * bx + m.e - qx, m.z * by + m.f - qy);
      if (d > peak) peak = d;
    }
    prev = m;
  }
  return peak * CAMERA_STEPS;
}

function cameraNum(key, lo, hi) {
  const v = CONFIG[key];
  if (v >= lo && v <= hi) return v;
  const c = Math.min(hi, Math.max(lo, Number.isFinite(v) ? v : lo));
  console.warn(`${key} = ${v} is outside ${lo}..${hi}; using ${c}`);
  CONFIG[key] = c;
  return c;
}

// The city line's bbox in the fitted frame's px, or null without a boundary.
function boundaryBoxPx() {
  const b = meta.boundary;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const ring of (b && b.rings) || []) {
    if (ring.length < 6) continue;
    for (let i = 0; i < ring.length; i += 2) {
      const x = OX + ring[i] * SCALE, y = OY - ring[i + 1] * SCALE;
      if (x < x0) x0 = x;
      if (x > x1) x1 = x;
      if (y < y0) y0 = y;
      if (y > y1) y1 = y;
    }
  }
  return x0 <= x1 ? [x0, y0, x1, y1] : null;
}

// Where the camera may take the city line's bbox: each side at the looser of
// the safe zone and the fitted bbox, plus the slack, and never off the frame.
// Null when the fitted frame already crops the line (a rush close-up), since
// then there is no whole outline to keep in view.
function cameraKeepRect(box) {
  if (!box || box[0] < 0 || box[1] < 0 || box[2] > W || box[3] > H) return null;
  const t = CAMERA_EDGE_SLACK;
  return [Math.max(0, Math.min(SAFE.x0, box[0]) - t), Math.max(0, Math.min(SAFE.y0, box[1]) - t),
    Math.min(W, Math.max(SAFE.x1, box[2]) + t), Math.min(H, Math.max(SAFE.y1, box[3]) + t)];
}

// Whether the bbox stays inside the keep rect at every sampled phase. The
// camera is a scale and a shift, so the bbox's own edges are the line's.
function cameraKeeps(path, Z, R, box, keep) {
  for (let k = 0; k < CAMERA_STEPS; k++) {
    const m = cameraMatrix(path, Z, R, k / CAMERA_STEPS);
    if (m.z * box[0] + m.e < keep[0] || m.z * box[1] + m.f < keep[1]
      || m.z * box[2] + m.e > keep[2] || m.z * box[3] + m.f > keep[3]) return false;
  }
  return true;
}

// Resolves the path and the amplitudes, then the base headroom: the union over
// the loop of the base rectangle that is on screen, so the cached base map can
// be drawn once at the push-in scale and only ever scaled down. Needs the frame
// (finishFrame), meta.boundary and totalFrames.
function buildCameraV4() {
  cam = null;
  if (!CONFIG.CAMERA) return;
  if (CONFIG.CAMERA_PATH === 'auto') CONFIG.CAMERA_PATH = cameraPathFor(meta.id);
  const path = CONFIG.CAMERA_PATH;
  const mix = CAMERA_MIX[path] || [1, 1];
  const Z0 = cameraNum('CAMERA_ZOOM', 0, 0.5) * mix[0];
  const R0 = cameraNum('CAMERA_DRIFT', 0, 0.2) * W * mix[1];
  const amp = cameraNum('CAMERA_AMP', 0, 3);
  const vmax = cameraNum('CAMERA_MAX_SPEED', 1e-4, 0.05) * W * totalFrames / CONFIG.FPS;
  // A short video would move faster for the same amplitudes; the cap holds the
  // rush to the day's speed. Bisection, since the speed is not quite linear.
  let s = 1;
  if (cameraPeak(path, Z0, R0) > vmax) {
    let lo = 0, hi = 1;
    for (let it = 0; it < 40; it++) {
      const mid = (lo + hi) / 2;
      if (cameraPeak(path, Z0 * mid, R0 * mid) <= vmax) lo = mid; else hi = mid;
    }
    s = lo;
  }
  let Z = Z0 * s * amp, R = R0 * s * amp;
  // The push-in about the pivot and the drift both carry the line outward, and
  // the speed cap knows nothing of the city: one that fills the fit box width
  // (D3.2, x 50..870) would reach x 19..901 at frame 0, past the frame edge
  // with FRAME_ZOOM over 1 and under the action buttons beyond x 880. One more
  // factor scales both down until the line's bbox stays in the keep rect.
  const box = boundaryBoxPx(), keep = cameraKeepRect(box);
  let bound = 1;
  if (keep && !cameraKeeps(path, Z, R, box, keep)) {
    let lo = 0, hi = 1;
    for (let it = 0; it < 40; it++) {
      const mid = (lo + hi) / 2;
      if (cameraKeeps(path, Z * mid, R * mid, box, keep)) lo = mid; else hi = mid;
    }
    bound = lo;
  }
  Z *= bound;
  R *= bound;
  if (!(Z > 0) && !(R > 0)) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (let k = 0; k < CAMERA_STEPS; k++) {
    const m = cameraMatrix(path, Z, R, k / CAMERA_STEPS);
    x0 = Math.min(x0, -m.e / m.z);
    y0 = Math.min(y0, -m.f / m.z);
    x1 = Math.max(x1, (W - m.e) / m.z);
    y1 = Math.max(y1, (H - m.f) / m.z);
  }
  // A few px of slack keep the bilinear taps at the screen edge inside the
  // cached base, and the sampled phases cover the motion between them.
  const pad = 3;
  const k = 1 + Z;
  const m0 = cameraMatrix(path, Z, R, 0);
  // Origin chosen so that at frame 0 the cache's pixel grid lands on the
  // frame's: the hook frame is one exact mipmap level, with no resampling.
  const q = CAMERA_SUPERSAMPLE;
  const tx0 = Math.floor(k * (x0 - pad) + m0.e), ty0 = Math.floor(k * (y0 - pad) + m0.f);
  const ax = (tx0 - m0.e) / k, ay = (ty0 - m0.f) / k;
  cam = {
    path, Z, R, scale: s, amp, bound, box, keep, k, q, ax, ay, tx0, ty0,
    w: Math.ceil((x1 + pad - ax) * k * q), h: Math.ceil((y1 + pad - ay) * k * q),
    peak: cameraPeak(path, Z, R) * CONFIG.FPS / totalFrames / W,
    outside: null, rings: null, dim: null, source: null,
  };
}

// The camera of frame phase u, or null when it is off.
function cameraAt(u) {
  return cam ? cameraMatrix(cam.path, cam.Z, cam.R, u) : null;
}

// The cached base, drawn finer than the push-in scale, scaled down to zoom z;
// or with CAMERA_BASE 'vector' the base drawn afresh under the camera.
function drawBaseCam(m) {
  if (cam.source) {
    const v = { w: W, h: H, k: m.z, s: SCALE * m.z, ox: OX * m.z + m.e, oy: OY * m.z + m.f };
    ctx.drawImage(buildBaseV4(cam.source.basemap, cam.source.network, v), 0, 0);
    return;
  }
  const sc = m.z / (cam.k * cam.q);
  let tx = m.z * cam.ax + m.e, ty = m.z * cam.ay + m.f;
  if (m.start) { tx = cam.tx0; ty = cam.ty0; }
  ctx.setTransform(sc, 0, 0, sc, tx, ty);
  // 'medium' filters through mipmaps; the sprites keep the default 'low'.
  ctx.imageSmoothingQuality = 'medium';
  ctx.drawImage(baseCanvas, 0, 0);
  ctx.imageSmoothingQuality = 'low';
  ctx.setTransform(1, 0, 0, 1, 0, 0);
}

// strokeRibbons under the camera: the paths stay in base px and the layer
// transform carries the camera, the widths divided by the zoom so a trail is
// as wide on screen at every phase.
function strokeRibbonsCam(layer, ts, onlyMode, m) {
  const bands = CONFIG.TRAIL_BANDS;
  layer.lineCap = 'butt';
  layer.lineJoin = 'round';
  layer.setTransform(ts * m.z, 0, 0, ts * m.z, ts * m.e, ts * m.f);
  for (let k = 0; k < modes.length; k++) {
    if (onlyMode !== undefined && k !== onlyMode) continue;
    const paths = bandPaths[k];
    layer.strokeStyle = modes[k].trailCss;
    for (let b = 0; b < bands; b++) {
      const path = paths[b];
      if (!path) continue;
      const a = CONFIG.TRAIL_ALPHA * (1 - (b + 0.5) / bands);
      if (CONFIG.TRAIL_SHOULDER_ALPHA > 0 && b < CONFIG.TRAIL_SHOULDER_BANDS) {
        layer.globalAlpha = a * CONFIG.TRAIL_SHOULDER_ALPHA;
        layer.lineWidth = CONFIG.TRAIL_SHOULDER_W / m.z;
        layer.stroke(path);
      }
      layer.globalAlpha = a;
      layer.lineWidth = CONFIG.TRAIL_CORE_W / m.z;
      layer.stroke(path);
    }
  }
  layer.setTransform(1, 0, 0, 1, 0, 0);
  layer.globalAlpha = 1;
}

// strokeColorRibbons under the camera, in the same order.
function strokeColorRibbonsCam(layer, ts, m) {
  const bands = CONFIG.TRAIL_BANDS;
  const nc = colors.length;
  layer.lineCap = 'butt';
  layer.lineJoin = 'round';
  layer.setTransform(ts * m.z, 0, 0, ts * m.z, ts * m.e, ts * m.f);
  for (let b = bands - 1; b >= 0; b--) {
    const a = CONFIG.TRAIL_ALPHA * (1 - (b + 0.5) / bands);
    if (CONFIG.TRAIL_SHOULDER_ALPHA > 0 && b < CONFIG.TRAIL_SHOULDER_BANDS) {
      layer.globalAlpha = a * CONFIG.TRAIL_SHOULDER_ALPHA;
      layer.lineWidth = CONFIG.TRAIL_SHOULDER_W / m.z;
      for (let k = 0; k < nc; k++) {
        const path = colorPaths[k * bands + b];
        if (!path) continue;
        layer.strokeStyle = colors[k].css;
        layer.stroke(path);
      }
    }
    layer.globalAlpha = a;
    layer.lineWidth = CONFIG.TRAIL_CORE_W / m.z;
    for (let k = 0; k < nc; k++) {
      const path = colorPaths[k * bands + b];
      if (!path) continue;
      layer.strokeStyle = colors[k].css;
      layer.stroke(path);
    }
  }
  layer.setTransform(1, 0, 0, 1, 0, 0);
  layer.globalAlpha = 1;
}

// stampTrailSprites at the camera's positions; the stamps keep their size.
function stampTrailSpritesCam(layer, sprites, first, last, ts, m) {
  const half = trailHalf;
  const w = layer.canvas.width + half, h = layer.canvas.height + half;
  for (let s = first; s <= last; s++) {
    const x = m.z * sampleX[s] + m.e * ts, y = m.z * sampleY[s] + m.f * ts;
    if (x < -half || x > w || y < -half || y > h) continue;
    layer.drawImage(sprites[s], Math.round(x - half), Math.round(y - half));
  }
}

// The outside dimming and the city line under the camera, as vectors each
// frame: a cached overlay would have to be resampled, which softens the
// brightest thin line on screen. The line keeps its width in screen px.
function drawBoundaryCam(m) {
  if (!cam.rings) return;
  ctx.globalCompositeOperation = 'source-over';
  ctx.setTransform(m.z, 0, 0, m.z, m.e, m.f);
  if (cam.outside) {
    ctx.globalAlpha = 1;
    ctx.fillStyle = cam.dim;
    ctx.fill(cam.outside, 'evenodd');
  }
  if (CONFIG.CITY_LINE_W > 0 && CONFIG.CITY_LINE_ALPHA > 0) {
    ctx.strokeStyle = CONFIG.COLORS.cityLine;
    ctx.globalAlpha = clamp01(CONFIG.CITY_LINE_ALPHA);
    ctx.lineWidth = CONFIG.CITY_LINE_W / m.z;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke(cam.rings);
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.globalAlpha = 1;
}

function fontsV4() {
  return CONFIG.FONT_SET === 'extended'
    ? { mont: 'MontserratX', tnum: 'MontserratXTnum', inter: 'InterX' }
    : { mont: 'Montserrat', tnum: 'MontserratTnum', inter: 'Inter' };
}

function bgRGB() {
  return cssToRGB(CONFIG.COLORS.bg, [7, 8, 12]);
}

// A mode's own colours, the source of MODE_SOURCES moved into the envelope.
function modeColorsV4(id) {
  const n = window.BusmapColor.normalise(MODE_SOURCES[id] || MODE_SOURCES.bus, themeEnv, bgRGB());
  return { line: window.BusmapColor.parseHex(n.line), trail: window.BusmapColor.parseHex(n.trail), n };
}

async function initV4(basemap, network) {
  if (!window.BusmapColor) throw new Error('web/color.js is not loaded');
  const renderQuery = parseV4Query();
  isV4 = true;
  meta = network.meta;
  shapes = network.shapes;
  trips = network.trips;
  hist = network.hist;
  const variants = meta.variants && typeof meta.variants === 'object' ? meta.variants : {};
  if (!CONFIG.VARIANT) CONFIG.VARIANT = Object.keys(variants)[0] || '';
  variantV = variants[CONFIG.VARIANT];
  if (!variantV) {
    throw new Error(`unknown variant "${CONFIG.VARIANT}"; this network has: ${Object.keys(variants).join(', ') || 'none'}`);
  }
  const tl = meta.timeline || {};
  periodS = Number(tl.period) || 86400;
  histN = Number(meta.hist_period) || hist.length;
  if (!Array.isArray(hist) || hist.length !== histN) throw new Error(`hist has ${hist && hist.length} bins, meta.hist_period is ${histN}`);
  // The page's own copy of the window, which timeAtProgress and progressAt clamp to.
  meta.day_start = variantV.start;
  meta.day_end = variantV.end;
  setFrame(variantV.frame || meta.frame);

  // A query value wins even when empty: ?preset= and ?theme= draw without one.
  if (!pinned.has('PRESET')) CONFIG.PRESET = meta.preset || '';
  if (CONFIG.PRESET) {
    if (!/^[A-Za-z0-9_-]+$/.test(CONFIG.PRESET)) throw new Error(`bad preset name ${CONFIG.PRESET}`);
    const preset = await fetchJSON(`presets/${CONFIG.PRESET}.json`);
    applyRender(preset.render);
  }
  for (const [key, value] of Object.entries(V4_COLORS)) if (!(key in CONFIG.COLORS)) CONFIG.COLORS[key] = value;
  themeEnv = { ...DEFAULT_ENV };
  if (!pinned.has('THEME')) CONFIG.THEME = (meta.theme && meta.theme.batch) || '';
  if (CONFIG.THEME) {
    const themes = await fetchJSON('themes.json');
    if (!Object.hasOwn(themes, CONFIG.THEME)) {
      throw new Error(`unknown theme "${CONFIG.THEME}"; web/themes.json has: ${Object.keys(themes).join(', ')}`);
    }
    applyThemeTokens(themes[CONFIG.THEME]);
  }
  if (meta.theme && typeof meta.theme === 'object') {
    const rest = { ...meta.theme };
    delete rest.batch;
    applyTheme(rest);
  }
  // The scrims and the outside dimming are the background, whatever the theme.
  CONFIG.COLORS.scrim = CONFIG.COLORS.bg;
  CONFIG.COLORS.outside = CONFIG.COLORS.bg;
  applyRender(meta.render);
  applyRender(variantV.render);
  applyRender(renderQuery);
  checkV4Config();
  finishFrame();
  totalFrames = CONFIG.HOLD_START + CONFIG.DURATION_FRAMES + CONFIG.HOLD_END;
  buildCameraV4();

  // Fonts come before the colours: the chips line is measured to know which
  // groups keep their own colour (B6, B9).
  const F = fontsV4();
  const faces = CONFIG.HUD_LAYOUT === 'shorts'
    ? [`700 ${CONFIG.TITLE_SIZE}px ${F.mont}`, `400 36px ${F.mont}`, `800 80px ${F.mont}`, `500 26px ${F.mont}`,
      `700 26px ${F.mont}`, `800 132px ${F.mont}`, `800 88px ${F.tnum}`, `600 40px ${F.tnum}`, `500 26px ${F.tnum}`,
      `500 24px ${F.tnum}`, `600 26px ${F.tnum}`, `400 22px ${F.inter}`, `500 44px ${F.inter}`, `400 32px ${F.inter}`]
    : ['600 58px Montserrat', '400 38px Montserrat', '800 108px MontserratTnum', '600 32px Montserrat',
      '500 24px Montserrat', '600 32px MontserratTnum', '500 24px MontserratTnum', '500 20px Montserrat', '400 18px Inter',
      `800 132px ${F.mont}`, `500 44px ${F.inter}`, `400 32px ${F.inter}`];
  await Promise.all(faces.map((f) => document.fonts.load(f).catch(() => null)));
  await document.fonts.ready;

  buildModesV4(network);
  chipMerged = new Set();
  if (CONFIG.HUD_LAYOUT === 'shorts' && CONFIG.MODE_CHIPS && Array.isArray(meta.groups) && meta.groups.length) {
    chipMerged = new Set(fitChips(chipParts(meta.groups, () => null), F, SHORTS_TEXT_W).merged);
  }
  const brand = CONFIG.COLOR_BY === 'brand' || (CONFIG.COLOR_BY === '' && meta.color_by === 'brand');
  if (brand) buildColorsBrand(network);
  else buildColors(network);
  buildGroupsV4(network);

  const kq = cam && cam.k * cam.q;
  baseCanvas = buildBaseV4(basemap, network, cam && {
    w: cam.w, h: cam.h, k: kq, s: SCALE * kq, ox: (OX - cam.ax) * kq, oy: (OY - cam.ay) * kq,
  });
  if (cam && CONFIG.CAMERA_BASE === 'vector') cam.source = { basemap, network };
  buildSpritesV4();
  await promoteSprites();
  buildRibbons();
  buildOccurrences();
  buildWarpV4();
  if (CONFIG.HUD_LAYOUT === 'shorts') buildShortsLayout();
  else buildHudLayout();
  buildSparklineV4();
  if (CONFIG.HUD_LAYOUT === 'shorts') buildShortsStatics();
  else buildHudStatics();
  buildBoundary();
  buildCard();
  checkLayoutV4();
  return { basemap, network };
}

// buildModes with the theme's mode colours: v4 files carry no mode colours.
function buildModesV4(network) {
  const list = Array.isArray(meta.modes) && meta.modes.length ? meta.modes : null;
  modes = (list || [{ id: 'bus', label: 'buses', singular: 'bus' }]).map((m) => {
    const c = modeColorsV4(m.id);
    return { id: m.id, label: m.label || m.id, singular: m.singular || m.label || m.id, color: c.line, trail: c.trail };
  });
  for (const m of modes) m.trailCss = rgba(m.trail, 1);
  const index = new Map(modes.map((m, i) => [m.id, i]));
  const top = CONFIG.STREETCAR_ON_TOP && index.has('streetcar') ? index.get('streetcar') : -1;
  onTopMode = top;
  layerOrder = modes.map((_, i) => i).filter((i) => i !== top);
  if (top >= 0) layerOrder.push(top);
  const routeMode = network.routes.map((r) => index.get(r.mode) || 0);
  routeModes = routeMode;
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

// color_by "brand" (B6). Brands whose group is shown are placed and get
// their own colour through the distinctness ladder; every other brand is
// drawn in the theme's foreign grey, like the "other" chip, so a small brand
// just across the border never looks like the host. Then the byRoute
// machinery of buildColors, one colour class per drawn colour.
function buildColorsBrand(network) {
  byRoute = true;
  const BC = window.BusmapColor;
  const C = CONFIG.COLORS;
  const bg = bgRGB();
  const env = themeEnv;
  const brands = Array.isArray(meta.brands) ? meta.brands : [];
  // A group the chips line folds into "other" is drawn like "other", so every
  // trail colour on the map has its chip.
  const shown = new Set((meta.groups || []).map((g) => g.id).filter((id) => id !== 'other' && !chipMerged.has(id)));
  const groupOf = (b) => (b.kind === 'gtfs' || b.kind === 'mode' ? b.id : b.entry || b.id);
  const firstMode = new Map();
  for (const r of network.routes) if (Number.isInteger(r.brand) && !firstMode.has(r.brand)) firstMode.set(r.brand, r.mode);
  // A brand with an empty (or uninformative) hex takes its mode's colour.
  const sourceOf = (b, i) => {
    const q = brandHexQuery.get(b.id);
    const hex = q !== undefined ? q : typeof b.hex === 'string' ? b.hex.replace(/^#/, '').toLowerCase() : '';
    if (BC.informative(hex)) return hex;
    const mode = b.kind === 'mode' ? String(b.id).replace(/^mode:/, '') : firstMode.get(i);
    return MODE_SOURCES[mode] || MODE_SOURCES.bus;
  };
  for (const id of brandHexQuery.keys()) {
    if (!brands.some((b) => b.id === id)) console.warn(`?brandhex: no brand "${id}" in meta.brands`);
  }
  const foreign = BC.toHex(cssToRGB(C.foreign, [138, 141, 150]));
  // The dormant line of a foreign brand: foreign at half strength over bg.
  const foreignLine = BC.over(foreign, 0.5, bg);
  brandMap = brands.map((b, i) => ({ id: b.id, hex: sourceOf(b, i), trail: foreign, line: foreignLine, how: 'foreign', placed: false }));
  const order = brands.map((_, i) => i).filter((i) => shown.has(groupOf(brands[i])))
    .sort((a, b) => (brands[b].share || 0) - (brands[a].share || 0) || (brands[a].id < brands[b].id ? -1 : brands[a].id > brands[b].id ? 1 : a - b));
  const placedTrails = [];
  const upL = Math.min(0.10, 0.92 - env.trailL);
  for (const i of order) {
    const b = brands[i], e = brandMap[i];
    let best = null, pass = null;
    for (const [how, dL0, rot] of BRAND_LADDER) {
      let src = e.hex;
      if (how === 'alt') {
        const alt = typeof b.alt === 'string' ? b.alt.replace(/^#/, '').toLowerCase() : '';
        if (!BC.informative(alt) || brandHexQuery.has(b.id)) continue;
        src = alt;
      }
      const n = BC.normalise(src, env, bg, how === 'dL+0.10' ? upL : dL0, rot);
      if (!n) continue;
      const dmin = placedTrails.reduce((m, t) => Math.min(m, BC.deltaE(n.trail, t)), Infinity);
      const cand = { how, n, dmin };
      if (!best || dmin > best.dmin) best = cand;
      if (dmin >= CONFIG.BRAND_MIN_DE && BC.contrast(n.trail, bg) >= 3) { pass = cand; break; }
    }
    const pick = pass || best;
    if (!pass) console.warn(`brand ${b.id}: no ladder step is ${CONFIG.BRAND_MIN_DE} from the placed brands; ${pick.how} is the farthest (${pick.dmin.toFixed(3)})`);
    Object.assign(e, { trail: pick.n.trail, line: pick.n.line, how: pick.how, placed: true });
    placedTrails.push(pick.n.trail);
  }

  // One colour class per drawn colour: rail brands after the others, then by
  // trips ascending, so lines lie over buses and the busiest bus brand is on
  // top of the bus layer.
  const index = new Map();
  const found = [];
  const classOf = (trail, line, rail, rank) => {
    const key = `${trail}/${line}`;
    if (!index.has(key)) {
      index.set(key, found.length);
      found.push({ trail, line, rail, rank, trips: 0, order: found.length });
    }
    const c = found[index.get(key)];
    c.rail = c.rail || rail;
    c.rank = Math.min(c.rank, rank);
    return index.get(key);
  };
  const brandClass = brandMap.map((e, i) => classOf(e.trail, e.line, e.placed && brands[i].rail === true, e.placed ? i : 1e6));
  const routeClass = network.routes.map((r) => {
    if (Number.isInteger(r.brand) && r.brand >= 0 && r.brand < brands.length) return brandClass[r.brand];
    const mc = modeColorsV4(r.mode).n;
    return classOf(mc.trail, mc.line, r.mode !== 'bus', 1e6 + 1);
  });
  const tripClass = new Uint16Array(trips.length);
  const used = new Uint8Array(shapes.length);
  for (let n = 0; n < trips.length; n++) {
    const k = routeClass[trips[n].r] || 0;
    tripClass[n] = k;
    found[k].trips++;
    used[trips[n].s] = 1;
  }
  let fallback = -1;
  for (let i = 0; i < shapes.length; i++) {
    if (!used[i]) { fallback = classOf(foreign, foreignLine, false, 1e6 + 2); break; }
  }
  if (!found.length) classOf(foreign, foreignLine, false, 1e6 + 2);
  const sorted = found.slice().sort((a, b) => (a.rail - b.rail) || a.trips - b.trips || a.rank - b.rank || a.order - b.order);
  const rank = new Uint16Array(found.length);
  sorted.forEach((c, i) => { rank[c.order] = i; });
  colors = sorted.map((c) => ({ hex: c.trail, css: `#${c.trail}`, lineCss: `#${c.line}`, rgb: BC.parseHex(c.trail), trips: c.trips }));
  tripColor = new Uint16Array(trips.length);
  for (let n = 0; n < trips.length; n++) tripColor[n] = rank[tripClass[n]];
  const seen = new Set();
  colorShapes = colors.map(() => []);
  for (let n = 0; n < trips.length; n++) {
    const key = trips[n].s * colors.length + tripColor[n];
    if (seen.has(key)) continue;
    seen.add(key);
    colorShapes[tripColor[n]].push(trips[n].s);
  }
  if (fallback >= 0) {
    for (let i = 0; i < shapes.length; i++) if (!used[i]) colorShapes[rank[fallback]].push(i);
  }
  for (const list of colorShapes) list.sort((a, b) => a - b);
  if (CONFIG.TRAIL_MODE === 'sprite') {
    console.warn('color_by brand draws ribbon trails; trailmode=sprite ignored');
    CONFIG.TRAIL_MODE = 'ribbon';
  }
  // Chip dots take the group's entry brand colour; "other" is foreign.
  groupColor = new Map();
  const byId = new Map(brands.map((b, i) => [b.id, brandMap[i]]));
  for (const g of meta.groups || []) {
    const e = g.id === 'other' ? null : byId.get(g.brand) || byId.get(g.id);
    groupColor.set(g.id, `#${e && e.placed ? e.trail : foreign}`);
  }
}

// buildGroups for v4 groups ({id, label, brand, share}); "other" collects
// routes of unknown groups.
function buildGroupsV4(network) {
  const list = Array.isArray(meta.groups) && meta.groups.length ? meta.groups : null;
  if (!list) return;
  groups = list.map((g) => ({ id: g.id, label: g.label || g.id, brand: g.brand || null, share: Number(g.share) || 0, shown: false }));
  const index = new Map(groups.map((g, i) => [g.id, i]));
  const fallback = index.has('other') ? index.get('other') : groups.length - 1;
  const routeGroup = network.routes.map((r) => (index.has(r.group) ? index.get(r.group) : fallback));
  tripGroup = new Uint8Array(trips.length);
  for (let n = 0; n < trips.length; n++) {
    const g = routeGroup[trips[n].r];
    tripGroup[n] = g === undefined ? fallback : g;
    groups[tripGroup[n]].shown = true;
  }
  if (!groups.some((g) => g.shown)) for (const g of groups) g.shown = true;
  runningByGroup = new Int32Array(groups.length);
}

// token moved towards (gain < 1) or away from (gain > 1) the background, per
// sRGB channel (B12).
function gainCss(token, gain) {
  if (gain === 1) return token;
  const bg = bgRGB();
  const c = cssToRGB(token, bg);
  const v = c.map((x, i) => Math.round(Math.min(255, Math.max(0, bg[i] + gain * (x - bg[i])))));
  return `rgb(${v[0]},${v[1]},${v[2]})`;
}

// tracePolyline into a view other than the frame: the camera's cached base
// (B18), a larger canvas at the push-in scale with its own origin.
function tracePolylineV4(g, xy, v) {
  const n = xy.length;
  let minx = Infinity, maxx = -Infinity, miny = Infinity, maxy = -Infinity;
  for (let i = 0; i < n; i += 2) {
    const x = v.ox + xy[i] * v.s;
    const y = v.oy - xy[i + 1] * v.s;
    if (x < minx) minx = x;
    if (x > maxx) maxx = x;
    if (y < miny) miny = y;
    if (y > maxy) maxy = y;
  }
  if (maxx < -4 || minx > v.w + 4 || maxy < -4 || miny > v.h + 4) return false;
  g.moveTo(v.ox + xy[0] * v.s, v.oy - xy[1] * v.s);
  for (let i = 2; i < n; i += 2) g.lineTo(v.ox + xy[i] * v.s, v.oy - xy[i + 1] * v.s);
  return true;
}

function strokeManyV4(g, list, v, chunk = 3000) {
  for (let s = 0; s < list.length; s += chunk) {
    g.beginPath();
    const end = Math.min(list.length, s + chunk);
    for (let i = s; i < end; i++) tracePolylineV4(g, list[i], v);
    g.stroke();
  }
}

// buildBase with the road and water gains, and the dormant network of a
// colour class in its line colour (lineCss) rather than its trail colour.
// With a view v (the camera's base, B18) it is all drawn v.k times larger,
// widths and dashes included, so the camera at zoom 1 shows today's base;
// without one every call is today's.
function buildBaseV4(basemap, network, v = null) {
  const C = CONFIG.COLORS;
  const rg = CONFIG.BASE_ROADS_GAIN, wg = CONFIG.BASE_WATER_GAIN;
  const bw = v ? v.w : W, bh = v ? v.h : H, L = v ? v.k : 1;
  const trace = v ? (g2, xy) => tracePolylineV4(g2, xy, v) : tracePolyline;
  const many = v ? (g2, list) => strokeManyV4(g2, list, v) : strokeMany;
  const c = document.createElement('canvas');
  c.width = bw;
  c.height = bh;
  const g = c.getContext('2d', { alpha: false });
  g.fillStyle = C.bg;
  g.fillRect(0, 0, bw, bh);
  g.lineCap = 'round';
  g.lineJoin = 'round';

  const water = basemap.water || { poly: [], line: [] };
  g.fillStyle = gainCss(C.water, wg);
  g.beginPath();
  for (const ring of water.poly || []) {
    if (trace(g, ring)) g.closePath();
  }
  g.fill();
  if (water.holes) {
    g.fillStyle = C.bg;
    g.beginPath();
    for (const ring of water.holes) {
      if (trace(g, ring)) g.closePath();
    }
    g.fill();
  }

  g.strokeStyle = gainCss(C.waterLine, wg);
  const byClass = { river: [], canal: [], stream: [] };
  for (const l of water.line || []) (byClass[l.c] || byClass.stream).push(l.xy);
  g.lineWidth = 2 * L;
  many(g, byClass.river);
  g.lineWidth = 1.4 * L;
  many(g, byClass.canal);
  g.lineWidth = 1 * L;
  many(g, byClass.stream);

  const roads = basemap.roads || {};
  g.lineWidth = 1 * L;
  g.strokeStyle = gainCss(C.minor, rg);
  many(g, roads.minor || []);
  g.lineWidth = 1.6 * L;
  g.strokeStyle = gainCss(C.major, rg);
  many(g, roads.major || []);
  g.setLineDash([7 * L, 5 * L]);
  g.lineWidth = 1.2 * L;
  g.strokeStyle = gainCss(C.rail, rg);
  many(g, roads.rail || []);
  g.setLineDash([]);

  g.globalAlpha = 0.4;
  g.lineWidth = 1 * L;
  g.strokeStyle = C.boundary;
  many(g, basemap.boundary || []);
  g.globalAlpha = 1;

  g.globalCompositeOperation = 'lighter';
  g.lineWidth = CONFIG.ROUTE_WIDTH * L;
  if (CONFIG.OSM_ROUTES && basemap.osm_routes) {
    g.strokeStyle = rgba(C.routeRGB, CONFIG.OSM_ROUTES_ALPHA);
    g.lineWidth = 1.5 * L;
    for (const r of basemap.osm_routes) {
      g.beginPath();
      if (trace(g, r.xy)) g.stroke();
    }
    g.lineWidth = CONFIG.ROUTE_WIDTH * L;
  }
  if (byRoute) {
    g.globalCompositeOperation = 'source-over';
    const ml = document.createElement('canvas');
    ml.width = bw;
    ml.height = bh;
    const lg = ml.getContext('2d');
    lg.lineCap = 'round';
    lg.lineJoin = 'round';
    lg.lineWidth = CONFIG.ROUTE_WIDTH * L;
    for (let k = 0; k < colors.length; k++) {
      lg.strokeStyle = colors[k].lineCss || colors[k].css;
      lg.beginPath();
      for (const i of colorShapes[k]) trace(lg, network.shapes[i].xy);
      lg.stroke();
    }
    g.globalAlpha = CONFIG.ROUTE_ALPHA;
    g.drawImage(ml, 0, 0);
    g.globalAlpha = 1;
    return c;
  }
  if (CONFIG.ROUTE_BLEND === 'bounded') {
    g.globalCompositeOperation = 'source-over';
    // The mode layers are frame sized and shared with the trails; a view gets one of its own.
    let viewLayer = null;
    if (v) {
      const vc = document.createElement('canvas');
      vc.width = bw;
      vc.height = bh;
      viewLayer = vc.getContext('2d');
    }
    for (let m = 0; m < modes.length; m++) {
      const ml = viewLayer || modeLayer(m);
      ml.globalCompositeOperation = 'source-over';
      ml.globalAlpha = 1;
      ml.clearRect(0, 0, bw, bh);
      ml.lineCap = 'round';
      ml.lineJoin = 'round';
      ml.lineWidth = CONFIG.ROUTE_WIDTH * L;
      ml.strokeStyle = rgba(modes[m].color, 1);
      ml.beginPath();
      for (let i = 0; i < network.shapes.length; i++) {
        if (shapeMode[i] === m) trace(ml, network.shapes[i].xy);
      }
      ml.stroke();
      g.globalAlpha = CONFIG.ROUTE_ALPHA;
      g.drawImage(ml.canvas, 0, 0);
      g.globalAlpha = 1;
    }
    return c;
  }
  let cur = -1;
  for (let i = 0; i < network.shapes.length; i++) {
    const m = shapeMode[i];
    if (m !== cur) {
      g.strokeStyle = rgba(modes[m].color, CONFIG.ROUTE_ALPHA);
      cur = m;
    }
    g.beginPath();
    if (trace(g, network.shapes[i].xy)) g.stroke();
  }
  g.globalCompositeOperation = 'source-over';
  return c;
}

// buildSprites with the theme's dot core: dotCore, and none at all for
// BUS_CORE_R 0 (the week: a bus moves tens of pixels a frame and a hard core
// strobes, B12).
function buildSpritesV4() {
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

  const core = CONFIG.COLORS.dotCore || '#ffffff';
  const busSprite = (halo) => {
    const sprite = makeRadialSprite(Math.max(CONFIG.BUS_HALO_R, CONFIG.BUS_CORE_R, 1), [
      [0, rgba(halo, CONFIG.BUS_HALO_ALPHA)],
      [0.35, rgba(halo, CONFIG.BUS_HALO_ALPHA * 0.45)],
      [0.7, rgba(halo, CONFIG.BUS_HALO_ALPHA * 0.12)],
      [1, rgba(halo, 0)],
    ]);
    if (CONFIG.BUS_CORE_R > 0) {
      const g = sprite.getContext('2d');
      g.fillStyle = core;
      g.beginPath();
      g.arc(sprite.width / 2, sprite.width / 2, CONFIG.BUS_CORE_R, 0, Math.PI * 2);
      g.fill();
    }
    return sprite;
  };
  busSprites = byRoute
    ? colors.map((c) => busSprite(c.rgb))
    : modes.map((mode) => busSprite(modes.length > 1 ? mode.trail : [255, 255, 255]));
  busHalf = busSprites[0].width / 2;

  if (byRoute) {
    const c = document.createElement('canvas');
    c.width = Math.round(W * ts);
    c.height = Math.round(H * ts);
    colorLayer = c.getContext('2d');
    colorPaths = new Array(colors.length * CONFIG.TRAIL_BANDS).fill(null);
  } else if (ts !== 1) {
    trailCanvas = document.createElement('canvas');
    trailCanvas.width = Math.round(W * ts);
    trailCanvas.height = Math.round(H * ts);
    trailCtx = trailCanvas.getContext('2d');
  }
}

// Every (trip, weekday, period shift) that can be on screen inside the
// window (B7). The composite repeats with period P, so a trip's copy one
// period later is the same trip, which is why the loop has no seam.
function buildOccurrences() {
  const W0 = meta.day_start, W1 = meta.day_end, P = periodS;
  const week = meta.timeline && meta.timeline.kind === 'week';
  const t0s = [], ns = [], offs = [];
  let warned = false;
  maxDur = 0;
  for (let n = 0; n < trips.length; n++) {
    const t0 = tripT0[n], t1 = tripT1[n];
    if (t1 - t0 > maxDur) maxDur = t1 - t0;
    let w = 1;
    if (week) {
      w = trips[n].w;
      if (!Number.isInteger(w)) {
        if (!warned) console.warn('week network: a trip has no "w" weekday mask; drawing it every day');
        warned = true;
        w = 127;
      }
    }
    for (let k = 0; k < 7; k++) {
      if (!(w >> k & 1)) continue;
      const b = 86400 * k;
      for (let j = -1; j <= 2; j++) {
        const off = b + j * P;
        if (t1 + off + trailWindow >= W0 && t0 + off <= W1) {
          t0s.push(t0 + off);
          ns.push(n);
          offs.push(off);
        }
      }
    }
  }
  occN = t0s.length;
  const idx = new Int32Array(occN);
  for (let j = 0; j < occN; j++) idx[j] = j;
  idx.sort((a, b) => t0s[a] - t0s[b] || ns[a] - ns[b] || offs[a] - offs[b]);
  occTrip = new Int32Array(occN);
  occOff = new Float64Array(occN);
  occT0 = new Float64Array(occN);
  occT1 = new Float64Array(occN);
  for (let j = 0; j < occN; j++) {
    const k = idx[j];
    occTrip[j] = ns[k];
    occOff[j] = offs[k];
    occT0[j] = t0s[k];
    occT1[j] = tripT1[ns[k]] + offs[k];
  }
}

// First index j with arr[j] >= v (arr sorted ascending, n entries).
function lowerBound(arr, n, v) {
  let lo = 0, hi = n;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] < v) lo = mid + 1; else hi = mid;
  }
  return lo;
}

// arr at fractional minute min, linearly interpolated, indices taken
// modulo the period.
function interpCircular(arr, min) {
  const n = arr.length;
  const i = Math.floor(min);
  const f = min - i;
  const a = arr[mod(i, n)] || 0;
  const b = arr[mod(i + 1, n)] || 0;
  return a + (b - a) * f;
}

// Raw mean count at a fractional minute; every number on screen reads this
// (B7), the smoothed curve only places the sparkline.
function histRaw(min) {
  return interpCircular(hist, min);
}

function sparkAt(min) {
  return interpCircular(sparkSmooth, min);
}

// Centred moving average over `win` minutes with circular wrap, as the
// legacy sparkline smooths (2 * floor(win / 2) + 1 bins).
function smoothCircular(arr, win) {
  const n = arr.length;
  const half = Math.floor((win || 1) / 2);
  const out = new Float64Array(n);
  let sum = 0;
  for (let k = -half; k <= half; k++) sum += arr[mod(k, n)] || 0;
  for (let m = 0; m < n; m++) {
    out[m] = sum / (2 * half + 1);
    sum += (arr[mod(m + half + 1, n)] || 0) - (arr[mod(m - half, n)] || 0);
  }
  return out;
}

// Time warp over the window (B8). 'activity' gives each minute screen time
// in proportion to how busy it is, never under TIME_WARP_FLOOR; the week's
// 'activity-daily' takes each composite day's own peak, days running 04:30 to
// 04:30, so a quiet Sunday still moves at full speed at its own busiest hour.
function buildWarpV4() {
  const mode = CONFIG.TIME_WARP_MODE;
  if (mode === 'empty') {
    buildWarp();
    return;
  }
  const m0 = Math.floor(meta.day_start / 60);
  const m1 = Math.ceil(meta.day_end / 60);
  const n = m1 - m0;
  const s = smoothCircular(hist, CONFIG.TIME_WARP_SMOOTH_MIN);
  let peakAll = 0;
  for (const v of s) if (v > peakAll) peakAll = v;
  const days = Math.max(1, Math.round(histN / 1440));
  const dayPeak = new Float64Array(days);
  for (let d = 0; d < days; d++) {
    for (let k = 0; k < 1440; k++) {
      const v = s[mod(270 + 1440 * d + k, histN)];
      if (v > dayPeak[d]) dayPeak[d] = v;
    }
  }
  const w = new Float64Array(n);
  const gamma = CONFIG.TIME_WARP_GAMMA, floor = CONFIG.TIME_WARP_FLOOR;
  for (let i = 0; i < n; i++) {
    if (mode === 'linear') { w[i] = 1; continue; }
    const m = m0 + i;
    const peak = mode === 'activity-daily' ? dayPeak[Math.floor(mod(m - 270, histN) / 1440) % days] : peakAll;
    w[i] = peak > 0 ? Math.max(floor, (s[mod(m, histN)] / peak) ** gamma) : 1;
  }
  const cum = new Float64Array(n + 1);
  for (let i = 0; i < n; i++) cum[i + 1] = cum[i] + w[i];
  warp = { m0, n, w, cum, total: cum[n] };
}

// Minutes of simulated time per frame at T, for the clock rounding (B9).
function rateAt(T) {
  const i = Math.min(warp.n - 1, Math.max(0, Math.floor(T / 60) - warp.m0));
  const frames = CONFIG.LOOP === 'none' ? CONFIG.DURATION_FRAMES : totalFrames;
  return warp.w[i] > 0 ? warp.total / frames / warp.w[i] : Infinity;
}

function frameTimeV4(i) {
  return CONFIG.LOOP === 'none' ? frameTime(i) : timeAtProgress(i / totalFrames);
}

// Width of a text in a font, with letter spacing in px as the title uses it.
function textWidth(text, font, spacing = 0) {
  ctx.font = font;
  ctx.letterSpacing = `${spacing}px`;
  const w = ctx.measureText(text).width;
  ctx.letterSpacing = '0px';
  return w;
}

// Splits text at spaces into at most maxLines lines of maxW px. 'greedy'
// fills each line in turn (the credit); 'balanced' picks the break that keeps
// the longer of two lines shortest (the card). null when it cannot fit.
function wrapText(text, font, maxW, maxLines, how, keep = '') {
  if (textWidth(text, font) <= maxW || maxLines < 2) return textWidth(text, font) <= maxW ? [text] : null;
  // The kept phrase (the place) wraps as one word: "Every bus in Richmond /
  // Hill, ..." under the title RICHMOND HILL reads as two names.
  const glue = '\u0001';
  const glued = keep && keep.includes(' ') ? text.split(keep).join(keep.split(' ').join(glue)) : text;
  const words = glued.split(' ').map((w) => w.split(glue).join(' '));
  if (how === 'balanced' && maxLines === 2) {
    let best = null;
    for (let k = 1; k < words.length; k++) {
      const a = words.slice(0, k).join(' '), b = words.slice(k).join(' ');
      const wa = textWidth(a, font), wb = textWidth(b, font);
      if (wa <= maxW && wb <= maxW && (!best || Math.max(wa, wb) < best.w)) best = { lines: [a, b], w: Math.max(wa, wb) };
    }
    return best && best.lines;
  }
  const lines = [];
  let cur = '';
  for (const word of words) {
    const next = cur ? `${cur} ${word}` : word;
    if (cur && textWidth(next, font) > maxW) {
      lines.push(cur);
      cur = word;
    } else {
      cur = next;
    }
  }
  lines.push(cur);
  return lines.length <= maxLines && lines.every((l) => textWidth(l, font) <= maxW) ? lines : null;
}

function joinWords(list) {
  if (list.length <= 1) return list.join('');
  return `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;
}

// Modes with at least MODE_PRESENT_MIN vehicles inside in some minute of the
// variant's window, which the count line and the card name (B9, B10). The
// peak minute alone missed GO trains inside for most of Whitby's rush, whose
// count then read as buses. make.py's modes_present uses the same rule.
const MODE_PRESENT_MIN = 0.5;
function modesInWindow() {
  const byMode = meta.hist_by_mode || {};
  const m0 = Math.ceil(variantV.start / 60), m1 = Math.ceil(variantV.end / 60);
  const list = modes.filter((md) => {
    const a = byMode[md.id];
    if (!Array.isArray(a) || !a.length) return false;
    for (let m = m0; m < m1; m++) if ((a[mod(m, a.length)] || 0) >= MODE_PRESENT_MIN) return true;
    return false;
  });
  return list.length ? list : modes;
}

// Shorts HUD geometry (B9). Text sizes are fitted once here, on the widest
// text the window can produce, so nothing changes size between frames.
function buildShortsLayout() {
  const F = fontsV4();
  const week = meta.timeline && meta.timeline.kind === 'week';
  const side = CONFIG.PANEL_SIDE || (HUD_OVERRIDE === 'left' || HUD_OVERRIDE === 'right' ? HUD_OVERRIDE : '')
    || (meta.panel && (meta.panel.side === 'left' || meta.panel.side === 'right') ? meta.panel.side : 'left');
  const dx = side === 'right' ? 260 : 0;
  const L = { F, week, side, dx, textX: 88 + dx, rightX: 592 + dx, textW: SHORTS_TEXT_W };

  let ts = Math.round(CONFIG.TITLE_SIZE);
  const title = meta.title || '';
  while (ts > 48 && textWidth(title, `700 ${ts}px ${F.mont}`, 0.12 * ts) > 796) ts -= 2;
  L.title = { text: title, size: ts, font: `700 ${ts}px ${F.mont}`, spacing: 0.12 * ts };
  const subtitle = variantV.label || meta.subtitle || '';
  let ss = 36;
  while (ss > 32 && textWidth(subtitle, `400 ${ss}px ${F.mont}`) > 796) ss--;
  L.subtitle = { text: subtitle, size: ss, font: `400 ${ss}px ${F.mont}` };

  // The count noun: a mode's own word when only one mode is inside in the window.
  const present = modesInWindow();
  L.nounMode = present.length === 1 ? present[0] : null;
  L.peakCount = variantV.peak ? variantV.peak.count : 0;
  const peakText = `${withCommas(L.peakCount)} ${countNoun(L, L.peakCount)} in ${meta.place || ''}`;
  let cs = 40;
  while (cs > 36 && textWidth(peakText, `600 ${cs}px ${F.tnum}`) > L.textW) cs--;
  L.split = textWidth(peakText, `600 ${cs}px ${F.tnum}`) > L.textW;
  if (L.split) {
    cs = 40;
    const a = `${withCommas(L.peakCount)} ${countNoun(L, L.peakCount)}`, b = `in ${meta.place || ''}`;
    while (cs > 36 && Math.max(textWidth(a, `600 ${cs}px ${F.tnum}`), textWidth(b, `600 ${cs}px ${F.tnum}`)) > L.textW) cs--;
  }
  L.count = { size: cs, font: `600 ${cs}px ${F.tnum}` };
  const up = L.split ? 44 : 0;
  L.clockY = (week ? 1238 : 1232) - up;
  L.clock = week ? { size: 40, font: `600 40px ${F.tnum}` } : { size: 88, font: `800 88px ${F.tnum}` };
  if (week) {
    let ws = 80;
    while (ws > 64 && textWidth('WEDNESDAY', `800 ${ws}px ${F.mont}`) > L.textW) ws -= 2;
    L.weekday = { size: ws, font: `800 ${ws}px ${F.mont}`, y: L.clockY - 50 };
  }
  L.countY = L.split ? 1238 : 1282;
  L.count2Y = 1282;
  L.panel = { x0: 60 + dx, x1: 620 + dx, y0: CONFIG.PANEL_TOP > 0 ? CONFIG.PANEL_TOP : (week ? 1110 : 1140) - up, y1: 1500 };

  // Chips: groups (else modes) with their means; fitted at each one's maximum
  // over the window.
  L.chips = null;
  if (CONFIG.MODE_CHIPS) {
    const C = CONFIG.COLORS;
    let parts;
    if (groups) {
      parts = chipParts(groups, (g) => (groupColor && groupColor.get(g.id)) || (g.id === 'other' ? C.foreign : C.breakdown));
    } else {
      const byMode = meta.hist_by_mode || {};
      parts = modes.map((m) => ({
        id: m.id, label: m.label, singular: m.singular, share: 0, other: false, color: m.trailCss,
        series: Array.isArray(byMode[m.id]) ? byMode[m.id] : new Array(histN).fill(0),
      }));
    }
    const { list, size, gap, merged, tried } = fitChips(parts, F, L.textW);
    if (merged.length && brandMap) console.warn(`chips: ${merged.join(', ')} joined "other" to fit ${L.textW} px; their trails are drawn as "other"`);
    L.chips = { parts: list, size, gap, font: `500 ${size}px ${F.tnum}`, y: 1320, merged, tried };
  }
  L.spark = { x0: 88 + dx, x1: 592 + dx, y0: 1334, y1: 1386 };
  L.axisY = 1416;
  L.axisFont = `500 26px ${F.mont}`;
  L.axisBold = `700 26px ${F.mont}`;
  L.peakFont = `600 26px ${F.tnum}`;
  // Constant over the video, so measured once rather than on every frame after the peak.
  const peakLabel = `peak ${withCommas(variantV.peak ? variantV.peak.count : 0)}`;
  L.peakLabel = { label: peakLabel, w: textWidth(peakLabel, L.peakFont) };
  L.creditFont = `400 22px ${F.inter}`;
  const credit = meta.credit || (Array.isArray(meta.attribution) ? meta.attribution.join(' ') : '');
  L.credit = wrapText(credit, L.creditFont, L.textW, 2, 'greedy');
  // Two lines read best broken between the data and the map credit, where
  // the line break takes the place of the separator dot.
  const dot = credit.indexOf(' · ');
  if (L.credit && L.credit.length === 2 && dot > 0) {
    const pair = [credit.slice(0, dot), credit.slice(dot + 3)];
    if (pair.every((l) => textWidth(l, L.creditFont) <= L.textW)) L.credit = pair;
  }
  if (!L.credit) {
    console.error(`credit "${credit}" needs more than two lines of ${L.textW} px at 22 px`);
    L.credit = wrapText(credit, L.creditFont, L.textW, 99, 'greedy') || [credit];
  }
  shorts = L;
}

function countNoun(L, n) {
  const m = L.nounMode;
  if (m) return n === 1 ? m.singular : m.label;
  return n === 1 ? 'vehicle' : 'vehicles';
}

function chipLabel(p, n) {
  return p.singular && n === 1 ? p.singular : p.label;
}

// Chip parts of the groups, with each group's mean series.
function chipParts(list, colorOf) {
  const byGroup = meta.hist_by_group || {};
  return list.map((g) => ({
    id: g.id, label: g.label || g.id, share: Number(g.share) || 0, other: g.id === 'other', color: colorOf(g),
    series: Array.isArray(byGroup[g.id]) ? byGroup[g.id] : new Array(histN).fill(0),
  }));
}

// Width of a chips line with each part at its maximum over the window, the
// widest it can be (B9).
function chipsWidth(list, size, gap, F) {
  const W0 = meta.day_start, W1 = meta.day_end;
  const maxOver = (series) => {
    let p = 0;
    for (let m = Math.floor(W0 / 60); m <= Math.ceil(W1 / 60); m++) p = Math.max(p, series[mod(m, histN)] || 0);
    return roundHalfEven(p);
  };
  return list.reduce((sum, p, k) => sum + (k ? gap : 0) + 26
    + textWidth(`${withCommas(maxOver(p.series))} ${chipLabel(p, maxOver(p.series))}`, `500 ${size}px ${F.tnum}`), 0);
}

// The chips line (B9), fitted once into textW px. Each set of parts tries
// 26 px, 24 px and 24 px with 8 px gaps before the smallest group joins
// "other", so a city keeps as many coloured groups as the minimum size
// allows. Returns the parts, size, gap, merged group ids and every width tried.
function fitChips(parts, F, textW) {
  const widthAt = (list, size, gap) => chipsWidth(list, size, gap, F);
  let list = parts;
  const merged = [];
  const tried = [];
  for (;;) {
    // The tight gap comes last: the dots still part the chips, and keeping a
    // group's chip (TTC in Vaughan) beats a grey trail.
    for (const [size, gap] of [[26, 20], [24, 20], [24, 8]]) {
      const w = widthAt(list, size, gap);
      tried.push({ n: list.length, size, gap, w });
      if (w <= textW) return { list, size, gap, merged, tried };
    }
    const real = list.filter((p) => !p.other);
    if (real.length <= 1) return { list, size: 24, gap: 8, merged, tried };
    const smallest = real.reduce((a, b) => (b.share < a.share || (b.share === a.share && list.indexOf(b) > list.indexOf(a)) ? b : a));
    let other = list.find((p) => p.other);
    if (!other) {
      other = { id: 'other', label: 'other', share: 0, other: true, color: CONFIG.COLORS.foreign, series: new Array(histN).fill(0) };
      list = list.concat([other]);
    }
    const sum = other.series.map((v, i) => v + (smallest.series[i] || 0));
    const joined = { ...other, share: other.share + smallest.share, series: sum };
    list = list.filter((p) => p !== smallest && p !== other).concat([joined]);
    merged.push(smallest.id);
  }
}

// buildSparkline over the window with circular smoothing, for either HUD. The
// legacy drawHUD reads spark.smooth by absolute minute, so it gets the
// periodic curve unrolled over the window.
function buildSparklineV4() {
  sparkSmooth = smoothCircular(hist, CONFIG.SPARK_SMOOTH_MIN);
  const mStart = meta.day_start / 60;
  const mEnd = meta.day_end / 60;
  const box = shorts ? shorts.spark : { x0: hud.sparkX0, x1: hud.sparkX1, y0: hud.sparkY0, y1: hud.sparkY1 };
  const { x0, x1, y0, y1 } = box;
  const headroom = shorts ? 6 : 10;
  let peak = 0;
  for (let m = Math.floor(mStart); m <= Math.ceil(mEnd); m++) peak = Math.max(peak, sparkSmooth[mod(m, histN)]);
  if (peak <= 0) peak = 1;
  const smooth = new Float64Array(Math.ceil(mEnd) + 2);
  for (let m = 0; m < smooth.length; m++) smooth[m] = sparkSmooth[mod(m, histN)];
  const xs = [], ys = [];
  if (shorts) {
    // One vertex per pixel column: a week is 10,080 minutes on 504 px.
    for (let x = x0; x <= x1; x++) {
      const m = mStart + ((x - x0) / (x1 - x0)) * (mEnd - mStart);
      xs.push(x);
      ys.push(y1 - (Math.min(sparkAt(m), peak) / peak) * (y1 - y0 - headroom));
    }
  } else {
    for (let m = Math.ceil(mStart); m <= Math.floor(mEnd); m++) {
      xs.push(x0 + ((m - mStart) / (mEnd - mStart)) * (x1 - x0));
      ys.push(y1 - ((smooth[m] || 0) / peak) * (y1 - y0 - headroom));
    }
  }
  spark = { x0, x1, y0, y1, mStart, mEnd, peak, xs, ys, smooth, headroom };
}

function sparkX(T) {
  return spark.x0 + clamp01((T / 60 - spark.mStart) / (spark.mEnd - spark.mStart)) * (spark.x1 - spark.x0);
}

function sparkY(T) {
  return spark.y1 - (Math.min(sparkAt(T / 60), spark.peak) / spark.peak) * (spark.y1 - spark.y0 - spark.headroom);
}

// The Shorts HUD's static sprites: the title scrim, the blurred panel
// backdrop (blurred once here; per frame it would cost more than the map),
// the sparkline gradient and the ?safe=1 overlay.
function buildShortsStatics() {
  const C = CONFIG.COLORS;
  const L = shorts;
  const st = {};
  const scrim = cssToRGB(C.scrim, bgRGB());
  if (CONFIG.TITLE_SCRIM > 0) {
    const s = CONFIG.TITLE_SCRIM;
    st.titleScrim = document.createElement('canvas');
    st.titleScrim.width = W;
    st.titleScrim.height = 460;
    const g = st.titleScrim.getContext('2d');
    const grad = g.createLinearGradient(0, 0, 0, 460);
    grad.addColorStop(0, rgba(scrim, s));
    grad.addColorStop(0.55, rgba(scrim, s));
    grad.addColorStop(0.80, rgba(scrim, 0.55 * s));
    grad.addColorStop(1, rgba(scrim, 0));
    g.fillStyle = grad;
    g.fillRect(0, 0, W, 460);
  }
  // Panel colour and alpha from the token (rgba(...) carries the theme's
  // 0.78), times PANEL_ALPHA, capped so the map always shows through a little.
  colorProbe.fillStyle = '#000000';
  colorProbe.fillStyle = C.panel;
  const probe = colorProbe.fillStyle;
  const am = /^rgba\(\d+,\s*\d+,\s*\d+,\s*([\d.]+)\)$/.exec(probe);
  const alpha = Math.min(0.95, (am ? Number(am[1]) : 1) * CONFIG.PANEL_ALPHA);
  const pad = 84;
  const p = L.panel;
  st.panelX = p.x0 - pad;
  st.panelY = p.y0 - pad;
  st.panel = document.createElement('canvas');
  st.panel.width = p.x1 - p.x0 + 2 * pad;
  st.panel.height = p.y1 - p.y0 + 2 * pad;
  const pg = st.panel.getContext('2d');
  pg.filter = 'blur(28px)';
  const panelRGB = cssToRGB(C.panel, [8, 13, 21]);
  // The peak label's outline (drawSparkShorts) is the panel colour at full alpha.
  st.panelOpaque = rgba(panelRGB, 1);
  pg.fillStyle = rgba(panelRGB, alpha);
  pg.beginPath();
  pg.roundRect(pad, pad, p.x1 - p.x0, p.y1 - p.y0, 24);
  pg.fill();
  pg.filter = 'none';
  const accent = cssToRGB(C.accent, [255, 224, 102]);
  const grad = ctx.createLinearGradient(0, spark.y0, 0, spark.y1);
  grad.addColorStop(0, rgba(accent, 0.5));
  grad.addColorStop(1, rgba(accent, 0.03));
  st.sparkFill = grad;
  if (showSafe) {
    st.safe = document.createElement('canvas');
    st.safe.width = W;
    st.safe.height = H;
    const g = st.safe.getContext('2d');
    g.fillStyle = 'rgba(255,0,0,0.18)';
    g.fillRect(0, 0, W, SAFE.y0);
    g.fillRect(0, SAFE.y1, W, H - SAFE.y1);
    g.fillRect(SAFE.x1, SAFE.y0, W - SAFE.x1, SAFE.y1 - SAFE.y0);
    g.strokeStyle = 'rgba(255,0,0,0.9)';
    g.lineWidth = 1;
    g.strokeRect(SAFE.x0 + 0.5, SAFE.y0 + 0.5, SAFE.x1 - SAFE.x0 - 1, SAFE.y1 - SAFE.y0 - 1);
  }
  shortsStatics = st;
}

// Boundary (B11): the mask A wrote, decoded once for the inside flags, and
// one overlay canvas that dims the outside and strokes the city line.
function buildBoundary() {
  boundaryMask = null;
  boundaryOverlay = null;
  const b = meta.boundary;
  if (!b) return;
  const mk = b.mask;
  if (mk && Array.isArray(mk.rle) && mk.nx > 0 && mk.ny > 0) {
    // Rows run south to north from y0, cells west to east from x0; each row
    // starts with an outside run and its runs add up to nx.
    const cells = new Uint8Array(mk.nx * mk.ny);
    let k = 0, ok = true;
    for (let row = 0; row < mk.ny && ok; row++) {
      let x = 0, inside = false;
      while (x < mk.nx) {
        if (k >= mk.rle.length) { ok = false; break; }
        const run = mk.rle[k++];
        if (inside) cells.fill(1, row * mk.nx + x, row * mk.nx + Math.min(mk.nx, x + run));
        x += run;
        inside = !inside;
      }
      if (x !== mk.nx) ok = false;
    }
    if (!ok || k !== mk.rle.length) console.error('meta.boundary.mask: the run lengths do not add up to nx in every row');
    boundaryMask = { cell: mk.cell_km, x0: mk.x0, y0: mk.y0, nx: mk.nx, ny: mk.ny, cells };
  } else {
    console.warn('meta.boundary has no mask; every vehicle counts as inside');
  }
  const rings = (b.rings || []).concat(b.holes || []);
  if (!rings.length) return;
  const C = CONFIG.COLORS;
  const path = new Path2D();
  for (const ring of rings) {
    if (ring.length < 6) continue;
    path.moveTo(OX + ring[0] * SCALE, OY - ring[1] * SCALE);
    for (let i = 2; i < ring.length; i += 2) path.lineTo(OX + ring[i] * SCALE, OY - ring[i + 1] * SCALE);
    path.closePath();
  }
  if (cam) {
    // Drawn per frame under the camera (drawBoundaryCam). The outside is the
    // whole headroom with the rings cut out by the even-odd rule, which is
    // what the overlay's fill and punch leave.
    cam.rings = path;
    if (CONFIG.OUTSIDE_DIM > 0) {
      cam.outside = new Path2D();
      cam.outside.rect(cam.ax, cam.ay, cam.w / (cam.k * cam.q), cam.h / (cam.k * cam.q));
      cam.outside.addPath(path);
      cam.dim = rgba(cssToRGB(C.outside, bgRGB()), clamp01(CONFIG.OUTSIDE_DIM));
    }
    return;
  }
  const c = document.createElement('canvas');
  c.width = W;
  c.height = H;
  const g = c.getContext('2d');
  if (CONFIG.OUTSIDE_DIM > 0) {
    g.fillStyle = rgba(cssToRGB(C.outside, bgRGB()), clamp01(CONFIG.OUTSIDE_DIM));
    g.fillRect(0, 0, W, H);
    g.globalCompositeOperation = 'destination-out';
    // destination-out keeps dst * (1 - src alpha); the dim's own alpha would
    // leave the inside a(1-a) dimmed, so the punch must be opaque.
    g.fillStyle = '#000';
    g.fill(path, 'evenodd');
    g.globalCompositeOperation = 'source-over';
  }
  if (CONFIG.CITY_LINE_W > 0 && CONFIG.CITY_LINE_ALPHA > 0) {
    g.strokeStyle = C.cityLine;
    g.globalAlpha = clamp01(CONFIG.CITY_LINE_ALPHA);
    g.lineWidth = CONFIG.CITY_LINE_W;
    g.lineJoin = 'round';
    g.lineCap = 'round';
    g.stroke(path);
    g.globalAlpha = 1;
  }
  boundaryOverlay = c;
}

// 1 when the km point lies in an inside cell of the mask; with no boundary
// every vehicle is inside.
function insideKm(x, y) {
  const m = boundaryMask;
  if (!m) return 1;
  const ix = Math.floor((x - m.x0) / m.cell);
  const iy = Math.floor((y - m.y0) / m.cell);
  if (ix < 0 || iy < 0 || ix >= m.nx || iy >= m.ny) return 0;
  return m.cells[iy * m.nx + ix];
}

// The card text (B10), placeholders filled from meta and the variant.
function fillCardTemplate(tpl) {
  const plural = modesInWindow();
  const values = {
    place: meta.place || '',
    modes_singular: joinWords(plural.map((m) => m.singular)),
    modes_plural: joinWords(plural.map((m) => m.label)),
    peak_time: variantV.peak ? clockText(variantV.peak.time) : '',
    peak_count: variantV.peak ? withCommas(variantV.peak.count) : '',
    trips: withCommas(meta.trips_total || trips.length),
    month: (meta.timeline && meta.timeline.month_label) || '',
  };
  return String(tpl).replace(/\{([^{}]*)\}/g, (all, key) => {
    if (Object.hasOwn(values, key)) return values[key];
    console.error(`card template "${tpl}": unknown placeholder {${key}}`);
    return all;
  });
}

// Card layout (B10), for the active variant and CARD_LINES.
function buildCard() {
  cardLayout = null;
  const card = meta.card;
  const tpls = card && card.templates && card.templates[CONFIG.VARIANT];
  if (!card || !Array.isArray(tpls) || !tpls.length) {
    if (CONFIG.CARD) console.warn(`no card templates for variant ${CONFIG.VARIANT}; the card is off`);
    return;
  }
  const F = fontsV4();
  const pick = tpls[Math.min(tpls.length - 1, Math.max(0, Math.round(CONFIG.CARD_LINES)))];
  const line0 = fillCardTemplate(pick[0] || '');
  const line1 = fillCardTemplate(pick[1] || '');
  const title = card.title || meta.title || '';
  const titleFont = (s) => `800 ${s}px ${F.mont}`;
  const fits = (lines, s) => lines.every((l) => textWidth(l, titleFont(s), 0.04 * s) <= 796);
  const fitSize = (lines) => {
    for (let s = Math.floor(CONFIG.CARD_TITLE_MAX / 2) * 2; s >= 72; s -= 2) if (fits(lines, s)) return s;
    return null;
  };
  let titleLines = [title];
  let S = fitSize(titleLines);
  if ((S === null || S < 100) && title.includes(' ')) {
    const words = title.split(' ');
    let best = null;
    for (let k = 1; k < words.length; k++) {
      const pair = [words.slice(0, k).join(' '), words.slice(k).join(' ')];
      const longer = Math.max(...pair.map((l) => textWidth(l, titleFont(100), 4)));
      if (!best || longer < best.longer) best = { pair, longer };
    }
    const s2 = fitSize(best.pair);
    if (s2 !== null && (S === null || s2 >= S)) {
      titleLines = best.pair;
      S = s2;
    }
  }
  if (S === null) S = 72;
  const items = [];
  let y = 0;
  titleLines.forEach((text, k) => {
    y = 0.80 * S + k * 0.98 * S;
    items.push({ name: k ? 'card_title2' : 'card_title', text, y, font: titleFont(S), size: S, spacing: 0.04 * S, kind: 'title' });
  });
  const ruleTop = y + 30;
  // A smaller line keeps the place whole before a split place is accepted.
  const place = meta.place || '';
  let l0size = 40, l0 = null;
  for (const [size, keep] of [[44, place], [40, place], [44, ''], [40, '']]) {
    l0 = wrapText(line0, `500 ${size}px ${F.inter}`, 796, 2, 'balanced', keep);
    if (l0) { l0size = size; break; }
  }
  if (!l0) l0 = [line0];
  y = ruleTop + 64;
  l0.forEach((text, k) => {
    if (k) y += 54;
    items.push({ name: k ? 'card_line0b' : 'card_line0', text, y, font: `500 ${l0size}px ${F.inter}`, size: l0size, kind: 'line0' });
  });
  let l1size = 32;
  while (l1size > 30 && textWidth(line1, `400 ${l1size}px ${F.inter}`) > 796) l1size--;
  const l1font = `400 ${l1size}px ${F.inter}`;
  const l1 = line1 ? wrapText(line1, l1font, 796, 2, 'balanced', place) || wrapText(line1, l1font, 796, 2, 'balanced')
    || [line1] : [];
  l1.forEach((text, k) => {
    y += k ? 40 : 52;
    items.push({ name: k ? 'card_line1b' : 'card_line1', text, y, font: `400 ${l1size}px ${F.inter}`, size: l1size, kind: 'line1' });
  });
  const h = y + 12;
  let B = Math.round(CONFIG.CARD_CENTER_Y - h / 2);
  B = Math.max(400, Math.min(1100 - h, B));
  for (const it of items) it.y += B;
  // The band behind the text: full strength between 48 px feathers.
  const top = B - 60, bandH = h + 120;
  const band = document.createElement('canvas');
  band.width = W;
  band.height = Math.max(1, Math.ceil(bandH));
  const bg = band.getContext('2d');
  const grad = bg.createLinearGradient(0, 0, 0, bandH);
  const scrim = cssToRGB(CONFIG.COLORS.scrim, bgRGB());
  const f = Math.min(0.5, 48 / bandH);
  grad.addColorStop(0, rgba(scrim, 0));
  grad.addColorStop(f, rgba(scrim, 1));
  grad.addColorStop(1 - f, rgba(scrim, 1));
  grad.addColorStop(1, rgba(scrim, 0));
  bg.fillStyle = grad;
  bg.fillRect(0, 0, W, bandH);
  cardLayout = { B, h, items, ruleY: B + ruleTop, band, bandY: top, lines: [line0, line1], titleSize: S };
}

// Card alpha at frame i (B10): held, faded out, and for a wrap loop faded
// back in so the last frame carries the full card like frame 0.
function cardAlphaV4(i) {
  if (!isV4 || !CONFIG.CARD || !cardOn || !cardLayout) return 0;
  const N = totalFrames;
  const aOut = i < CONFIG.CARD_HOLD ? 1 : 1 - smoothstep(Math.min(1, (i - CONFIG.CARD_HOLD) / CONFIG.CARD_FADE_OUT));
  const aIn = CONFIG.LOOP === 'wrap' ? smoothstep(clamp01((i - (N - 1 - CONFIG.CARD_FADE_IN)) / CONFIG.CARD_FADE_IN)) : 0;
  return Math.max(aOut, aIn);
}

// Measures a text, records its box for hudBoxes() and draws it unless the
// HUD is in notext mode.
function hudText(name, text, x, y, font, color, size, alpha, align = 'left', spacing = 0) {
  ctx.font = font;
  ctx.letterSpacing = `${spacing}px`;
  ctx.textAlign = align;
  const m = ctx.measureText(text);
  const left = align === 'right' ? x - m.width : align === 'center' ? x - m.width / 2 : x;
  lastBoxes.push({
    name, text,
    x0: Math.min(left, x - m.actualBoundingBoxLeft),
    y0: y - m.actualBoundingBoxAscent,
    x1: Math.max(left + m.width, x + m.actualBoundingBoxRight),
    y1: y + m.actualBoundingBoxDescent,
    color, size, font, alpha,
  });
  if (hudMode === 'full' && alpha > 0) {
    ctx.globalAlpha = alpha;
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
  }
  ctx.letterSpacing = '0px';
}

// Hour clock for the week ("8 am"), else the time floored to CLOCK_ROUND
// minutes; 0 floors to 5 minutes while the clock runs faster than 2 minutes a
// frame, so the night digits do not flicker.
function clockTextV4(T) {
  const r = CONFIG.CLOCK_ROUND > 0 ? CONFIG.CLOCK_ROUND : rateAt(T) > 2 ? 5 : 1;
  const step = 60 * r;
  const t = Math.floor(T / step) * step;
  if (r >= 60) {
    const s = mod(t, 86400);
    let h = Math.floor(s / 3600);
    const ap = h < 12 ? 'am' : 'pm';
    h %= 12;
    return `${h === 0 ? 12 : h} ${ap}`;
  }
  return clockText(t);
}

// Whole numbers that add up to n, by largest remainder: the chips then add
// up to the count line even though each is a rounded mean.
function largestRemainder(values, n) {
  const v = values.map((x) => Math.max(0, x));
  const out = v.map(Math.floor);
  let left = n - out.reduce((a, b) => a + b, 0);
  const order = v.map((_, i) => i).sort((a, b) => (v[b] - out[b]) - (v[a] - out[a]) || a - b);
  while (left > 0 && order.length) {
    for (const i of order) {
      if (left <= 0) break;
      out[i]++;
      left--;
    }
  }
  while (left < 0) {
    let moved = false;
    for (let k = order.length - 1; k >= 0 && left < 0; k--) {
      if (out[order[k]] > 0) {
        out[order[k]]--;
        left++;
        moved = true;
      }
    }
    if (!moved) break;
  }
  return out;
}

// The numbers the HUD shows at T: the count line and each chip.
function countAtV4(T) {
  if (!isV4) return null;
  const total = roundHalfEven(histRaw(T / 60));
  const byGroup = {};
  if (shorts && shorts.chips) {
    const parts = shorts.chips.parts;
    const vals = largestRemainder(parts.map((p) => interpCircular(p.series, T / 60)), total);
    parts.forEach((p, k) => { byGroup[p.id] = vals[k]; });
  } else if (groups) {
    const byG = meta.hist_by_group || {};
    const vals = largestRemainder(groups.map((g) => (byG[g.id] ? interpCircular(byG[g.id], T / 60) : 0)), total);
    groups.forEach((g, k) => { byGroup[g.id] = vals[k]; });
  }
  return { total, byGroup };
}

// The Shorts HUD at alpha a (1 - card alpha).
function drawHudShorts(T, a) {
  const C = CONFIG.COLORS;
  const L = shorts;
  const st = shortsStatics;
  const text = hudMode === 'full';
  ctx.globalCompositeOperation = 'source-over';
  ctx.textBaseline = 'alphabetic';
  if (a > 0 && st.titleScrim) {
    ctx.globalAlpha = a;
    ctx.drawImage(st.titleScrim, 0, 0);
  }
  hudText('title', L.title.text, 72, 316, L.title.font, C.title, L.title.size, a, 'left', L.title.spacing);
  hudText('subtitle', L.subtitle.text, 72, 370, L.subtitle.font, C.subtitle, L.subtitle.size, a);
  if (a > 0) {
    ctx.globalAlpha = a;
    ctx.drawImage(st.panel, st.panelX, st.panelY);
  }
  const x = L.textX;
  if (L.week) {
    const day = DAY_NAMES[mod(Math.floor(T / 86400), 7)];
    hudText('weekday', day, x, L.weekday.y, L.weekday.font, C.clock, L.weekday.size, a);
  }
  hudText('clock', clockTextV4(T), x, L.clockY, L.clock.font, C.clock, L.clock.size, a);
  const counts = countAtV4(T);
  const n = counts.total;
  const noun = countNoun(L, n);
  if (L.split) {
    hudText('count', `${withCommas(n)} ${noun}`, x, L.countY, L.count.font, C.accent, L.count.size, a);
    hudText('count2', `in ${meta.place || ''}`, x, L.count2Y, L.count.font, C.accent, L.count.size, a);
  } else {
    hudText('count', `${withCommas(n)} ${noun} in ${meta.place || ''}`, x, L.countY, L.count.font, C.accent, L.count.size, a);
  }
  if (L.chips) {
    // One box for the whole line: every part shares the font and colour.
    let cx = x;
    const y = L.chips.y;
    const start = lastBoxes.length;
    L.chips.parts.forEach((p, k) => {
      if (k) cx += L.chips.gap;
      if (text && a > 0) {
        ctx.globalAlpha = a;
        ctx.fillStyle = p.color;
        ctx.beginPath();
        ctx.arc(cx + 9, y - 9, 9, 0, Math.PI * 2);
        ctx.fill();
      }
      const v = counts.byGroup[p.id] || 0;
      hudText('chips', `${withCommas(v)} ${chipLabel(p, v)}`, cx + 26, y, L.chips.font, C.breakdown, L.chips.size, a);
      cx = lastBoxes[lastBoxes.length - 1].x1;
    });
    const parts = lastBoxes.splice(start);
    if (parts.length) {
      lastBoxes.push({
        ...parts[0], text: parts.map((b) => b.text).join('  '), x0: x,
        y0: Math.min(...parts.map((b) => b.y0), y - 18), x1: parts[parts.length - 1].x1, y1: Math.max(...parts.map((b) => b.y1)),
      });
    }
  }
  drawSparkShorts(T, a, text);
  drawAxisShorts(T, a);
  L.credit.forEach((line, k) => hudText(k ? 'credit2' : 'credit', line, x, k ? 1476 : 1450, L.creditFont, C.credit, 22, a));
  ctx.globalAlpha = 1;
  ctx.textAlign = 'left';
}

function drawSparkShorts(T, a, text) {
  const C = CONFIG.COLORS;
  const L = shorts;
  const sp = spark;
  const xCur = sparkX(T);
  if (text && a > 0) {
    ctx.globalAlpha = a;
    if (L.week && CONFIG.WEEKEND_BAND) {
      const xa = sparkX(5 * 86400), xb = sparkX(7 * 86400);
      if (xb > xa) {
        ctx.globalAlpha = a * 0.07;
        ctx.fillStyle = C.accent;
        ctx.fillRect(xa, sp.y0, xb - xa, sp.y1 - sp.y0);
        ctx.globalAlpha = a;
      }
    }
    const yCur = sparkY(T);
    const last = Math.min(sp.xs.length - 1, Math.floor(xCur - sp.x0));
    ctx.beginPath();
    ctx.moveTo(sp.x0, sp.y1);
    for (let j = 0; j <= last; j++) ctx.lineTo(sp.xs[j], sp.ys[j]);
    ctx.lineTo(xCur, yCur);
    ctx.lineTo(xCur, sp.y1);
    ctx.closePath();
    ctx.fillStyle = shortsStatics.sparkFill;
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(sp.xs[0], sp.ys[0]);
    for (let j = 1; j <= last; j++) ctx.lineTo(sp.xs[j], sp.ys[j]);
    ctx.lineTo(xCur, yCur);
    ctx.strokeStyle = C.accent;
    ctx.lineWidth = 3;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(sp.x0, sp.y1 + 0.5);
    ctx.lineTo(sp.x1, sp.y1 + 0.5);
    ctx.strokeStyle = C.floor;
    ctx.lineWidth = 1;
    ctx.stroke();
  }
  if (CONFIG.PEAK_MARKER && variantV.peak) {
    let pt = variantV.peak.time;
    while (pt < meta.day_start) pt += periodS;
    if (T >= pt) {
      const px = sparkX(pt), py = sparkY(pt);
      if (text && a > 0) {
        ctx.globalAlpha = a;
        ctx.fillStyle = C.accent;
        ctx.beginPath();
        ctx.arc(px, py, 5, 0, Math.PI * 2);
        ctx.fill();
      }
      const { label, w } = L.peakLabel;
      const right = px + 11 + w <= L.rightX;
      const by = Math.min(1386, Math.max(1353, py + 9));
      const lx = right ? px + 11 : px - 11;
      if (text && a > 0) {
        // The label sits on the curve it names, in the same accent; an outline
        // in the panel colour keeps the two apart.
        ctx.globalAlpha = a;
        ctx.font = L.peakFont;
        ctx.textAlign = right ? 'left' : 'right';
        ctx.lineJoin = 'round';
        ctx.lineWidth = 6;
        ctx.strokeStyle = shortsStatics.panelOpaque;
        ctx.strokeText(label, lx, by);
      }
      hudText('peak', label, lx, by, L.peakFont, C.accent, 26, a, right ? 'left' : 'right');
    }
  }
}

// Axis under the sparkline (B9): the window's ends and a midnight tick for
// the day, hourly ticks for a shorter window, day letters for the week.
function drawAxisShorts(T, a) {
  const C = CONFIG.COLORS;
  const L = shorts;
  const sp = spark;
  const W0 = meta.day_start, W1 = meta.day_end;
  const ticks = [];
  const tick = (t) => { if (t > W0 && t < W1) ticks.push(sparkX(t)); };
  if (L.week) {
    for (let d = Math.ceil(W0 / 86400); d * 86400 < W1; d++) tick(d * 86400);
    const today = Math.floor(T / 86400);
    for (let d = Math.floor(W0 / 86400); d * 86400 < W1; d++) {
      const xa = sparkX(Math.max(W0, d * 86400)), xb = sparkX(Math.min(W1, (d + 1) * 86400));
      if (xb - xa < 30) continue;
      const cur = d === today;
      hudText('axis', DAY_LETTERS[mod(d, 7)], (xa + xb) / 2, L.axisY, cur ? L.axisBold : L.axisFont, cur ? C.accent : C.axis, 26, a, 'center');
    }
  } else {
    hudText('axis', clockText(W0), sp.x0, L.axisY, L.axisFont, C.axis, 26, a, 'left');
    const leftEnd = lastBoxes[lastBoxes.length - 1].x1;
    hudText('axis', clockText(W1), sp.x1, L.axisY, L.axisFont, C.axis, 26, a, 'right');
    const rightStart = lastBoxes[lastBoxes.length - 1].x0;
    if (W1 - W0 >= periodS) {
      for (let d = Math.ceil(W0 / 86400); d * 86400 < W1; d++) {
        const t = d * 86400;
        if (t <= W0) continue;
        tick(t);
        // The label also needs room between the end labels: with the window
        // starting at the morning peak, midnight sits about 170 px from the
        // right end, where "8:04 am" already is.
        const xm = sparkX(t);
        const half = textWidth('midnight', L.axisFont) / 2;
        if (xm - sp.x0 >= 120 && sp.x1 - xm >= 120 && xm - half - 12 >= leftEnd && xm + half + 12 <= rightStart) {
          hudText('axis', 'midnight', xm, L.axisY, L.axisFont, C.axis, 26, a, 'center');
        }
      }
    } else {
      for (let t = Math.ceil(W0 / 3600) * 3600; t < W1; t += 3600) tick(t);
    }
  }
  if (hudMode === 'full' && a > 0 && ticks.length) {
    ctx.globalAlpha = a;
    ctx.strokeStyle = C.floor;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const x of ticks) {
      const xr = Math.round(x) + 0.5;
      ctx.moveTo(xr, sp.y1);
      ctx.lineTo(xr, sp.y1 + 8);
    }
    ctx.stroke();
  }
}

// The card at alpha a (B10): a light scrim over the whole frame so the map
// stays the hook, a feathered band behind the text, then the text.
function drawCard(a) {
  if (!cardLayout || a <= 0 || hudMode === 'none') return;
  const C = CONFIG.COLORS;
  const cl = cardLayout;
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  ctx.fillStyle = rgba(cssToRGB(C.scrim, bgRGB()), CONFIG.CARD_SCRIM * a);
  ctx.fillRect(0, 0, W, H);
  ctx.globalAlpha = clamp01(CONFIG.CARD_BAND * a);
  ctx.drawImage(cl.band, 0, cl.bandY);
  ctx.textBaseline = 'alphabetic';
  for (const it of cl.items) {
    const alpha = it.kind === 'line1' ? 0.85 * a : a;
    hudText(it.name, it.text, 72, it.y, it.font, C.title, it.size, alpha, 'left', it.spacing || 0);
  }
  if (hudMode === 'full') {
    ctx.globalAlpha = a;
    ctx.fillStyle = C.accent;
    ctx.fillRect(72, cl.ruleY, 96, 6);
  }
  ctx.globalAlpha = 1;
}

// One frame of the map at T: base, trails, dots, boundary overlay. The
// per-trip body is renderAt's, run over the occurrences that can be on screen
// (B7), with each vehicle's inside flag recorded for lastVehicles (B11).
// cm is the camera (B18) or null: the trips are sampled in base px as always
// and only drawing goes through it, so the inside flags read the mask at the
// vehicle's own km position while lastVehicles reports where it is on screen.
function drawMapV4(T, cm = null) {
  if (!baseCanvas) throw new Error('busmap not ready');
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  if (cm) drawBaseCam(cm);
  else ctx.drawImage(baseCanvas, 0, 0);

  const bounded = !byRoute && CONFIG.TRAIL_BLEND === 'bounded' && CONFIG.TRAIL_MODE !== 'sprite';
  const ts = bounded ? 1 : CONFIG.TRAIL_SCALE;
  const layer = ts === 1 || byRoute ? ctx : trailCtx;
  if (ts !== 1 && !byRoute) {
    layer.globalCompositeOperation = 'source-over';
    layer.clearRect(0, 0, trailCanvas.width, trailCanvas.height);
  }
  layer.globalCompositeOperation = 'lighter';

  const buses = [];
  const horizon = T - trailWindow;
  const sprite = CONFIG.TRAIL_MODE === 'sprite';
  const step = sprite ? CONFIG.TRAIL_STEP_S : trailWindow / CONFIG.TRAIL_BANDS;
  const count = sprite ? trailSteps + 1 : CONFIG.TRAIL_BANDS + 1;
  if (byRoute) {
    colorPaths.fill(null);
  } else if (!sprite) {
    for (let m = 0; m < modes.length; m++) {
      for (let b = 0; b < CONFIG.TRAIL_BANDS; b++) bandPaths[m][b] = new Path2D();
    }
  }
  runningByMode.fill(0);
  if (runningByGroup) runningByGroup.fill(0);
  let running = 0;
  vehN = 0;
  for (let j = lowerBound(occT0, occN, T - trailWindow - maxDur); j < occN && occT0[j] <= T; j++) {
    if (occT1[j] < horizon) continue;
    const n = occTrip[j];
    const trip = trips[n];
    const Tl = T - occOff[j];
    const range = sampleTrail(trip, Tl, ts, step, count);
    if (range < 0) continue;
    const first = Math.floor(range / 1024);
    const last = range % 1024;
    const m = tripMode[n];
    if (first === 0 && Tl <= tripT1[n]) {
      running++;
      runningByMode[m]++;
      if (runningByGroup) runningByGroup[tripGroup[n]]++;
      const sx = sampleX[0] / ts, sy = sampleY[0] / ts;
      const qx = cm ? cm.z * sx + cm.e : sx, qy = cm ? cm.z * sy + cm.f : sy;
      buses.push(qx, qy, byRoute ? tripColor[n] : m);
      if (3 * vehN + 3 > vehBuf.length) {
        const grown = new Float32Array(vehBuf.length * 2);
        grown.set(vehBuf);
        vehBuf = grown;
      }
      vehBuf[3 * vehN] = qx;
      vehBuf[3 * vehN + 1] = qy;
      vehBuf[3 * vehN + 2] = insideKm((sx - OX) / SCALE, (OY - sy) / SCALE);
      vehN++;
    }
    if (byRoute) addRibbonColor(trip, tripColor[n], first, last);
    else if (sprite && cm) stampTrailSpritesCam(layer, trailSprites[m], first, last, ts, cm);
    else if (sprite) stampTrailSprites(layer, trailSprites[m], first, last);
    else addRibbon(trip, bandPaths[m], first, last);
  }
  if (byRoute) {
    colorLayer.globalCompositeOperation = 'source-over';
    colorLayer.globalAlpha = 1;
    colorLayer.clearRect(0, 0, colorLayer.canvas.width, colorLayer.canvas.height);
    if (cm) strokeColorRibbonsCam(colorLayer, ts, cm);
    else strokeColorRibbons(colorLayer, ts);
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = CONFIG.TRAIL_LAYER_ALPHA;
    ctx.drawImage(colorLayer.canvas, 0, 0, W, H);
    ctx.globalAlpha = 1;
  } else if (!sprite && CONFIG.TRAIL_BLEND === 'bounded') {
    for (const m of layerOrder) {
      const ml = modeLayer(m);
      ml.globalCompositeOperation = 'source-over';
      ml.clearRect(0, 0, W, H);
      if (cm) strokeRibbonsCam(ml, 1, m, cm);
      else strokeRibbons(ml, 1, m);
      ctx.globalCompositeOperation = m === onTopMode ? 'source-over' : 'lighter';
      ctx.globalAlpha = CONFIG.TRAIL_LAYER_ALPHA;
      ctx.drawImage(ml.canvas, 0, 0);
      ctx.globalAlpha = 1;
    }
  } else {
    if (!sprite && cm) strokeRibbonsCam(layer, ts, undefined, cm);
    else if (!sprite) strokeRibbons(layer, ts);
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
  ctx.globalCompositeOperation = 'source-over';
  if (cm) drawBoundaryCam(cm);
  else if (boundaryOverlay) ctx.drawImage(boundaryOverlay, 0, 0);
  return running;
}

// The whole frame at T with the HUD at hudAlpha and the card at cardA, the
// map under the camera at phase u (B18).
function drawV4(T, hudAlpha, cardA, u) {
  lastBoxes = [];
  drawMapV4(T, cameraAt(u));
  const n = roundHalfEven(histRaw(T / 60));
  if (hudMode !== 'none') {
    if (shorts) drawHudShorts(T, hudAlpha);
    else drawHUD(T, n);
    drawCard(cardA);
  }
  ctx.globalAlpha = 1;
  ctx.globalCompositeOperation = 'source-over';
  return n;
}

function drawSafe() {
  if (shortsStatics && shortsStatics.safe) {
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    ctx.drawImage(shortsStatics.safe, 0, 0);
  }
}

// A still at T: full HUD, no card (B10). Returns the count line's number.
// The camera is where the video has it at T, unless setCamera pinned a phase.
function renderAtV4(T) {
  const n = drawV4(T, 1, 0, camPin !== null ? camPin : (cam ? progressAt(T) : 0));
  drawSafe();
  return n;
}

// Frame i of the video, a pure function of i. i may be totalFrames, the
// virtual frame after the last, which equals frame 0 for both loops.
function renderFrameV4(i) {
  const N = totalFrames;
  const idx = Math.min(Math.max(Math.floor(i), 0), N);
  const T = frameTimeV4(idx);
  let s = 0;
  if (CONFIG.LOOP === 'xfade') {
    // The cross-fade to frame 0 reaches 1 at the virtual frame N, so the
    // step from N - 1 back to 0 is one ordinary fade step.
    s = smoothstep(clamp01((idx - (N - CONFIG.CARD_FADE_IN)) / CONFIG.CARD_FADE_IN));
    if (s > 0 && !loopSnapshot) {
      const a0 = cardAlphaV4(0);
      drawV4(frameTimeV4(0), 1 - a0, a0, 0);
      loopSnapshot = document.createElement('canvas');
      loopSnapshot.width = W;
      loopSnapshot.height = H;
      loopSnapshot.getContext('2d').drawImage(canvas, 0, 0);
    }
  }
  const a = cardAlphaV4(idx);
  drawV4(T, 1 - a, a, idx / N);
  if (s > 0) {
    ctx.globalAlpha = s;
    ctx.drawImage(loopSnapshot, 0, 0);
    ctx.globalAlpha = 1;
  }
  drawSafe();
  return T;
}

function setHudV4(mode) {
  if (!['full', 'notext', 'none'].includes(mode)) throw new Error(`setHud: want full, notext or none, got ${mode}`);
  hudMode = mode;
  loopSnapshot = null;
}

function setCardV4(on) {
  cardOn = Boolean(on);
  loopSnapshot = null;
}

// Times the stills are taken at (B15), each inside the window.
function stillTimesV4() {
  if (!isV4) return null;
  const W0 = meta.day_start, W1 = meta.day_end, P = periodS;
  const at = (T) => {
    let t = T;
    while (t < W0) t += P;
    return t;
  };
  if (meta.timeline && meta.timeline.kind === 'week') {
    let fri = 4 * 1440 + 870;
    for (let m = fri; m <= 4 * 1440 + 1170; m++) if (hist[mod(m, histN)] > hist[mod(fri, histN)]) fri = m;
    return { am: at(meta.am_peak.time), noon: at(2 * 86400 + 45000), pm: at(fri * 60), sat: at(5 * 86400 + 46800), sun: at(6 * 86400 + 82800) };
  }
  if (W1 - W0 < P) {
    return { am: at(variantV.peak ? variantV.peak.time : W0), early: at(24300), mid: at(28800), late: at(33300) };
  }
  return { am: at(meta.am_peak.time), noon: at(45000), pm: at(meta.pm_peak.time), late: at(81000), night: at(9000) };
}

// Init-time asserts (B9): every HUD and card box inside the safe zone and at
// or above its minimum size, measured on the widest text the window can
// show. A failure is a console.error, which fails a render.
function checkLayoutV4() {
  if (!shorts) return;
  const keep = { boxes: lastBoxes, hudMode };
  hudMode = 'notext';
  lastBoxes = [];
  // Drawn into a scratch state: notext draws no text, and the canvas is
  // overwritten by the first real frame anyway.
  let T = variantV.peak ? variantV.peak.time : meta.day_start;
  while (T < meta.day_start) T += periodS;
  drawHudShorts(Math.min(meta.day_end, T), 1);
  drawCard(1);
  const L = shorts;
  const probe = [];
  // The widest numbers the panel can show: the peak count and each chip at its maximum.
  if (L.chips) {
    const w = chipsWidth(L.chips.parts, L.chips.size, L.chips.gap, L.F);
    probe.push({ name: 'chips', x0: L.textX, x1: L.textX + w, y0: L.chips.y - 18, y1: L.chips.y, size: L.chips.size });
  }
  const problems = [];
  for (const b of lastBoxes.concat(probe)) {
    if (b.x0 < SAFE.x0 - 0.5 || b.x1 > SAFE.x1 + 0.5 || b.y0 < SAFE.y0 - 0.5 || b.y1 > SAFE.y1 + 0.5) {
      problems.push(`${b.name} "${b.text || ''}" at x ${b.x0.toFixed(0)}..${b.x1.toFixed(0)}, y ${b.y0.toFixed(0)}..${b.y1.toFixed(0)} leaves the safe zone`);
    }
    const min = MIN_SIZE[b.name];
    if (min && b.size < min) problems.push(`${b.name} is ${b.size} px, under its ${min} px minimum`);
  }
  for (const p of problems) console.error(`layout: ${p}`);
  if (Number.isFinite(L.peakCount) && variantV.peak) {
    const m = variantV.peak.time / 60;
    const got = roundHalfEven(histRaw(m));
    if (got !== variantV.peak.count) console.warn(`variant peak count ${variantV.peak.count} but hist reads ${got} at ${clockText(variantV.peak.time)}`);
  }
  lastBoxes = keep.boxes;
  hudMode = keep.hudMode;
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
  renderFrame: (i) => (isV4 ? renderFrameV4(i) : renderFrame(i)),
  renderAt: (T) => (isV4 ? renderAtV4(T) : renderAt(T)),
  config: CONFIG,
  canvas,
  get meta() { return meta; },
  get hist() { return hist; },
  get modes() { return modes; },
  get runningByMode() { return runningByMode; },
  get runningByGroup() { return runningByGroup; },
  get groups() { return groups; },
  get colors() { return colors; },
  get hud() { return hud; },
  clockText,
  frameTime: (i) => frameTime(i),
  progressAt: (T) => progressAt(T),
  timeAtProgress: (u) => timeAtProgress(u),
  // Shorts additions (spec 2.11); all of them are inert for a legacy file.
  get variant() { return isV4 ? CONFIG.VARIANT : ''; },
  get window() { return meta ? { start: meta.day_start, end: meta.day_end } : null; },
  safe: { x0: SAFE.x0, y0: SAFE.y0, x1: SAFE.x1, y1: SAFE.y1 },
  hudBoxes: () => lastBoxes.map((b) => ({ ...b })),
  get lastVehicles() { return vehBuf.slice(0, 3 * vehN); },
  setHud: (mode) => setHudV4(mode),
  setCard: (on) => setCardV4(on),
  cardAlpha: (i) => cardAlphaV4(i),
  stillTimes: () => stillTimesV4(),
  get brandMap() { return brandMap; },
  countAt: (T) => countAtV4(T),
  // The chips line's fit (B9): the parts shown, their size, every width
  // tried on the way, and the groups folded into "other", which B6 then
  // draws in the foreign colour.
  get chips() {
    const c = shorts && shorts.chips;
    return c ? { ids: c.parts.map((p) => p.id), size: c.size, gap: c.gap, merged: c.merged.slice(), width: shorts.textW,
      tried: c.tried.map((t) => ({ ...t })) } : null;
  },
  // The camera (B18), or null when it is off: path, zoom amplitude, drift in
  // px, the speed cap's factor, the city line's factor with the line's fitted
  // bbox and its keep rect (null when the frame crops the line), the fastest
  // on-screen motion in frame widths a second, the pivot and the cached base.
  get camera() {
    return cam ? { path: cam.path, zoom: cam.Z, drift: cam.R, scale: cam.scale, amp: cam.amp, bound: cam.bound,
      box: cam.box && cam.box.slice(), keep: cam.keep && cam.keep.slice(), peak_speed: cam.peak,
      pivot: CAMERA_PIVOT.slice(), base: { w: cam.w, h: cam.h, k: cam.k * cam.q, x0: cam.ax, y0: cam.ay } } : null;
  },
  // screen = zoom * base + (e, f) at phase u (frame i of N is u = i / N).
  cameraAt: (u) => {
    const m = cameraAt(Number(u));
    return m ? { zoom: m.z, e: m.e, f: m.f } : { zoom: 1, e: 0, f: 0 };
  },
  // Pins the phase renderAt draws the camera at; null follows T again.
  setCamera: (u) => { camPin = u === null || u === undefined ? null : Number(u); },
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
    if (t != null) busmap.renderAt(t);
    else if (f != null && Number.isFinite(Number(f))) busmap.renderFrame(Number(f));
    else busmap.renderFrame(0);
  });
} else {
  ready.then(() => {
    const scrub = document.getElementById('scrub');
    const playBtn = document.getElementById('play');
    const timeEl = document.getElementById('time');
    const playSeconds = CONFIG.DURATION_FRAMES / CONFIG.FPS;
    // The controls follow the city's accent; the tab is named after it.
    if (meta.theme && meta.theme.accent) {
      scrub.style.accentColor = CONFIG.COLORS.accent;
      timeEl.style.color = CONFIG.COLORS.accent;
    }
    if (meta.title) document.title = meta.title.toLowerCase().replace(/(^|\s)\S/g, (c) => c.toUpperCase());
    let T = meta.day_start;
    const t0 = parseTime(params.get('t'));
    if (t0 != null) T = Math.min(meta.day_end, Math.max(meta.day_start, t0));
    let playing = false;
    let last = 0;

    function show() {
      const running = busmap.renderAt(T);
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
