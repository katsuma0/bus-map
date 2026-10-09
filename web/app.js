/* Tsukuba bus day renderer. See docs/CONTRACT.md, "Visual spec".
 *
 * render(T) is a pure function of simulated time T (seconds since midnight,
 * may exceed 86400). The only state kept between frames is the cached static
 * base map, the glow sprites and the sparkline geometry, all built once in
 * init(). Playwright drives renderFrame(i) in any order through window.busmap.
 */
(() => {
'use strict';

const CONFIG = {
  WIDTH: 1080,
  HEIGHT: 1920,
  FPS: 30,
  // Map scale: the frame height spans this many km, centred on the data
  // origin plus CENTER_KM (km east, km north).
  KM_VERTICAL: 36,
  CENTER_KM: [-1.7, 0],
  // Timeline: the service day plays over DURATION_FRAMES, with still frames
  // held at both ends so the opening and closing states register.
  DURATION_FRAMES: 1440,
  HOLD_START: 45,
  HOLD_END: 60,
  // Time warp: minutes with no bus on the road (and none within
  // TIME_WARP_MARGIN_MIN) get TIME_WARP_EMPTY of the screen time a normal
  // minute gets. Tsukuba has no night service, so without this a quarter of
  // the video is an empty map. The clock and the sparkline stay honest; the
  // night just goes by faster.
  TIME_WARP: true,
  TIME_WARP_EMPTY: 0.25,
  TIME_WARP_MARGIN_MIN: 10,
  // Trails: one sprite stamp every TRAIL_STEP_S seconds of simulated time
  // over the last TRAIL_MINUTES, fading from TRAIL_ALPHA to 0.
  TRAIL_MINUTES: 40,
  TRAIL_STEP_S: 15,
  TRAIL_RADIUS: 7,
  TRAIL_ALPHA: 0.35,
  // How trails reach the canvas. 'sprite' stamps one glow sprite per
  // TRAIL_STEP_S sample, the literal look from the contract, but software
  // raster in headless Chromium costs about 3 us per drawImage, so 300 buses
  // (48k stamps) take well over 100 ms. 'ribbon' instead cuts the trail into
  // TRAIL_BANDS age bands, appends every bus's stretch for a band to one
  // shared Path2D built from the simplified route geometry, and strokes each
  // band once for the core and once wider for the soft shoulder: about 32
  // stroke calls per frame however many buses are running.
  TRAIL_MODE: 'ribbon',
  TRAIL_BANDS: 16,
  TRAIL_SIMPLIFY_PX: 1.5,
  // Ribbon widths in px and the shoulder's alpha relative to the core's. The
  // shoulder pass is pure pixel cost, so it only goes on the freshest
  // TRAIL_SHOULDER_BANDS bands, where the trail is bright enough to show it.
  TRAIL_CORE_W: 5,
  TRAIL_SHOULDER_W: 13,
  TRAIL_SHOULDER_ALPHA: 0.4,
  TRAIL_SHOULDER_BANDS: 3,
  // Sparkline smoothing window in minutes (per-minute counts are spiky).
  SPARK_SMOOTH_MIN: 21,
  // Trail layer resolution relative to the frame. Below 1 the trails go to a
  // smaller canvas that is scaled up; the glow is soft so nothing is lost and
  // the per-frame fill cost drops with the square of the factor.
  TRAIL_SCALE: 1,
  BUS_CORE_R: 4.5,
  BUS_HALO_R: 14,
  BUS_HALO_ALPHA: 0.35,
  ROUTE_ALPHA: 0.22,
  ROUTE_WIDTH: 2.5,
  OSM_ROUTES: false,
  OSM_ROUTES_ALPHA: 0.10,
  BASEMAP_URL: '../data/built/basemap.json',
  NETWORK_URL: '../data/built/network.json',
  COLORS: {
    bg: '#07080c',
    water: '#15171c',
    waterLine: '#1b1e24',
    minor: '#23252b',
    major: '#33363d',
    rail: '#2a2d35',
    boundary: '#2b2e36',
    route: '#2f6bff',
    routeRGB: [47, 107, 255],
    trailRGB: [47, 107, 255],
    title: '#ffffff',
    subtitle: '#8c8f99',
    panel: 'rgba(10,11,16,0.72)',
    clock: '#ffffff',
    accent: '#f2c230',
    axis: '#9a9da6',
    credit: '#6f737d',
  },
  ATTRIBUTION: [
    'Data: Tsukuba City GTFS-JP (CC BY 4.0) · Overture Maps, OSM contributors',
    'Made by Katsuma Onishi',
  ],
};

const W = CONFIG.WIDTH;
const H = CONFIG.HEIGHT;
const params = new URLSearchParams(location.search);
const RECORD = params.has('record') && params.get('record') !== '0';
if (params.has('data')) CONFIG.NETWORK_URL = params.get('data');
if (params.has('basemap')) CONFIG.BASEMAP_URL = params.get('basemap');
if (params.has('osm')) CONFIG.OSM_ROUTES = params.get('osm') !== '0';
if (params.has('trailscale')) CONFIG.TRAIL_SCALE = Number(params.get('trailscale')) || 1;
if (params.has('trailmode')) CONFIG.TRAIL_MODE = params.get('trailmode');
if (params.has('trailstep')) CONFIG.TRAIL_STEP_S = Number(params.get('trailstep')) || 15;
if (params.has('trailbands')) CONFIG.TRAIL_BANDS = Number(params.get('trailbands')) || 16;
if (params.has('shoulder')) CONFIG.TRAIL_SHOULDER_ALPHA = Number(params.get('shoulder')) || 0;
if (params.has('shoulderbands')) CONFIG.TRAIL_SHOULDER_BANDS = Number(params.get('shoulderbands')) || 0;
if (params.has('simplify')) CONFIG.TRAIL_SIMPLIFY_PX = Number(params.get('simplify')) || 1;
if (params.has('corew')) CONFIG.TRAIL_CORE_W = Number(params.get('corew')) || 5;
if (params.has('shoulderw')) CONFIG.TRAIL_SHOULDER_W = Number(params.get('shoulderw')) || 13;

const canvas = document.getElementById('frame');
canvas.width = W;
canvas.height = H;
const ctx = canvas.getContext('2d', { alpha: false });

// Projection: km east/north of origin to canvas pixels, y flipped.
const SCALE = H / CONFIG.KM_VERTICAL;
const OX = W / 2 - CONFIG.CENTER_KM[0] * SCALE;
const OY = H / 2 + CONFIG.CENTER_KM[1] * SCALE;

// Static caches, filled by init().
let meta = null;
let shapes = null;
let trips = null;
let hist = null;
let baseCanvas = null;
let trailCanvas = null;
let trailCtx = null;
let trailSprites = null;   // one per age step, alpha baked in
let trailHalf = 0;
let busSprite = null;
let busHalf = 0;
let spark = null;
let totalFrames = 0;
let trailWindow = 0;       // seconds of trail kept behind each bus
let trailSteps = 0;
// Scratch buffers for one trip's trail samples, overwritten per trip.
let sampleX = null;
let sampleY = null;
let sampleD = null;
let trailColor = '';
// Per shape: simplified screen-space polyline {x, y, cum} for ribbon trails.
let ribbonShapes = null;
let bandPaths = null;

// ---------------------------------------------------------------- helpers

function fetchJSON(url) {
  return fetch(url).then((r) => {
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  });
}

function rgba(rgb, a) {
  return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`;
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
    let active = !CONFIG.TIME_WARP;
    for (let k = m0 + i - margin; !active && k <= m0 + i + margin; k++) {
      if (hist[k] > 0) active = true;
    }
    w[i] = active ? 1 : CONFIG.TIME_WARP_EMPTY;
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
  strokeMany(g, basemap.boundary);
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
  g.strokeStyle = rgba(C.routeRGB, CONFIG.ROUTE_ALPHA);
  for (const s of network.shapes) {
    g.beginPath();
    if (tracePolyline(g, s.xy)) g.stroke();
  }
  g.globalCompositeOperation = 'source-over';
  return c;
}

function buildSprites() {
  const C = CONFIG.COLORS;
  const ts = CONFIG.TRAIL_SCALE;
  trailWindow = CONFIG.TRAIL_MINUTES * 60;
  trailSteps = Math.round(trailWindow / CONFIG.TRAIL_STEP_S);
  trailSprites = [];
  for (let k = 0; k <= trailSteps; k++) {
    const a = CONFIG.TRAIL_ALPHA * (1 - k / trailSteps);
    trailSprites.push(makeRadialSprite(CONFIG.TRAIL_RADIUS * ts, [
      [0, rgba(C.trailRGB, a)],
      [0.35, rgba(C.trailRGB, a * 0.6)],
      [0.7, rgba(C.trailRGB, a * 0.18)],
      [1, rgba(C.trailRGB, 0)],
    ]));
  }
  trailHalf = trailSprites[0].width / 2;
  sampleX = new Float64Array(trailSteps + 1);
  sampleY = new Float64Array(trailSteps + 1);
  sampleD = new Float64Array(trailSteps + 1);
  trailColor = rgba(C.trailRGB, 1);

  busSprite = makeRadialSprite(CONFIG.BUS_HALO_R, [
    [0, rgba([255, 255, 255], CONFIG.BUS_HALO_ALPHA)],
    [0.35, rgba([255, 255, 255], CONFIG.BUS_HALO_ALPHA * 0.45)],
    [0.7, rgba([255, 255, 255], CONFIG.BUS_HALO_ALPHA * 0.12)],
    [1, 'rgba(255,255,255,0)'],
  ]);
  const g = busSprite.getContext('2d');
  g.fillStyle = '#ffffff';
  g.beginPath();
  g.arc(busSprite.width / 2, busSprite.width / 2, CONFIG.BUS_CORE_R, 0, Math.PI * 2);
  g.fill();
  busHalf = busSprite.width / 2;

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
    const bitmaps = await Promise.all(trailSprites.map((c) => createImageBitmap(c)));
    trailSprites = bitmaps;
    busSprite = await createImageBitmap(busSprite);
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
  bandPaths = [];
}

function buildSparkline() {
  const x0 = 70, x1 = 610, y0 = 1500, y1 = 1620;
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

// Smoothed bus count at minute-resolution T, interpolated so the sparkline
// head sits on the curve.
function histAt(minutes) {
  const sm = spark.smooth;
  const i = Math.floor(minutes);
  const f = minutes - i;
  const a = sm[i] || 0;
  const b = sm[i + 1] === undefined ? a : sm[i + 1];
  return a + (b - a) * f;
}

async function init() {
  const [basemap, network] = await Promise.all([
    fetchJSON(CONFIG.BASEMAP_URL),
    fetchJSON(CONFIG.NETWORK_URL),
  ]);
  meta = network.meta;
  shapes = network.shapes;
  trips = network.trips;
  hist = network.hist;
  totalFrames = CONFIG.HOLD_START + CONFIG.DURATION_FRAMES + CONFIG.HOLD_END;
  buildWarp();

  // Canvas text only triggers a font load on first use, so request every
  // face explicitly before waiting on document.fonts.ready.
  const faces = [
    '500 58px Montserrat', '400 34px Montserrat', '800 118px Montserrat',
    '600 36px Montserrat', '500 20px Montserrat', '400 20px Inter',
  ];
  await Promise.all(faces.map((f) => document.fonts.load(f).catch(() => null)));
  await document.fonts.ready;

  baseCanvas = buildBase(basemap, network);
  buildSprites();
  await promoteSprites();
  buildRibbons();
  buildSparkline();
  return { basemap, network };
}

// ------------------------------------------------------------- per frame

// Walks a trip backwards from T and fills sampleX/sampleY/sampleD with the
// bus position (and km along the shape) at each of `count` samples spaced
// `step` seconds apart, where the trip was under way. Returns the inclusive
// sample range [first, last] packed as first * 1024 + last, or -1 when
// nothing is on the road. Indices into t[] and cum[] only move backwards
// because d and cum are non-decreasing in time.
function sampleTrail(trip, T, ts, step, count) {
  const t = trip.t, d = trip.d;
  const shape = shapes[trip.s];
  const cum = shape.cum, xy = shape.xy;
  const tEnd = t[t.length - 1];
  const t0 = t[0];
  let i = t.length - 2;
  let k = cum.length - 2;
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
    if (tt > tEnd) continue;
    while (i > 0 && t[i] > tt) i--;
    let dist;
    const dt = t[i + 1] - t[i];
    if (dt <= 0) dist = d[i];
    else dist = d[i] + ((tt - t[i]) / dt) * (d[i + 1] - d[i]);
    while (k > 0 && cum[k] > dist) k--;
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
// into the shared path for that band.
function addRibbon(trip, T, ts, first, last) {
  const rs = ribbonShapes[trip.s];
  if (rs.cum.length < 2) return;
  for (let b = first; b < last; b++) {
    const dA = sampleD[b + 1], dB = sampleD[b];
    if (dB - dA < 1e-4) continue;
    appendStretch(bandPaths[b], rs, dA, dB);
  }
}

function strokeRibbons(layer, ts) {
  const bands = CONFIG.TRAIL_BANDS;
  layer.strokeStyle = trailColor;
  layer.lineCap = 'butt';
  layer.lineJoin = 'round';
  // Paths are in frame pixels; a scaled layer maps them with a transform so
  // the line widths scale along.
  if (ts !== 1) layer.setTransform(ts, 0, 0, ts, 0, 0);
  for (let b = 0; b < bands; b++) {
    const path = bandPaths[b];
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
  if (ts !== 1) layer.setTransform(1, 0, 0, 1, 0, 0);
  layer.globalAlpha = 1;
}

// Sprite mode: integer positions, because a fractional drawImage goes through
// bilinear resampling and costs twice as much in software raster.
function stampTrailSprites(layer, first, last) {
  const half = trailHalf;
  const w = layer.canvas.width + half, h = layer.canvas.height + half;
  for (let s = first; s <= last; s++) {
    const x = sampleX[s], y = sampleY[s];
    if (x < -half || x > w || y < -half || y > h) continue;
    layer.drawImage(trailSprites[s], Math.round(x - half), Math.round(y - half));
  }
}

function drawHUD(T, running) {
  const C = CONFIG.COLORS;
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  ctx.textBaseline = 'alphabetic';

  // Title with tracking. Chromium adds the spacing after every glyph
  // including the last, so nudge by half a space to keep it optically centred.
  const spacing = 58 * 0.28;
  ctx.fillStyle = C.title;
  ctx.font = '500 58px Montserrat';
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
  ctx.font = '400 34px Montserrat';
  ctx.fillText(meta.subtitle || '', W / 2, 206);

  // Bottom-left panel.
  ctx.fillStyle = C.panel;
  ctx.beginPath();
  ctx.roundRect(40, 1240, 600, 460, 24);
  ctx.fill();

  ctx.textAlign = 'left';
  ctx.fillStyle = C.clock;
  ctx.font = '800 118px Montserrat';
  ctx.fillText(clockText(T), 70, 1395);

  ctx.fillStyle = C.accent;
  ctx.font = '600 36px Montserrat';
  ctx.fillText(`${running} ${running === 1 ? 'bus' : 'buses'} running`, 70, 1455);

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
  ctx.globalAlpha = 0.85;
  ctx.fillStyle = C.accent;
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.beginPath();
  ctx.moveTo(sp.x0, sp.y1 - (Math.min(histAt(sp.mStart), sp.peak) / sp.peak) * (sp.y1 - sp.y0 - 10));
  for (let j = 0; j <= lastIdx && j < sp.xs.length; j++) ctx.lineTo(sp.xs[j], sp.ys[j]);
  ctx.lineTo(xCur, yCur);
  ctx.strokeStyle = C.accent;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = 'round';
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(xCur, yCur, 4.5, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = C.axis;
  ctx.font = '500 20px Montserrat';
  ctx.textAlign = 'left';
  ctx.fillText(clockText(meta.day_start), sp.x0, 1650);
  ctx.textAlign = 'right';
  ctx.fillText(clockText(meta.day_end), sp.x1, 1650);

  ctx.fillStyle = C.credit;
  ctx.font = '400 20px Inter';
  ctx.textAlign = 'left';
  ctx.fillText(CONFIG.ATTRIBUTION[0], 70, 1680);
  ctx.fillText(CONFIG.ATTRIBUTION[1], 70, 1704);
}

function renderAt(T) {
  if (!baseCanvas) throw new Error('busmap not ready');
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  ctx.drawImage(baseCanvas, 0, 0);

  const ts = CONFIG.TRAIL_SCALE;
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
    for (let b = 0; b < CONFIG.TRAIL_BANDS; b++) bandPaths[b] = new Path2D();
  }
  let running = 0;
  for (let n = 0; n < trips.length; n++) {
    const trip = trips[n];
    const t = trip.t;
    // Finished trips keep fading for the trail window so trails never pop.
    if (t[0] > T || t[t.length - 1] < horizon) continue;
    const range = sampleTrail(trip, T, ts, step, count);
    if (range < 0) continue;
    const first = Math.floor(range / 1024);
    const last = range % 1024;
    if (first === 0) {
      running++;
      buses.push(sampleX[0] / ts, sampleY[0] / ts);
    }
    if (sprite) stampTrailSprites(layer, first, last);
    else addRibbon(trip, T, ts, first, last);
  }
  if (!sprite) strokeRibbons(layer, ts);
  if (ts !== 1) {
    ctx.globalCompositeOperation = 'lighter';
    ctx.drawImage(trailCanvas, 0, 0, W, H);
  }

  ctx.globalCompositeOperation = 'lighter';
  for (let b = 0; b < buses.length; b += 2) {
    const x = buses[b], y = buses[b + 1];
    if (x < -busHalf || x > W + busHalf || y < -busHalf || y > H + busHalf) continue;
    ctx.drawImage(busSprite, x - busHalf, y - busHalf);
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
    else if (f != null) renderFrame(Number(f));
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
      timeEl.textContent = `${clockText(T)} · ${running} running`;
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
