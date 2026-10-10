#!/usr/bin/env node
// Render a city's transit day to an MP4.
//
// The page in web/ does all the drawing. This script serves the repo over
// HTTP (fetch() refuses file:// URLs), drives window.busmap in headless
// Chromium one frame at a time, and pipes the PNGs into ffmpeg. The busmap
// API relied on here is defined in docs/CONTRACT.md.
//
// Usage: node scripts/render_video.mjs [options]      (npm run render -- [options])
//
//   --root DIR        directory served as /, default: the repo root
//   --page PATH       page under root, default web/index.html
//   --city ID         city config id (tsukuba, gta, tokyo-trains, tokyo-buses, kyoto-trains,
//                     kyoto-buses, osaka-trains, osaka-buses): adds city=ID to the page query,
//                     which selects that city's built data, and defaults --out to out/ID.mp4
//   --query STR       extra query appended after record=1, e.g. "data=../data/built/network.stub.json"
//   --out FILE        default out/tsukuba-buses.mp4 in the repo (out/<city>.mp4 with --city)
//   --fps N           default 30
//   --start N         first frame, default 0
//   --end N           one past the last frame, default busmap.totalFrames
//   --capture MODE    screenshot (default) or canvas
//                     screenshot: page.screenshot() of the 1080x1920 clip
//                     canvas: canvas.toDataURL('image/png') in the page, base64-decoded here
//   --png-dir DIR     where inspection PNGs go, default <out dir>/frames
//   --png-every N     dump every Nth rendered frame as PNG; 0 = off (60 when only --png-dir is given)
//   --crf N           libx264 quality, default 17
//   --preset NAME     libx264 preset, default slow
//   --bench [N]       time both capture modes over N frames (default 30) and exit, no video
//   --serve           only serve --root and print the URL, for working on the page in a browser
//   --dry-run         print the page URL and the ffmpeg arguments, render nothing
//
// Page query (any mode; paths are relative to --root, the page sits in web/):
//
//   --data PATH       network file; adds data=../PATH
//   --basemap PATH    basemap file; adds basemap=../PATH
//   --variant NAME    adds variant=NAME (day, rush, week)
//   --render-json J   adds render=<J>, a JSON object of CONFIG keys
//   --brandhex STR    adds brandhex=STR, id:rrggbb;id:rrggbb
//
// Shorts tiers (spec C5). Without --tier everything above behaves exactly as before.
//
//   --tier T          stills | preview | final | tune
//   --out FILE        final, preview: the MP4, default out/shorts/<batch>/<name>.mp4
//                     (preview: <name>.preview.mp4)
//   --name STEM       output stem, default from --out, else <id>-<variant> of the network
//   --out-dir DIR     stills (default build/<id>/stills) and tune (required): output directory
//   --times LIST      stills, tune: comma list of H:MM (26:30 allowed) or seconds, or none to
//                     skip the stills; default busmap.stillTimes()
//   --frames LIST     stills, tune: comma list of frame indices, last allowed
//   --sheet           stills: also write <out-dir>/<name>.sheet.jpg, 270x480 tiles, at most 1568 px
//   --clip-at T       tune: the clip starts at the frame whose time is T, default stillTimes().am
//   --clip-frames N   tune: clip length, default 90; 0 = no clip
//   --roundtrip       tune: also write the 720p VP9 round trips of the am-peak still and the clip
//   --capture MODE    final, preview: screenshot | canvas | raw | webcodecs, default
//                     cities/defaults.json render.capture, else canvas
//   --jobs N          raw: pages drawing contiguous chunks at once, default min(3, CPUs - 1)
//   --start/--end N   final, preview: only frames [start, end), a slice for tests
//   --preset NAME     final: libx264 preset, default slow
//   --min-kbps N      final: bitrate floor, default 8000
//   --crf-ladder L    final: CRFs tried in turn until the floor is met, default 18,16,14,12,10
//   --key HEX         final: the render key, written into the sidecar
//   --review-dir DIR  final: write f<iiii>.png of --keep-frames and thumb-f0300.jpg there
//   --keep-frames L   final: frames kept as lossless canvas PNGs, default 0,300,last
//   --keep-intermediate  final, preview: keep the lossless intermediate next to the MP4
//
// A final or preview writes the sidecar <name>.json (preview: <name>.preview.json)
// next to the MP4, checks the MP4 with ffprobe, and prints SIDECAR <json> as
// its last line. Stills and tune print FILES <json> last.
//
// Exit status is non-zero when the page logs an error, a request fails, any
// frame fails to render or capture, or ffmpeg does not finish cleanly.

import crypto from 'node:crypto';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import zlib from 'node:zlib';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

import {
  FrameReceiver, RawSink, feedFfmpeg, finishFfmpeg, intermediateArgs, killFfmpeg, pngFromDataUrl, runFfmpeg,
  startFfmpeg as spawnFfmpeg,
} from './capture/common.mjs';
import { captureFrame as screenshotFrame } from './capture/screenshot.mjs';
import { captureFrame as canvasFrame } from './capture/canvas.mjs';

const require = createRequire(import.meta.url);
const SCRIPT_FILE = fileURLToPath(import.meta.url);
const SCRIPT_DIR = path.dirname(SCRIPT_FILE);
const REPO_ROOT = path.resolve(SCRIPT_DIR, '..');
const WIDTH = 1080;
const HEIGHT = 1920;
const PROGRESS_EVERY = 60;
const READY_TIMEOUT_MS = 180000;

const TIERS = ['stills', 'preview', 'final', 'tune'];
const PLUGINS = ['screenshot', 'canvas', 'raw', 'webcodecs'];
const DEFAULTS_FILE = path.join(REPO_ROOT, 'cities', 'defaults.json');
// The judge's image input is downscaled past this, so no sheet is larger.
const LONG_EDGE = 1568;
const TILE = { width: 270, height: 480 };
const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const SHORTS_FPS = 30;
const PREVIEW = { width: 540, height: 960, fps: 15 };
const CLIP_SMALL = { width: 540, height: 960 };
// Frames per page.evaluate in the tune clip, so one reply stays a few MB.
const CLIP_BATCH = 15;

// The header comment above doubles as the help text.
const USAGE = (() => {
  const lines = fs.readFileSync(SCRIPT_FILE, 'utf8').split('\n').slice(1);
  const out = [];
  for (const l of lines) {
    if (!l.startsWith('//')) break;
    out.push(l.slice(3));
  }
  return out.join('\n').trim();
})();

let aborted = false;

// ------------------------------------------------------------- arguments

export class UsageError extends Error {}

// Seconds from H:MM (any number of hours, so 26:30 is the next morning and
// 133:00 a week's Saturday) or a plain number of seconds.
export function parseTime(v) {
  const s = String(v).trim();
  const m = /^(\d{1,3}):([0-5]\d)$/.exec(s);
  if (m) return Number(m[1]) * 3600 + Number(m[2]) * 60;
  if (/^\d+(\.\d+)?$/.test(s)) return Number(s);
  throw new UsageError(`want a time as H:MM or seconds, got ${v}`);
}

export function parseTimes(v) {
  if (v.trim() === 'none') return [];
  return v.split(',').map((x) => parseTime(x));
}

// Frame indices and "last", kept in order, duplicates dropped.
export function parseFrameList(v, key) {
  const out = [];
  for (const raw of v.split(',')) {
    const x = raw.trim();
    const val = x === 'last' ? 'last' : /^\d+$/.test(x) ? Number(x) : null;
    if (val === null) throw new UsageError(`--${key} wants frame indices or last, got ${x}`);
    if (!out.includes(val)) out.push(val);
  }
  return out;
}

const FLAGS = new Set(['sheet', 'roundtrip', 'keep-intermediate', 'dry-run']);
// Options each tier takes beyond the page query; anything else given with
// that tier is a usage error, so a wrong call fails before it renders.
// --capture and --jobs are accepted everywhere so a caller can pass its
// defaults to every tier; stills and tune ignore them.
const TIER_OPTS = {
  stills: ['out-dir', 'name', 'times', 'frames', 'sheet', 'capture', 'jobs'],
  tune: ['out-dir', 'times', 'frames', 'clip-at', 'clip-frames', 'roundtrip', 'capture', 'jobs'],
  preview: ['out', 'name', 'capture', 'jobs', 'start', 'end', 'keep-intermediate'],
  final: ['out', 'name', 'capture', 'jobs', 'start', 'end', 'preset', 'min-kbps', 'crf-ladder', 'key',
    'keep-frames', 'review-dir', 'keep-intermediate'],
};
const COMMON_OPTS = ['root', 'page', 'city', 'query', 'data', 'basemap', 'variant', 'render-json', 'brandhex',
  'tier', 'dry-run', 'help', 'h'];
const TIER_ONLY = ['name', 'out-dir', 'times', 'frames', 'sheet', 'clip-at', 'clip-frames', 'roundtrip', 'jobs',
  'min-kbps', 'crf-ladder', 'key', 'keep-frames', 'review-dir', 'keep-intermediate'];

export function parseArgs(argv, { defaultsFile = DEFAULTS_FILE } = {}) {
  const opts = {
    root: REPO_ROOT,
    page: 'web/index.html',
    city: null,
    query: '',
    out: null,
    fps: 30,
    start: 0,
    end: null,
    capture: 'screenshot',
    pngDir: null,
    pngEvery: null,
    crf: 17,
    preset: 'slow',
    bench: null,
    serve: false,
    help: false,
    // Spec 2.12.
    data: null,
    basemap: null,
    variant: null,
    renderJson: null,
    brandhex: null,
    tier: null,
    name: null,
    outDir: null,
    times: null,
    frames: null,
    sheet: false,
    clipAt: null,
    clipFrames: 90,
    roundtrip: false,
    jobs: null,
    minKbps: 8000,
    crfLadder: [18, 16, 14, 12, 10],
    key: null,
    keepFrames: null,
    reviewDir: null,
    keepIntermediate: false,
    dryRun: false,
    captureGiven: false,
  };
  const given = new Set();
  const valued = new Set(['root', 'page', 'city', 'query', 'out', 'fps', 'start', 'end', 'capture',
    'png-dir', 'png-every', 'crf', 'preset', 'data', 'basemap', 'variant', 'render-json', 'brandhex', 'tier',
    'name', 'out-dir', 'times', 'frames', 'clip-at', 'clip-frames', 'jobs', 'min-kbps', 'crf-ladder', 'key',
    'keep-frames', 'review-dir']);
  const int = (key, v, min) => {
    if (!/^-?\d+$/.test(v) || Number(v) < min) throw new UsageError(`--${key} wants an integer >= ${min}, got ${v}`);
    return Number(v);
  };
  // A file for the page query, relative to the served root and never
  // climbing out of it.
  const relPath = (key, v) => {
    const p = v.replace(/\\/g, '/').replace(/^\.\//, '');
    if (!p || path.isAbsolute(p) || p.split('/').includes('..')) throw new UsageError(`--${key} wants a path relative to --root, got ${v}`);
    return p;
  };
  for (let k = 0; k < argv.length; k++) {
    const arg = argv[k];
    if (!arg.startsWith('--')) throw new UsageError(`unexpected argument ${arg}`);
    let key = arg.slice(2);
    let val;
    const eq = key.indexOf('=');
    if (eq >= 0) { val = key.slice(eq + 1); key = key.slice(0, eq); }
    given.add(key);
    if (key === 'help' || key === 'h') { opts.help = true; continue; }
    if (key === 'serve') { opts.serve = true; continue; }
    if (key === 'bench') {
      if (val === undefined && k + 1 < argv.length && /^\d+$/.test(argv[k + 1])) val = argv[++k];
      opts.bench = val === undefined ? 30 : int(key, val, 1);
      continue;
    }
    if (FLAGS.has(key)) {
      if (val !== undefined) throw new UsageError(`--${key} takes no value`);
      opts[key.replace(/-(\w)/g, (_, c) => c.toUpperCase())] = true;
      continue;
    }
    if (!valued.has(key)) throw new UsageError(`unknown option --${key}`);
    if (val === undefined) {
      if (k + 1 >= argv.length) throw new UsageError(`--${key} needs a value`);
      val = argv[++k];
    }
    switch (key) {
      case 'root': opts.root = path.resolve(val); break;
      case 'page': opts.page = val.replace(/^\/+/, ''); break;
      case 'city':
        if (!/^[a-z0-9_-]+$/i.test(val)) throw new UsageError(`--city wants a config id like gta or tokyo-trains, got ${val}`);
        opts.city = val;
        break;
      case 'query': opts.query = val.replace(/^[?&]+/, ''); break;
      case 'out': opts.out = path.resolve(val); break;
      case 'fps': opts.fps = int(key, val, 1); break;
      case 'start': opts.start = int(key, val, 0); break;
      case 'end': opts.end = int(key, val, 1); break;
      case 'capture':
        if (!PLUGINS.includes(val)) throw new UsageError(`--capture must be one of ${PLUGINS.join(', ')}, got ${val}`);
        opts.capture = val;
        opts.captureGiven = true;
        break;
      case 'png-dir': opts.pngDir = path.resolve(val); break;
      case 'png-every': opts.pngEvery = int(key, val, 0); break;
      case 'crf': opts.crf = int(key, val, 0); break;
      case 'preset': opts.preset = val; break;
      case 'data': opts.data = relPath(key, val); break;
      case 'basemap': opts.basemap = relPath(key, val); break;
      case 'variant':
        if (!/^[a-z0-9_-]+$/.test(val)) throw new UsageError(`--variant wants a name like day, rush or week, got ${val}`);
        opts.variant = val;
        break;
      case 'render-json': {
        let obj;
        try {
          obj = JSON.parse(val);
        } catch (e) {
          throw new UsageError(`--render-json is not JSON: ${e.message}`);
        }
        if (!obj || typeof obj !== 'object' || Array.isArray(obj)) throw new UsageError('--render-json wants a JSON object of CONFIG keys');
        opts.renderJson = val;
        break;
      }
      case 'brandhex':
        // Brand ids may hold a colon themselves (brampton:zum), so the hex is
        // whatever follows the last one.
        if (!val.split(';').every((e) => /^[^;]+:[0-9a-fA-F]{6}$/.test(e))) {
          throw new UsageError(`--brandhex wants id:rrggbb;id:rrggbb, got ${val}`);
        }
        opts.brandhex = val;
        break;
      case 'tier':
        if (!TIERS.includes(val)) throw new UsageError(`--tier must be one of ${TIERS.join(', ')}, got ${val}`);
        opts.tier = val;
        break;
      case 'name':
        if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(val)) throw new UsageError(`--name wants a file stem without slashes, got ${val}`);
        opts.name = val;
        break;
      case 'out-dir': opts.outDir = path.resolve(val); break;
      case 'times': opts.times = parseTimes(val); break;
      case 'frames': opts.frames = parseFrameList(val, key); break;
      case 'clip-at': opts.clipAt = parseTime(val); break;
      case 'clip-frames': opts.clipFrames = int(key, val, 0); break;
      case 'jobs': opts.jobs = int(key, val, 1); break;
      case 'min-kbps': opts.minKbps = int(key, val, 0); break;
      case 'crf-ladder':
        opts.crfLadder = val.split(',').map((x) => int(key, x.trim(), 0));
        if (opts.crfLadder.some((c) => c > 51)) throw new UsageError(`--crf-ladder values must be 0..51, got ${val}`);
        break;
      case 'key':
        if (!/^[0-9a-f]{1,128}$/i.test(val)) throw new UsageError(`--key wants a hex render key, got ${val}`);
        opts.key = val.toLowerCase();
        break;
      case 'keep-frames': opts.keepFrames = parseFrameList(val, key); break;
      case 'review-dir': opts.reviewDir = path.resolve(val); break;
      default: throw new UsageError(`unknown option --${key}`);
    }
  }
  if (opts.tier === null) {
    // Today's behaviour exactly: the same options, defaults and errors.
    const extra = TIER_ONLY.filter((k) => given.has(k));
    if (extra.length) throw new UsageError(`${extra.map((k) => `--${k}`).join(', ')} only work${extra.length > 1 ? '' : 's'} with --tier`);
    if (opts.capture !== 'screenshot' && opts.capture !== 'canvas') {
      throw new UsageError(`--capture must be screenshot or canvas without --tier, got ${opts.capture}`);
    }
    // The Tsukuba video keeps its historical file name; other cities are named after their config.
    if (opts.out === null) opts.out = path.join(REPO_ROOT, 'out', opts.city ? `${opts.city}.mp4` : 'tsukuba-buses.mp4');
    // Asking for a PNG directory without a cadence means "dump something".
    if (opts.pngEvery === null) opts.pngEvery = opts.pngDir ? PROGRESS_EVERY : 0;
    if (opts.pngDir === null) opts.pngDir = path.join(path.dirname(opts.out), 'frames');
    if (opts.end !== null && opts.end <= opts.start) throw new UsageError(`--end (${opts.end}) must be greater than --start (${opts.start})`);
    return opts;
  }

  const t = opts.tier;
  const allowed = new Set([...TIER_OPTS[t], ...COMMON_OPTS]);
  const wrong = [...given].filter((k) => !allowed.has(k));
  if (wrong.length) throw new UsageError(`--tier ${t} does not take ${wrong.map((k) => `--${k}`).join(', ')}`);
  if (t === 'tune' && !opts.outDir) throw new UsageError('--tier tune needs --out-dir');
  if (opts.keepFrames && !opts.reviewDir) throw new UsageError('--keep-frames needs --review-dir');
  if (opts.end !== null && opts.end <= opts.start) throw new UsageError(`--end (${opts.end}) must be greater than --start (${opts.start})`);
  if (!opts.crfLadder.length) throw new UsageError('--crf-ladder is empty');
  if (opts.reviewDir && !opts.keepFrames) opts.keepFrames = [0, 300, 'last'];
  if (!opts.captureGiven) opts.capture = defaultCapture(defaultsFile);
  if (opts.jobs === null) opts.jobs = Math.max(1, Math.min(3, cpuCount() - 1));
  return opts;
}

function cpuCount() {
  return typeof os.availableParallelism === 'function' ? os.availableParallelism() : os.cpus().length;
}

// C3: the tiers' capture method is one line in cities/defaults.json, so the
// switch to a faster plug-in needs no change here.
export function defaultCapture(file = DEFAULTS_FILE) {
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch {
    return 'canvas';
  }
  const c = ((JSON.parse(text) || {}).render || {}).capture;
  if (c === undefined) return 'canvas';
  if (!PLUGINS.includes(c)) throw new UsageError(`${file}: render.capture ${JSON.stringify(c)} is not one of ${PLUGINS.join(', ')}`);
  return c;
}

// ----------------------------------------------------------------- tools

// Spec C2: each tool is resolved once at start, first existing path wins, and
// a miss fails with every path tried. On this machine the first choice is the
// old fixed path; on Actions, after playwright install and apt-get install
// ffmpeg, Playwright's own Chromium and /usr/bin/ffmpeg apply with no
// environment variables. A value may be a function so Playwright is only
// asked for its Chromium when the first two choices miss.
export function resolveTools({ env = process.env, exists = fs.existsSync, pwPath = null, need = ['chromium', 'ffmpeg', 'ffprobe'] } = {}) {
  const onPath = (name) => (env.PATH || '').split(path.delimiter).filter(Boolean).map((d) => ['PATH', path.join(d, name)]);
  const chains = {
    chromium: [['$PLAYWRIGHT_CHROMIUM', env.PLAYWRIGHT_CHROMIUM], ['default', '/opt/pw-browsers/chromium'], ['playwright', pwPath]],
    ffmpeg: [['$FFMPEG', env.FFMPEG], ['default', '/usr/bin/ffmpeg'], ...onPath('ffmpeg')],
    ffprobe: [['$FFPROBE', env.FFPROBE], ['default', '/usr/bin/ffprobe'], ...onPath('ffprobe')],
  };
  const out = {};
  const missing = [];
  for (const name of need) {
    const tried = [];
    for (const [how, v] of chains[name]) {
      let p = v;
      if (typeof v === 'function') {
        try {
          p = v();
        } catch (e) {
          tried.push(`${how} (${e.message.split('\n')[0]})`);
          continue;
        }
      }
      if (!p) continue;
      tried.push(p);
      if (exists(p)) {
        out[name] = { path: p, how };
        break;
      }
    }
    if (!out[name]) missing.push(`${name} not found; tried ${tried.length ? tried.join(', ') : 'nothing'}`);
  }
  if (missing.length) throw new Error(missing.join('\n'));
  return out;
}

// --------------------------------------------------------- static server

const MIME = new Map(Object.entries({
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.geojson': 'application/geo+json; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.ttf': 'font/ttf',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
  '.csv': 'text/csv; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  // Built data for gzip cities. Served as a plain file, never with
  // Content-Encoding, so the page inflates it itself with DecompressionStream.
  '.gz': 'application/gzip',
}));

function reply(res, status, text) {
  res.writeHead(status, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end(text);
}

async function serveFile(root, req, res) {
  if (req.method !== 'GET' && req.method !== 'HEAD') return reply(res, 405, 'method not allowed');
  let pathname;
  try {
    pathname = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
  } catch {
    return reply(res, 400, 'bad request');
  }
  // normalize() resolves ".." before the join, so a request can never climb
  // out of root; the prefix test is a second guard in case that ever changes.
  let file = path.join(root, path.normalize(pathname));
  if (file !== root && !file.startsWith(root + path.sep)) return reply(res, 403, 'forbidden');
  let st;
  try {
    st = await fsp.stat(file);
    if (st.isDirectory()) {
      file = path.join(file, 'index.html');
      st = await fsp.stat(file);
    }
    if (!st.isFile()) throw new Error('not a file');
  } catch {
    // Chromium asks for a favicon on every load; that miss is not worth a line.
    if (pathname !== '/favicon.ico') console.error(`  server: 404 ${pathname}`);
    return reply(res, 404, `not found: ${pathname}`);
  }
  res.writeHead(200, {
    'Content-Type': MIME.get(path.extname(file).toLowerCase()) || 'application/octet-stream',
    'Content-Length': st.size,
    'Cache-Control': 'no-store',
  });
  if (req.method === 'HEAD') return res.end();
  const stream = fs.createReadStream(file);
  stream.on('error', () => res.destroy());
  stream.pipe(res);
}

// The receiver takes the raw frames pages post (capture raw, the tune round
// trip); a legacy page never asks for its route.
function startServer(root, receiver = null) {
  const server = http.createServer((req, res) => {
    if (receiver && receiver.handle(req, res)) return;
    serveFile(root, req, res).catch((err) => {
      if (!res.headersSent) res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end(`server error: ${err.message}`);
    });
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port }));
  });
}

function stopServer(server) {
  server.closeAllConnections();
  server.close();
}

// A repo file as a query value next to web/index.html: encoded, with its
// slashes kept readable.
function queryPath(p) {
  return encodeURIComponent(`../${p}`).replace(/%2F/g, '/');
}

// The page query in a fixed order: record, city, the 2.12 additions, then
// --query. Without the additions it is exactly today's query.
export function buildQuery(opts, record) {
  const q = [];
  if (record) q.push('record=1');
  if (opts.city) q.push(`city=${encodeURIComponent(opts.city)}`);
  if (opts.data) q.push(`data=${queryPath(opts.data)}`);
  if (opts.basemap) q.push(`basemap=${queryPath(opts.basemap)}`);
  if (opts.variant) q.push(`variant=${encodeURIComponent(opts.variant)}`);
  if (opts.renderJson) q.push(`render=${encodeURIComponent(opts.renderJson)}`);
  if (opts.brandhex) q.push(`brandhex=${encodeURIComponent(opts.brandhex)}`);
  if (opts.query) q.push(opts.query);
  return q.join('&');
}

function pageUrl(port, opts, record) {
  const q = buildQuery(opts, record);
  return `http://127.0.0.1:${port}/${opts.page}${q ? '?' + q : ''}`;
}

// ------------------------------------------------------------ playwright

function loadPlaywright() {
  // The dependency is declared in package.json, but the checkout may have no
  // node_modules of its own while the image ships Playwright under
  // /opt/node-tools and the global npm root, so look there too.
  const globalLib = path.join(path.dirname(path.dirname(process.execPath)), 'lib');
  const roots = [REPO_ROOT, '/opt/node-tools', globalLib];
  let resolved;
  try {
    resolved = require.resolve('playwright');
  } catch {
    try {
      resolved = require.resolve('playwright', { paths: roots });
    } catch {
      throw new Error(`playwright not found; looked in ${roots.map((r) => path.join(r, 'node_modules')).join(', ')}`);
    }
  }
  return require(resolved);
}

function throwIfPageErrors(pageErrors) {
  if (pageErrors.length) throw new Error(`the page reported errors:\n  ${pageErrors.join('\n  ')}`);
}

function withTimeout(promise, ms, what) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${what} timed out after ${ms / 1000} s`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

// The browser is launched separately from the page so the caller holds its
// handle before anything that can fail; an open Chromium keeps Node alive.
async function launchBrowser(pw, chromium) {
  return pw.chromium.launch({
    executablePath: chromium,
    headless: true,
    // /dev/shm is tiny in containers and a 1080x1920 page fills it quickly.
    args: ['--disable-dev-shm-usage'],
  });
}

async function openPage(browser, url, pageErrors) {
  const context = await browser.newContext({
    viewport: { width: WIDTH, height: HEIGHT },
    deviceScaleFactor: 1,
    colorScheme: 'dark',
  });
  const page = await context.newPage();
  page.on('pageerror', (e) => pageErrors.push(`page error: ${e.message}`));
  page.on('crash', () => pageErrors.push('page crashed'));
  page.on('console', (m) => {
    const text = m.text();
    if (m.type() === 'error') {
      // Failed resources arrive through the response hook with their URL;
      // Chromium's own console line for them adds nothing.
      if (!text.startsWith('Failed to load resource')) pageErrors.push(`console.error: ${text}`);
    } else if (m.type() === 'warning') {
      console.error(`  page warning: ${text}`);
    } else {
      console.log(`  page: ${text}`);
    }
  });
  page.on('response', (r) => {
    if (r.status() >= 400 && !r.url().endsWith('/favicon.ico')) pageErrors.push(`HTTP ${r.status()} ${r.url()}`);
  });
  page.on('requestfailed', (r) => {
    const why = (r.failure() && r.failure().errorText) || 'unknown';
    if (why !== 'net::ERR_ABORTED') pageErrors.push(`request failed: ${r.url()} (${why})`);
  });

  await page.goto(url, { waitUntil: 'load', timeout: 60000 });
  await page.waitForFunction(() => Boolean(window.busmap && window.busmap.ready), null, { timeout: 30000 });
  await withTimeout(page.evaluate(() => window.busmap.ready.then(() => true)), READY_TIMEOUT_MS, 'busmap.ready');
  throwIfPageErrors(pageErrors);
  const totalFrames = await page.evaluate(() => window.busmap.totalFrames);
  if (!Number.isInteger(totalFrames) || totalFrames <= 0) throw new Error(`busmap.totalFrames is ${totalFrames}`);
  return { page, totalFrames };
}

// Today's two capture modes, one frame each: { T, drawMs, png }. The code
// lives with the plug-ins in scripts/capture/; the tiers use their segment().
const CAPTURE = {
  screenshot: screenshotFrame,
  canvas: canvasFrame,
};

function pngSize(buf) {
  if (buf.length < 24 || buf.readUInt32BE(0) !== 0x89504e47) throw new Error('capture did not return a PNG');
  return [buf.readUInt32BE(16), buf.readUInt32BE(20)];
}

// ---------------------------------------------------------------- ffmpeg

export function legacyFfmpegArgs(opts) {
  return [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'image2pipe', '-vcodec', 'png', '-framerate', String(opts.fps), '-i', '-',
    '-c:v', 'libx264', '-preset', opts.preset, '-crf', String(opts.crf),
    // Convert and tag as BT.709 explicitly. Left alone, swscale uses BT.601
    // coefficients for RGB to YUV while players assume 709 for HD, which
    // shifts the blues and yellows the whole look rests on.
    '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p',
    '-pix_fmt', 'yuv420p',
    '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
    '-movflags', '+faststart',
    opts.out,
  ];
}

function startFfmpeg(opts, ffmpeg) {
  const args = legacyFfmpegArgs(opts);
  const proc = spawn(ffmpeg, args, { stdio: ['pipe', 'inherit', 'pipe'] });
  const ff = { proc, args, stderr: '', exited: null, stdinError: null };
  proc.stderr.on('data', (d) => { ff.stderr += d; });
  proc.stdin.on('error', (e) => { ff.stdinError = e; });
  ff.done = new Promise((resolve) => {
    proc.on('error', (e) => {
      ff.stderr += `could not start ${ffmpeg}: ${e.message}\n`;
      ff.exited = { code: -1, signal: null };
      resolve(ff.exited);
    });
    proc.on('close', (code, signal) => {
      ff.exited = { code, signal };
      resolve(ff.exited);
    });
  });
  return ff;
}

function ffmpegFailure(ff) {
  const why = ff.exited ? `exited with code ${ff.exited.code}${ff.exited.signal ? ` (${ff.exited.signal})` : ''}`
    : `stdin error: ${ff.stdinError.message}`;
  return new Error(`ffmpeg ${why}\n${ff.stderr.trim()}`);
}

async function feedLegacy(ff, png) {
  if (ff.exited || ff.stdinError) throw ffmpegFailure(ff);
  if (!ff.proc.stdin.write(png)) {
    // Back-pressure: wait for ffmpeg to catch up, unless it dies meanwhile.
    await Promise.race([once(ff.proc.stdin, 'drain').catch(() => {}), ff.done]);
    if (ff.exited || ff.stdinError) throw ffmpegFailure(ff);
  }
}

function probe(file, ffprobe) {
  try {
    const out = execFileSync(ffprobe, [
      '-v', 'error', '-select_streams', 'v:0',
      '-show_entries', 'stream=width,height,r_frame_rate,nb_frames,pix_fmt,color_space:format=duration,size',
      '-of', 'default=noprint_wrappers=1', file,
    ], { encoding: 'utf8' });
    const kv = Object.fromEntries(out.trim().split('\n').map((l) => l.split('=')));
    return kv;
  } catch (e) {
    console.error(`  ffprobe failed: ${e.message.split('\n')[0]}`);
    return null;
  }
}

// -------------------------------------------------------------- reporting

const now = () => performance.now();

function fmtDuration(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h ? `${h}h ${String(m).padStart(2, '0')}m ${String(sec).padStart(2, '0')}s`
    : `${m}m ${String(sec).padStart(2, '0')}s`;
}

// GTFS style clock so times past midnight read as 25:10 instead of wrapping.
function fmtClock(T) {
  if (typeof T !== 'number' || !Number.isFinite(T)) return '';
  const h = Math.floor(T / 3600);
  const m = Math.floor((T % 3600) / 60);
  return `${h}:${String(m).padStart(2, '0')}`;
}

function progressLine(done, count, T, stats, t0, window) {
  const elapsed = now() - t0;
  const avg = elapsed / done;
  const recent = (now() - window.t) / (done - window.done);
  const eta = recent * (count - done);
  const pct = String(Math.round(100 * done / count)).padStart(3);
  console.log(
    `  ${String(done).padStart(String(count).length)}/${count} ${pct}%  ` +
    `${avg.toFixed(0)} ms/frame avg, ${recent.toFixed(0)} recent ` +
    `(draw ${(stats.draw / done).toFixed(0)}, capture ${(stats.capture / done).toFixed(0)}, write ${(stats.write / done).toFixed(0)})  ` +
    `ETA ${fmtDuration(eta)}  clock ${fmtClock(T)}`,
  );
}

// The tiers' progress line: segments report frames, not their parts.
function tierProgress(done, count, T, t0, window) {
  const avg = (now() - t0) / done;
  const recent = (now() - window.t) / (done - window.done);
  console.log(`  ${String(done).padStart(String(count).length)}/${count} ${String(Math.round(100 * done / count)).padStart(3)}%  ` +
    `${avg.toFixed(0)} ms/frame avg, ${recent.toFixed(0)} recent  ETA ${fmtDuration(recent * (count - done))}  clock ${fmtClock(T)}`);
}

// --------------------------------------------------------- legacy render

async function bench(page, opts, start, end, pageErrors) {
  const n = end - start;
  console.log(`benchmark: ${n} frames per mode (frames ${start}..${end - 1}), no encoding`);
  await fsp.mkdir(opts.pngDir, { recursive: true });
  const results = {};
  for (const mode of ['screenshot', 'canvas']) {
    const capture = CAPTURE[mode];
    // First use pays for JIT, font rasterisation and encoder setup; keep it off the clock.
    await capture(page, start);
    let total = 0, draw = 0, bytes = 0;
    for (let i = start; i < end; i++) {
      const a = now();
      const r = await capture(page, i);
      total += now() - a;
      draw += r.drawMs;
      bytes += r.png.length;
      if (i === start) await fsp.writeFile(path.join(opts.pngDir, `bench_${mode}.png`), r.png);
    }
    throwIfPageErrors(pageErrors);
    results[mode] = total / n;
    console.log(
      `  ${mode.padEnd(10)} ${(total / n).toFixed(1)} ms/frame ` +
      `(draw ${(draw / n).toFixed(1)}, raster+capture+transfer ${((total - draw) / n).toFixed(1)}), ` +
      `${(bytes / n / 1024).toFixed(0)} KB/frame`,
    );
  }
  const faster = results.screenshot <= results.canvas ? 'screenshot' : 'canvas';
  const slower = faster === 'screenshot' ? 'canvas' : 'screenshot';
  console.log(`  faster: --capture ${faster} (${(results[slower] / results[faster]).toFixed(2)}x)`);
  console.log(`  first frames of each mode saved as ${path.join(opts.pngDir, 'bench_<mode>.png')}`);
}

// No --tier: today's render, unchanged (spec C1; F2 compares its MP4s).
async function render(opts, pw, tools) {
  const pageErrors = [];
  const { server, port } = await startServer(opts.root);
  let browser = null;
  let ff = null;
  try {
    const url = pageUrl(port, opts, true);
    console.log(`serving ${opts.root} on http://127.0.0.1:${port}`);
    console.log(`opening ${url}`);
    browser = await launchBrowser(pw, tools.chromium.path);
    const { page, totalFrames } = await openPage(browser, url, pageErrors);

    const start = opts.start;
    let end = opts.end === null ? totalFrames : opts.end;
    if (end > totalFrames) {
      console.error(`  --end ${end} is past totalFrames ${totalFrames}, clamping`);
      end = totalFrames;
    }
    if (start >= end) throw new Error(`frame range [${start}, ${end}) is empty (totalFrames ${totalFrames})`);
    const count = end - start;
    console.log(`page ready: totalFrames ${totalFrames}, rendering [${start}, ${end}) = ${count} frames at ${opts.fps} fps (${fmtDuration(count / opts.fps * 1000)})`);

    if (opts.bench !== null) {
      await bench(page, opts, start, Math.min(end, start + opts.bench), pageErrors);
      return;
    }

    await fsp.mkdir(path.dirname(opts.out), { recursive: true });
    if (opts.pngEvery > 0) await fsp.mkdir(opts.pngDir, { recursive: true });
    ff = startFfmpeg(opts, tools.ffmpeg.path);
    console.log(`capture ${opts.capture}, ffmpeg libx264 ${opts.preset} crf ${opts.crf} -> ${opts.out}`);
    if (opts.pngEvery > 0) console.log(`PNG every ${opts.pngEvery} frames -> ${opts.pngDir}`);

    const capture = CAPTURE[opts.capture];
    const stats = { draw: 0, capture: 0, write: 0, bytes: 0 };
    const t0 = now();
    const window = { t: t0, done: 0 };
    let lastT = null;
    for (let i = start; i < end; i++) {
      if (aborted) throw new Error('interrupted');
      const a = now();
      let frame;
      try {
        frame = await capture(page, i);
      } catch (e) {
        throw new Error(`frame ${i} failed: ${e.message}`);
      }
      const b = now();
      const [w, h] = pngSize(frame.png);
      if (w !== WIDTH || h !== HEIGHT) throw new Error(`frame ${i} is ${w}x${h}, expected ${WIDTH}x${HEIGHT}`);
      throwIfPageErrors(pageErrors);
      if (opts.pngEvery > 0 && (i - start) % opts.pngEvery === 0) {
        await fsp.writeFile(path.join(opts.pngDir, `frame_${String(i).padStart(5, '0')}.png`), frame.png);
      }
      await feedLegacy(ff, frame.png);
      const c = now();
      stats.draw += frame.drawMs;
      stats.capture += (b - a) - frame.drawMs;
      stats.write += c - b;
      stats.bytes += frame.png.length;
      lastT = frame.T;
      const done = i - start + 1;
      if (done % PROGRESS_EVERY === 0 || done === count) {
        progressLine(done, count, frame.T, stats, t0, window);
        window.t = now();
        window.done = done;
      }
    }

    ff.proc.stdin.end();
    const exit = await ff.done;
    if (exit.code !== 0) throw ffmpegFailure(ff);
    if (ff.stderr.trim()) console.error(ff.stderr.trim());

    const elapsed = now() - t0;
    console.log(`done: ${count} frames in ${fmtDuration(elapsed)}, ${(elapsed / count).toFixed(0)} ms/frame, ` +
      `${(stats.bytes / count / 1024).toFixed(0)} KB/frame PNG, last clock ${fmtClock(lastT)}`);
    const info = probe(opts.out, tools.ffprobe.path);
    if (info) {
      console.log(`wrote ${opts.out}: ${info.width}x${info.height} ${info.r_frame_rate} fps, ` +
        `${info.nb_frames} frames, ${Number(info.duration).toFixed(2)} s, ${info.pix_fmt} ${info.color_space || ''}, ` +
        `${(Number(info.size) / 1048576).toFixed(1)} MB`);
      if (Number(info.nb_frames) !== count) throw new Error(`output has ${info.nb_frames} frames, expected ${count}`);
    }
  } finally {
    if (ff && !ff.exited) {
      ff.proc.stdin.destroy();
      ff.proc.kill('SIGKILL');
    }
    if (browser) await browser.close().catch(() => {});
    stopServer(server);
  }
}

async function serveOnly(opts) {
  const { server, port } = await startServer(opts.root);
  console.log(`serving ${opts.root}`);
  console.log(`  interactive: ${pageUrl(port, opts, false)}`);
  console.log(`  record view: ${pageUrl(port, opts, true)}`);
  console.log('Ctrl-C to stop');
  await new Promise((resolve) => {
    const tick = setInterval(() => { if (aborted) { clearInterval(tick); resolve(); } }, 200);
  });
  stopServer(server);
}

// ================================================================= tiers

// ---------------------------------------------------- names and layouts

const pad4 = (i) => String(i).padStart(4, '0');
const pad2 = (i) => String(i).padStart(2, '0');

// File label of a still at absolute time T: hhmm (26:30 stays 2630), and on
// a week timeline ddd-hhmm with the day counted from Monday 00:00.
export function timeLabel(T, week) {
  const s = Math.floor(T);
  if (week) {
    const t = s % 86400;
    return `${DAYS[Math.floor(s / 86400) % 7]}-${pad2(Math.floor(t / 3600))}${pad2(Math.floor((t % 3600) / 60))}`;
  }
  return `${pad2(Math.floor(s / 3600))}${pad2(Math.floor((s % 3600) / 60))}`;
}

function clockLabel(T, week) {
  if (!week) return fmtClock(T);
  return `${DAYS[Math.floor(T / 86400) % 7]} ${fmtClock(T % 86400)}`;
}

// Sheet grid for n tiles of 270x480: five across, scaled down only when the
// long edge would pass 1568 px.
export function sheetLayout(n) {
  const cols = Math.max(1, Math.min(n, 5));
  const rows = Math.max(1, Math.ceil(n / cols));
  const s = Math.min(1, LONG_EDGE / Math.max(cols * TILE.width, rows * TILE.height));
  const tw = Math.floor(TILE.width * s);
  const th = Math.floor(TILE.height * s);
  return { cols, rows, tw, th, width: cols * tw, height: rows * th };
}

// C4: the final encode, from the lossless intermediate.
export function finalArgs(input, format, crf, preset, out) {
  // An RGB intermediate is converted with the BT.709 matrix as today. The IVF
  // is already BT.709 limited-range I420 but untagged, and a bare
  // out_color_matrix would make swscale assume BT.601 input, so it gets no
  // filter at all; the output tags are written either way.
  const vf = format === 'mkv-rgb' ? ['-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p'] : [];
  return [
    '-hide_banner', '-loglevel', 'error', '-y', '-i', input,
    ...vf,
    '-c:v', 'libx264', '-profile:v', 'high', '-level:v', '4.1', '-preset', preset, '-crf', String(crf),
    // A closed 15-frame GOP, YouTube's upload recommendation (half the frame
    // rate); it also lifts the bitrate of dark, sparse maps.
    '-g', '15', '-bf', '2', '-flags', '+cgop', '-x264-params', 'keyint=15:min-keyint=15:scenecut=0',
    '-pix_fmt', 'yuv420p', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
    '-r', '30', '-an', '-movflags', '+faststart', '-f', 'mp4', out,
  ];
}

// C5: the preview, every second frame at half size and 15 fps.
export function previewArgs(input, format, out) {
  const vf = format === 'mkv-rgb'
    ? 'scale=540:960:flags=area,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p'
    : 'scale=540:960:flags=area,format=yuv420p';
  return [
    '-hide_banner', '-loglevel', 'error', '-y', '-i', input,
    '-vf', vf, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26',
    '-pix_fmt', 'yuv420p', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
    '-r', String(PREVIEW.fps), '-an', '-movflags', '+faststart', '-f', 'mp4', out,
  ];
}

// C5 round trip, near enough to what a phone shows after YouTube's transcode.
export function roundtripArgs(inputArgs, out) {
  return [
    '-hide_banner', '-loglevel', 'error', '-y', ...inputArgs,
    '-vf', 'scale=720:1280:flags=area', '-c:v', 'libvpx-vp9', '-b:v', '1500k', '-row-mt', '1',
    '-an', '-f', 'webm', out,
  ];
}

// Contiguous chunks of the frame list, one per page (C3).
export function splitFrames(frames, jobs) {
  const n = Math.max(1, Math.min(jobs, frames.length));
  const size = Math.ceil(frames.length / n);
  const out = [];
  for (let k = 0; k < frames.length; k += size) out.push(frames.slice(k, k + size));
  return out;
}

// ------------------------------------------------------ probe and checks

function probeJson(file, ffprobe) {
  return JSON.parse(execFileSync(ffprobe, ['-v', 'error', '-show_streams', '-show_format', '-of', 'json', file], { encoding: 'utf8' }));
}

// Top-level MP4 box types in file order, from the box headers only, so a
// 200 MB file is never read in full.
export function mp4TopBoxes(file) {
  const fd = fs.openSync(file, 'r');
  try {
    const size = fs.fstatSync(fd).size;
    const head = Buffer.alloc(16);
    const out = [];
    let o = 0;
    while (o + 8 <= size && out.length < 64) {
      fs.readSync(fd, head, 0, 16, o);
      let len = head.readUInt32BE(0);
      if (len === 1) len = Number(head.readBigUInt64BE(8));
      else if (len === 0) len = size - o;
      out.push(head.toString('latin1', 4, 8));
      if (len < 8) break;
      o += len;
    }
    return out;
  } finally {
    fs.closeSync(fd);
  }
}

// C-2: the properties every final must have (want.profile only for finals).
export function checkFinal(info, boxes, want) {
  const problems = [];
  const streams = info.streams || [];
  const v = streams.filter((s) => s.codec_type === 'video');
  const a = streams.filter((s) => s.codec_type === 'audio');
  if (v.length !== 1) problems.push(`${v.length} video streams`);
  if (a.length) problems.push(`${a.length} audio stream(s)`);
  const s = v[0] || {};
  const expect = {
    width: want.width, height: want.height, r_frame_rate: `${want.fps}/1`, nb_frames: want.frames,
    pix_fmt: 'yuv420p', color_space: 'bt709', color_transfer: 'bt709', color_primaries: 'bt709', color_range: 'tv',
  };
  if (want.profile) expect.profile = want.profile;
  for (const [k, w] of Object.entries(expect)) {
    if (String(s[k]) !== String(w)) problems.push(`${k} is ${s[k]}, want ${w}`);
  }
  const moov = boxes.indexOf('moov');
  const mdat = boxes.indexOf('mdat');
  if (moov < 0 || mdat < 0 || moov > mdat) problems.push(`moov is not before mdat (boxes ${boxes.join(' ')})`);
  return problems;
}

// ---------------------------------------------------------- environment

function safeRealpath(p) {
  try {
    return fs.realpathSync(p);
  } catch {
    return p;
  }
}

function sha256File(file) {
  if (!file || !fs.existsSync(file)) return null;
  const h = crypto.createHash('sha256');
  const fd = fs.openSync(file, 'r');
  try {
    const buf = Buffer.alloc(1 << 20);
    let n;
    while ((n = fs.readSync(fd, buf, 0, buf.length, null)) > 0) h.update(buf.subarray(0, n));
  } finally {
    fs.closeSync(fd);
  }
  return h.digest('hex');
}

function codeShas(root) {
  const web = (p) => path.join(root, 'web', p);
  return {
    'app.js': sha256File(web('app.js')),
    'index.html': sha256File(web('index.html')),
    'color.js': sha256File(web('color.js')),
    'themes.json': sha256File(web('themes.json')),
    'presets/shorts.json': sha256File(web(path.join('presets', 'shorts.json'))),
    'render_video.mjs': sha256File(SCRIPT_FILE),
  };
}

function gitCommit() {
  try {
    return execFileSync('git', ['rev-parse', 'HEAD'], { cwd: REPO_ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
  } catch {
    return process.env.GITHUB_SHA || null;
  }
}

function ffmpegVersion(ffmpeg) {
  try {
    const line = execFileSync(ffmpeg, ['-version'], { encoding: 'utf8' }).split('\n')[0];
    const m = /version (\S+)/.exec(line);
    return m ? m[1] : line;
  } catch {
    return null;
  }
}

function runnerName() {
  if (process.env.GITHUB_ACTIONS === 'true') {
    const e = process.env;
    return `github-actions ${[e.RUNNER_OS, e.RUNNER_ARCH, e.RUNNER_NAME].filter(Boolean).join(' ')}, ${cpuCount()} cpus`;
  }
  return `local ${os.hostname()}, ${cpuCount()} cpus`;
}

// The network's meta without parsing the whole file, for --dry-run's
// defaults: the builders write meta first, so the head of the stream holds it.
export async function readNetworkMeta(file, maxBytes = 64 << 20) {
  const stream = fs.createReadStream(file);
  const src = file.endsWith('.gz') ? stream.pipe(zlib.createGunzip()) : stream;
  let text = '';
  try {
    for await (const chunk of src) {
      text += chunk.toString('utf8');
      const meta = metaFromHead(text);
      if (meta) return meta;
      if (text.length > maxBytes) break;
    }
  } finally {
    stream.destroy();
  }
  const meta = metaFromHead(text);
  if (!meta) throw new Error(`no complete "meta" object in the first ${maxBytes >> 20} MB of ${file}`);
  return meta;
}

function metaFromHead(text) {
  const k = text.indexOf('"meta"');
  const start = k < 0 ? -1 : text.indexOf('{', k);
  if (start < 0) return null;
  let depth = 0;
  let inStr = false;
  for (let i = start; i < text.length; i++) {
    const c = text[i];
    if (inStr) {
      if (c === '\\') i++;
      else if (c === '"') inStr = false;
    } else if (c === '"') {
      inStr = true;
    } else if (c === '{') {
      depth++;
    } else if (c === '}' && --depth === 0) {
      return JSON.parse(text.slice(start, i + 1));
    }
  }
  return null;
}

// ------------------------------------------------------------ page facts

async function pageInfo(page) {
  return page.evaluate(() => {
    const bm = window.busmap;
    const m = bm.meta || {};
    return {
      totalFrames: bm.totalFrames,
      v4: m.schema === 4,
      id: m.id || null,
      batch: m.batch || null,
      variant: bm.variant || '',
      week: Boolean(m.timeline && m.timeline.kind === 'week'),
      window: bm.window || null,
      stillTimes: typeof bm.stillTimes === 'function' ? bm.stillTimes() : null,
      tune: ['setHud', 'setCard', 'hudBoxes', 'stillTimes', 'progressAt'].every((k) => typeof bm[k] === 'function'),
    };
  });
}

// Output names (spec 2.12): --name, else from --out, else <id>-<variant>.
export function resolveNames(opts, info) {
  let stem = opts.name;
  if (!stem && opts.out) {
    stem = path.basename(opts.out).replace(/\.mp4$/i, '');
    if (opts.tier === 'preview') stem = stem.replace(/\.preview$/, '');
  }
  const id = info.id || opts.city || 'tsukuba';
  if (!stem) stem = info.variant ? `${id}-${info.variant}` : id;
  const outDir = path.join(REPO_ROOT, 'out', 'shorts', info.batch || 'legacy');
  let out = opts.out;
  if (!out && opts.tier === 'final') out = path.join(outDir, `${stem}.mp4`);
  if (!out && opts.tier === 'preview') out = path.join(outDir, `${stem}.preview.mp4`);
  const dir = opts.outDir || (opts.tier === 'stills' ? path.join(REPO_ROOT, 'build', id, 'stills') : null);
  // The sidecar takes the MP4's own name, so a preview's never replaces the
  // final's <stem>.json in the same directory.
  const sidecar = out ? out.replace(/\.mp4$/i, '') + '.json' : null;
  return { stem, out, dir, sidecar };
}

// The stills of a tier: --times (none = no stills), else the page's
// stillTimes(), each with its file label; a repeated label is kept once.
function stillList(opts, info) {
  const st = info.stillTimes || {};
  let times = opts.times;
  if (times === null) {
    if (!info.stillTimes) {
      if (opts.frames) return [];
      throw new Error('this network has no busmap.stillTimes(); pass --times or --frames');
    }
    times = Object.values(st);
  }
  const seen = new Set();
  const out = [];
  for (const T of times) {
    const label = timeLabel(T, info.week);
    if (seen.has(label)) continue;
    seen.add(label);
    out.push({ T, label, key: Object.keys(st).find((k) => st[k] === T) || null });
  }
  return out;
}

function frameList(list, N) {
  const out = [];
  for (const k of list || []) {
    const i = k === 'last' ? N - 1 : k;
    // N itself is the virtual frame after the last, which a loop draws as frame 0.
    if (i > N) throw new Error(`frame ${i} is past totalFrames ${N}`);
    if (!out.includes(i)) out.push(i);
  }
  return out;
}

// ------------------------------------------------------------ in the page

// These run in the page (page.evaluate), so they use nothing from here.

// Draws one still (renderAt) or frame (renderFrame), optionally into a sheet
// tile, and returns the canvas PNG.
function pageStill({ kind, x, tile, label }) {
  const bm = window.busmap;
  const c = bm.canvas;
  const t0 = performance.now();
  let T = x;
  if (kind === 'time') bm.renderAt(x);
  else T = bm.renderFrame(x);
  const png = c.toDataURL('image/png');
  const ms = performance.now() - t0;
  if (tile) {
    const g = window.__renderSheet.getContext('2d');
    g.imageSmoothingEnabled = true;
    g.imageSmoothingQuality = 'high';
    g.drawImage(c, tile.x, tile.y, tile.w, tile.h);
    g.font = '600 16px sans-serif';
    g.fillStyle = 'rgba(0,0,0,0.6)';
    g.fillRect(tile.x, tile.y, g.measureText(label).width + 12, 24);
    g.fillStyle = '#fff';
    g.fillText(label, tile.x + 6, tile.y + 18);
  }
  return { T, png, ms };
}

// A PNG (base64) scaled to w x h with the browser's best filter, as JPEG.
async function pageJpeg({ b64, w, h, quality }) {
  const img = await createImageBitmap(await (await fetch(`data:image/png;base64,${b64}`)).blob());
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  const g = c.getContext('2d', { alpha: false });
  g.imageSmoothingEnabled = true;
  g.imageSmoothingQuality = 'high';
  g.drawImage(img, 0, 0, w, h);
  return c.toDataURL('image/jpeg', quality);
}

// The tune clip: each frame drawn at full size, read back, downscaled to a
// 540x960 PNG, and for the round trip posted to the VP9 encoder.
async function pageClip({ frames, url, depth, small, post }) {
  const bm = window.busmap;
  const c = bm.canvas;
  const g = c.getContext('2d');
  const s = document.createElement('canvas');
  s.width = small.width;
  s.height = small.height;
  const sg = s.getContext('2d', { alpha: false });
  sg.imageSmoothingEnabled = true;
  sg.imageSmoothingQuality = 'high';
  const out = { pngs: [], draw: [], read: [] };
  const inflight = [];
  for (const n of frames) {
    const t0 = performance.now();
    const T = bm.renderFrame(n);
    const t1 = performance.now();
    // Read back whether or not it is posted, so ms_per_frame means the same
    // for every arm.
    const data = g.getImageData(0, 0, c.width, c.height).data;
    out.draw.push(t1 - t0);
    out.read.push(performance.now() - t1);
    sg.drawImage(c, 0, 0, small.width, small.height);
    out.pngs.push(s.toDataURL('image/png'));
    if (post) {
      const p = fetch(`${url}/${n}?T=${T}`, { method: 'POST', body: new Blob([data]) }).then(async (r) => {
        if (!r.ok) throw new Error(`frame ${n}: ${r.status} ${await r.text()}`);
      });
      p.catch(() => {});
      inflight.push(p);
      if (inflight.length >= depth) await inflight.shift();
    }
  }
  await Promise.all(inflight);
  return out;
}

// ---------------------------------------------------------------- driver

// One browser, one server and the first page, shared by a tier's steps.
class Session {
  constructor(opts, pw, tools) {
    this.opts = opts;
    this.pw = pw;
    this.tools = tools;
    this.pageErrors = [];
    this.receiver = new FrameReceiver();
    this.startedAt = new Date().toISOString();
  }

  async start() {
    const { server, port } = await startServer(this.opts.root, this.receiver);
    this.server = server;
    this.port = port;
    this.url = pageUrl(port, this.opts, true);
    console.log(`serving ${this.opts.root} on http://127.0.0.1:${port}`);
    console.log(`opening ${this.url}`);
    this.browser = await launchBrowser(this.pw, this.tools.chromium.path);
    // The browser's version, plus Playwright's build number when the
    // executable lives in a chromium-<build> directory (the lock's tools).
    const build = /chromium-(\d+)/.exec(safeRealpath(this.tools.chromium.path));
    this.chromiumVersion = `${this.browser.version()}${build ? ` (chromium-${build[1]})` : ''}`;
    const t0 = now();
    const { page, totalFrames } = await openPage(this.browser, this.url, this.pageErrors);
    this.loadMs = now() - t0;
    this.page = page;
    this.totalFrames = totalFrames;
    this.info = await pageInfo(page);
  }

  openPage() {
    return openPage(this.browser, this.url, this.pageErrors);
  }

  frameUrl(id) {
    return `http://127.0.0.1:${this.port}/__frame/${id}`;
  }

  async close() {
    if (this.browser) await this.browser.close().catch(() => {});
    if (this.server) stopServer(this.server);
  }
}

async function loadPlugin(name) {
  return (await import(`./capture/${name}.mjs`)).default;
}

// C3: frames through a capture plug-in into one intermediate. Contiguous
// chunks run on up to `jobs` pages at once, each its own segment, joined with
// the concat demuxer. Returns { file, format, drawMs, captureMs, wallMs }.
async function captureIntermediate(session, { plugin, jobs, frames, fps, workDir, keepFrames }) {
  const ffmpeg = session.tools.ffmpeg.path;
  const chunks = splitFrames(frames, Math.min(jobs, plugin.maxJobs));
  const ext = plugin.name === 'webcodecs' ? 'ivf' : 'mkv';
  const count = frames.length;
  const stats = { draw: 0, capture: 0 };
  const t0 = now();
  const window = { t: t0, done: 0 };
  let done = 0;
  let lastT = null;
  let firstError = null;
  const progress = () => {
    let prev = 0;
    return (segDone, T) => {
      done += segDone - prev;
      prev = segDone;
      if (typeof T === 'number' && (lastT === null || T > lastT)) lastT = T;
      if (done - window.done >= PROGRESS_EVERY || done === count) {
        tierProgress(done, count, T, t0, window);
        window.t = now();
        window.done = done;
      }
    };
  };
  console.log(`capture ${plugin.name}: ${chunks.length} page${chunks.length > 1 ? 's on contiguous chunks' : ''}, ` +
    `intermediate in ${workDir}`);
  const ctx = {
    browser: session.browser,
    url: session.url,
    size: { width: WIDTH, height: HEIGHT },
    fps,
    ffmpeg,
    pageErrors: session.pageErrors,
    keepFrames,
    openPage: () => session.openPage(),
    receiver: {
      register: (sink) => session.receiver.register(sink),
      unregister: (id) => session.receiver.unregister(id),
      url: (id) => session.frameUrl(id),
    },
    // One failed segment stops the others at their next frame.
    aborted: () => aborted || firstError !== null,
  };
  const segs = chunks.map((list, k) => ({ list, file: path.join(workDir, `seg${pad2(k)}.${ext}`) }));
  const results = await Promise.allSettled(segs.map((s, k) => plugin.segment({
    ...ctx, frames: s.list, outFile: s.file, page: k === 0 ? session.page : null, progress: progress(),
  }).catch((e) => {
    if (firstError === null) firstError = e;
    throw e;
  })));
  if (firstError) throw firstError;
  for (const r of results) {
    stats.draw += r.value.drawMs;
    stats.capture += r.value.captureMs;
  }
  const wallMs = now() - t0;
  let file = segs[0].file;
  if (segs.length > 1) {
    const list = path.join(workDir, 'segments.txt');
    await fsp.writeFile(list, segs.map((s) => `file '${s.file.replace(/'/g, "'\\''")}'\n`).join(''));
    file = path.join(workDir, `inter.${ext}`);
    await runFfmpeg(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy', file]);
    for (const s of segs) await fsp.rm(s.file, { force: true });
  }
  console.log(`captured ${count} frames in ${fmtDuration(wallMs)}, ${(wallMs / count).toFixed(0)} ms/frame ` +
    `(per page: draw ${(stats.draw / count).toFixed(0)}, capture ${(stats.capture / count).toFixed(0)}), last clock ${fmtClock(lastT)}`);
  return { file, format: results[0].value.format, drawMs: stats.draw, captureMs: stats.capture, wallMs };
}

function frameRange(opts, N) {
  const start = opts.start;
  let end = opts.end === null ? N : opts.end;
  if (end > N) {
    console.error(`  --end ${end} is past totalFrames ${N}, clamping`);
    end = N;
  }
  if (start >= end) throw new Error(`frame range [${start}, ${end}) is empty (totalFrames ${N})`);
  return { start, end, slice: start !== 0 || end !== N };
}

// Scratch space beside the output, so the final rename stays on one file
// system; removed whatever happens.
async function workDirFor(out) {
  const dir = path.join(path.dirname(out), `.${path.basename(out)}.work`);
  await fsp.rm(dir, { recursive: true, force: true });
  await fsp.mkdir(dir, { recursive: true });
  return dir;
}

async function keepIntermediate(file, out) {
  const kept = `${out.replace(/\.mp4$/i, '')}.inter${path.extname(file)}`;
  await fsp.rename(file, kept);
  console.log(`kept the intermediate as ${kept}`);
}

// Spec 2.13, in its key order.
function sidecarBase(session, names, fields) {
  const { opts, tools } = session;
  return {
    name: names.stem,
    variant: session.info.variant,
    tier: opts.tier,
    ...fields,
    query: buildQuery(opts, true),
    network_sha256: opts.data ? sha256File(path.join(opts.root, opts.data)) : null,
    basemap_sha256: opts.basemap ? sha256File(path.join(opts.root, opts.basemap)) : null,
    code_sha256: codeShas(opts.root),
    git_commit: gitCommit(),
    chromium: session.chromiumVersion,
    ffmpeg: ffmpegVersion(tools.ffmpeg.path),
    runner: runnerName(),
    started_at: session.startedAt,
    key: opts.key,
  };
}

async function writeSidecar(file, sc) {
  await fsp.writeFile(file, JSON.stringify(sc, null, 1) + '\n');
  console.log(`wrote ${file}`);
}

// --tier final (C3, C4, C6).
async function tierFinal(session) {
  const { opts, tools } = session;
  const N = session.totalFrames;
  const names = resolveNames(opts, session.info);
  const { start, end, slice } = frameRange(opts, N);
  const frames = [];
  for (let i = start; i < end; i++) frames.push(i);
  const count = frames.length;
  const plugin = await loadPlugin(opts.capture);
  await fsp.mkdir(path.dirname(names.out), { recursive: true });

  // Review frames: lossless canvas PNGs, written while capturing.
  const keepFrames = new Map();
  if (opts.reviewDir) {
    await fsp.mkdir(opts.reviewDir, { recursive: true });
    for (const k of opts.keepFrames) {
      const i = k === 'last' ? end - 1 : k;
      if (i < start || i >= end) {
        console.error(`  --keep-frames ${k}: frame ${i} is outside the rendered frames [${start}, ${end}), skipped`);
        continue;
      }
      keepFrames.set(i, path.join(opts.reviewDir, `f${pad4(i)}.png`));
    }
  }
  console.log(`final ${names.stem}: frames [${start}, ${end}) of ${N}${slice ? ' (a slice)' : ''}, capture ${plugin.name}` +
    `${plugin.maxJobs > 1 ? ` with ${Math.min(opts.jobs, plugin.maxJobs)} jobs` : ''}, crf ladder ${opts.crfLadder.join(',')}, ` +
    `floor ${opts.minKbps} kbps -> ${names.out}`);

  const workDir = await workDirFor(names.out);
  try {
    const inter = await captureIntermediate(session, { plugin, jobs: opts.jobs, frames, fps: SHORTS_FPS, workDir, keepFrames });

    // C4: each step of the ladder from the same intermediate; the first CRF
    // that meets the floor wins, else the last one is kept.
    const ladder = [];
    let chosen = null;
    for (const crf of opts.crfLadder) {
      const tmp = path.join(workDir, `crf${crf}.mp4`);
      const t0 = now();
      await runFfmpeg(tools.ffmpeg.path, finalArgs(inter.file, inter.format, crf, opts.preset, tmp));
      const kbps = Number(probeJson(tmp, tools.ffprobe.path).format.bit_rate) / 1000;
      ladder.push({ crf, kbps: Math.round(kbps), file: tmp });
      console.log(`  crf ${crf}: ${kbps.toFixed(0)} kbps (${((now() - t0) / 1000).toFixed(1)} s)`);
      if (kbps >= opts.minKbps) {
        chosen = ladder[ladder.length - 1];
        break;
      }
    }
    const floorMet = chosen !== null;
    if (!floorMet) {
      chosen = ladder[ladder.length - 1];
      console.error(`WARNING: no CRF of ${opts.crfLadder.join(',')} reaches ${opts.minKbps} kbps; ` +
        `keeping crf ${chosen.crf} at ${chosen.kbps} kbps (bitrate_floor_met false)`);
    }
    await fsp.rename(chosen.file, names.out);

    const problems = checkFinal(probeJson(names.out, tools.ffprobe.path), mp4TopBoxes(names.out),
      { width: WIDTH, height: HEIGHT, fps: SHORTS_FPS, frames: count, profile: 'High' });
    if (problems.length) throw new Error(`${names.out} fails the final checks (C-2):\n  ${problems.join('\n  ')}`);
    console.log(`checked ${names.out}: 1080x1920, 30/1, ${count} frames, High, yuv420p, bt709 tv, moov first, no audio`);
    if (opts.keepIntermediate) await keepIntermediate(inter.file, names.out);

    // C6: the review files.
    for (const f of keepFrames.values()) console.log(`wrote ${f}`);
    if (keepFrames.has(300)) {
      const b64 = (await fsp.readFile(keepFrames.get(300))).toString('base64');
      const jpg = await session.page.evaluate(pageJpeg, { b64, w: TILE.width, h: TILE.height, quality: 0.85 });
      const thumb = path.join(opts.reviewDir, 'thumb-f0300.jpg');
      await fsp.writeFile(thumb, pngFromDataUrl(jpg));
      console.log(`wrote ${thumb}`);
    }

    const sc = sidecarBase(session, names, {
      frames: count, fps: SHORTS_FPS, width: WIDTH, height: HEIGHT, duration_s: Number((count / SHORTS_FPS).toFixed(3)),
      crf: chosen.crf, kbps: chosen.kbps, bitrate_floor_met: floorMet, capture: plugin.name,
      ms_per_frame: Number((inter.wallMs / count).toFixed(1)),
    });
    // Beyond 2.13: every ladder step, and the frame range of a test slice.
    sc.ladder = ladder.map(({ crf, kbps }) => ({ crf, kbps }));
    if (slice) sc.range = [start, end];
    await writeSidecar(names.sidecar, sc);
    return `SIDECAR ${JSON.stringify(sc)}`;
  } finally {
    await fsp.rm(workDir, { recursive: true, force: true });
  }
}

// --tier preview (C5): every second frame, 540x960 at 15 fps.
async function tierPreview(session) {
  const { opts, tools } = session;
  const N = session.totalFrames;
  const names = resolveNames(opts, session.info);
  const { start, end, slice } = frameRange(opts, N);
  const frames = [];
  for (let i = start; i < end; i += 2) frames.push(i);
  const plugin = await loadPlugin(opts.capture);
  await fsp.mkdir(path.dirname(names.out), { recursive: true });
  console.log(`preview ${names.stem}: every 2nd frame of [${start}, ${end}), capture ${plugin.name} -> ${names.out}`);
  const workDir = await workDirFor(names.out);
  try {
    const inter = await captureIntermediate(session, { plugin, jobs: opts.jobs, frames, fps: PREVIEW.fps, workDir, keepFrames: null });
    const tmp = path.join(workDir, 'preview.mp4');
    await runFfmpeg(tools.ffmpeg.path, previewArgs(inter.file, inter.format, tmp));
    await fsp.rename(tmp, names.out);
    const info = probeJson(names.out, tools.ffprobe.path);
    const problems = checkFinal(info, mp4TopBoxes(names.out),
      { width: PREVIEW.width, height: PREVIEW.height, fps: PREVIEW.fps, frames: frames.length });
    if (problems.length) throw new Error(`${names.out} fails the preview checks:\n  ${problems.join('\n  ')}`);
    if (opts.keepIntermediate) await keepIntermediate(inter.file, names.out);
    const kbps = Math.round(Number(info.format.bit_rate) / 1000);
    console.log(`wrote ${names.out}: ${frames.length} frames, ${kbps} kbps`);
    const sc = sidecarBase(session, names, {
      frames: frames.length, fps: PREVIEW.fps, width: PREVIEW.width, height: PREVIEW.height,
      duration_s: Number((frames.length / PREVIEW.fps).toFixed(3)), crf: 26, kbps, bitrate_floor_met: null,
      capture: plugin.name, ms_per_frame: Number((inter.wallMs / frames.length).toFixed(1)),
    });
    if (slice) sc.range = [start, end];
    await writeSidecar(names.sidecar, sc);
    return `SIDECAR ${JSON.stringify(sc)}`;
  } finally {
    await fsp.rm(workDir, { recursive: true, force: true });
  }
}

// --tier stills (C5): exact canvas PNGs, and optionally a contact sheet.
async function tierStills(session) {
  const { opts, info, page } = session;
  const N = session.totalFrames;
  const names = resolveNames(opts, info);
  const items = [
    ...stillList(opts, info).map((s) => ({ kind: 'time', x: s.T, file: `${names.stem}-${s.label}.png`, label: clockLabel(s.T, info.week) })),
    ...frameList(opts.frames, N).map((i) => ({ kind: 'frame', x: i, file: `${names.stem}-f${pad4(i)}.png`, label: `frame ${i}` })),
  ];
  if (!items.length) throw new Error('nothing to draw: --times none and no --frames');
  await fsp.mkdir(names.dir, { recursive: true });
  const L = opts.sheet ? sheetLayout(items.length) : null;
  if (L) {
    await page.evaluate(([w, h]) => {
      const s = document.createElement('canvas');
      s.width = w;
      s.height = h;
      const g = s.getContext('2d', { alpha: false });
      g.fillStyle = '#000';
      g.fillRect(0, 0, w, h);
      window.__renderSheet = s;
    }, [L.width, L.height]);
  }
  const files = [];
  for (const [k, it] of items.entries()) {
    if (aborted) throw new Error('interrupted');
    const tile = L ? { x: (k % L.cols) * L.tw, y: Math.floor(k / L.cols) * L.th, w: L.tw, h: L.th } : null;
    const r = await page.evaluate(pageStill, { kind: it.kind, x: it.x, tile, label: it.label });
    throwIfPageErrors(session.pageErrors);
    const file = path.join(names.dir, it.file);
    await fsp.writeFile(file, pngFromDataUrl(r.png));
    files.push(file);
    console.log(`wrote ${file} (${it.kind === 'time' ? `T ${it.x}, ${it.label}` : `frame ${it.x}, T ${Math.round(r.T)}`}, ${r.ms.toFixed(0)} ms)`);
  }
  if (L) {
    const jpg = await page.evaluate(() => window.__renderSheet.toDataURL('image/jpeg', 0.9));
    const file = path.join(names.dir, `${names.stem}.sheet.jpg`);
    await fsp.writeFile(file, pngFromDataUrl(jpg));
    files.push(file);
    console.log(`wrote ${file} (${L.width}x${L.height}, ${items.length} tiles)`);
  }
  return `FILES ${JSON.stringify(files)}`;
}

const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0);

// --tier tune (C5, G2): every file of one tuning arm from one page load.
async function tierTune(session) {
  const { opts, tools, info, page } = session;
  if (!info.tune) throw new Error('--tier tune needs a schema 4 network (busmap.setHud, setCard, hudBoxes, stillTimes)');
  const N = session.totalFrames;
  const dir = opts.outDir;
  await fsp.mkdir(dir, { recursive: true });
  const t0 = now();
  const files = [];
  const write = async (name, data) => {
    const file = path.join(dir, name);
    await fsp.writeFile(file, data);
    files.push(file);
    return file;
  };
  const json = (v) => JSON.stringify(v) + '\n';
  const round2 = (a) => a.map((v) => Math.round(v * 100) / 100);

  // The effective knobs after init, before any switch below.
  await write('config.json', JSON.stringify(await page.evaluate(() => JSON.parse(JSON.stringify(window.busmap.config))), null, 1) + '\n');

  const timing = { load_ms: Math.round(session.loadMs), total_frames: N, stills: [], frames: [] };
  const stills = stillList(opts, info);
  await page.evaluate(() => { window.busmap.setHud('full'); window.busmap.setCard(true); });
  for (const s of stills) {
    const r = await page.evaluate((T) => {
      const bm = window.busmap;
      const t0 = performance.now();
      bm.renderAt(T);
      const png = bm.canvas.toDataURL('image/png');
      return { png, ms: performance.now() - t0, boxes: bm.hudBoxes(), veh: Array.from(bm.lastVehicles) };
    }, s.T);
    throwIfPageErrors(session.pageErrors);
    s.file = await write(`still-${s.label}.png`, pngFromDataUrl(r.png));
    await write(`vehicles-${s.label}.json`, json(round2(r.veh)));
    await write(`boxes-${s.label}.json`, json(r.boxes));
    timing.stills.push({ label: s.label, key: s.key, T: s.T, ms: Math.round(r.ms) });
  }
  await page.evaluate(() => window.busmap.setHud('notext'));
  for (const s of stills) {
    const png = await page.evaluate((T) => { window.busmap.renderAt(T); return window.busmap.canvas.toDataURL('image/png'); }, s.T);
    await write(`bg-${s.label}.png`, pngFromDataUrl(png));
  }
  for (const i of frameList(opts.frames, N)) {
    const r = await page.evaluate((n) => {
      const bm = window.busmap;
      bm.setHud('full');
      bm.setCard(true);
      const t0 = performance.now();
      bm.renderFrame(n);
      const png = bm.canvas.toDataURL('image/png');
      const ms = performance.now() - t0;
      const boxes = bm.hudBoxes();
      const veh = Array.from(bm.lastVehicles);
      bm.setHud('notext');
      bm.renderFrame(n);
      return { png, ms, boxes, veh, bg: bm.canvas.toDataURL('image/png') };
    }, i);
    throwIfPageErrors(session.pageErrors);
    await write(`frame-${pad4(i)}.png`, pngFromDataUrl(r.png));
    await write(`bgframe-${pad4(i)}.png`, pngFromDataUrl(r.bg));
    await write(`boxes-f${pad4(i)}.json`, json(r.boxes));
    // G4 card_cover: how much of the city the frame-0 card hides.
    await write(`vehicles-f${pad4(i)}.json`, json(round2(r.veh)));
    timing.frames.push({ frame: i, ms: Math.round(r.ms) });
  }

  // The clip, card off, from the frame whose time is --clip-at.
  const rtClip = path.join(dir, '.rt-clip.webm');
  if (opts.clipFrames > 0) {
    const n = Math.min(opts.clipFrames, N);
    const at = opts.clipAt !== null ? opts.clipAt : info.stillTimes.am;
    const i0raw = await page.evaluate(([T, total]) => Math.round(window.busmap.progressAt(T) * total), [at, N]);
    const i0 = Math.max(0, Math.min(i0raw, N - n));
    const clip = [];
    for (let i = i0; i < i0 + n; i++) clip.push(i);
    await page.evaluate(() => { window.busmap.setHud('full'); window.busmap.setCard(false); });
    let ff = null;
    let sink = null;
    let sinkId = null;
    if (opts.roundtrip) {
      ff = spawnFfmpeg(tools.ffmpeg.path, roundtripArgs(['-f', 'rawvideo', '-pix_fmt', 'rgba', '-video_size', `${WIDTH}x${HEIGHT}`,
        '-framerate', String(SHORTS_FPS), '-i', '-'], rtClip));
      sink = new RawSink(clip, WIDTH * HEIGHT * 4, async (chunks) => { for (const c of chunks) await feedFfmpeg(ff, c); },
        () => {}, () => aborted);
      sinkId = session.receiver.register(sink);
    }
    const c0 = now();
    const draw = [];
    const read = [];
    try {
      for (let k = 0; k < clip.length; k += CLIP_BATCH) {
        if (aborted) throw new Error('interrupted');
        const r = await page.evaluate(pageClip, {
          frames: clip.slice(k, k + CLIP_BATCH), url: sinkId === null ? '' : session.frameUrl(sinkId), depth: 2,
          small: CLIP_SMALL, post: sink !== null,
        });
        if (sink && sink.error) throw sink.error;
        throwIfPageErrors(session.pageErrors);
        for (const [j, png] of r.pngs.entries()) await write(`clip-${String(k + j).padStart(3, '0')}.png`, pngFromDataUrl(png));
        draw.push(...r.draw);
        read.push(...r.read);
      }
      if (ff) await finishFfmpeg(ff);
    } finally {
      if (sinkId !== null) session.receiver.unregister(sinkId);
      killFfmpeg(ff);
    }
    timing.clip = {
      at, start: i0, frames: n, wall_ms: Math.round(now() - c0),
      draw_ms: Number(mean(draw).toFixed(1)), read_ms: Number(mean(read).toFixed(1)),
      ms_per_frame: Number(mean(draw.map((d, k) => d + read[k])).toFixed(1)),
    };
    console.log(`clip: frames ${i0}..${i0 + n - 1} from ${fmtClock(at)}, ${timing.clip.ms_per_frame} ms/frame draw and readback`);
  }

  if (opts.roundtrip) {
    const r0 = now();
    const s = stills.find((x) => x.key === 'am') || stills[0];
    if (s) {
      const webm = path.join(dir, '.rt-still.webm');
      await runFfmpeg(tools.ffmpeg.path, roundtripArgs(['-framerate', String(SHORTS_FPS), '-i', s.file], webm));
      const png = path.join(dir, `rt-still-${s.label}.png`);
      await runFfmpeg(tools.ffmpeg.path, ['-hide_banner', '-loglevel', 'error', '-y', '-i', webm, '-frames:v', '1',
        '-f', 'image2', '-update', '1', png]);
      files.push(png);
      await fsp.rm(webm, { force: true });
    }
    if (timing.clip) {
      await runFfmpeg(tools.ffmpeg.path, ['-hide_banner', '-loglevel', 'error', '-y', '-i', rtClip,
        '-start_number', '0', '-f', 'image2', path.join(dir, 'rt-clip-%03d.png')]);
      const rt = (await fsp.readdir(dir)).filter((f) => /^rt-clip-\d{3}\.png$/.test(f)).sort();
      if (rt.length !== timing.clip.frames) throw new Error(`the clip round trip decoded ${rt.length} frames, want ${timing.clip.frames}`);
      files.push(...rt.map((f) => path.join(dir, f)));
      await fsp.rm(rtClip, { force: true });
    }
    timing.roundtrip_ms = Math.round(now() - r0);
  }

  timing.total_ms = Math.round(now() - t0 + session.loadMs);
  if (timing.clip) {
    timing.ms_per_frame = timing.clip.ms_per_frame;
    timing.ms_per_frame_basis = 'clip frames: renderFrame plus the full-size getImageData readback';
  } else {
    timing.ms_per_frame = Number(mean(timing.stills.map((s) => s.ms).concat(timing.frames.map((f) => f.ms))).toFixed(1));
    timing.ms_per_frame_basis = 'stills and frames: render plus the full-size PNG';
  }
  await write('timing.json', JSON.stringify(timing, null, 1) + '\n');
  console.log(`tune: ${files.length} files in ${dir}, ${fmtDuration(timing.total_ms)}`);
  return `FILES ${JSON.stringify(files)}`;
}

// --dry-run: the page URL and the ffmpeg arguments, without a browser. The
// network's meta (read from the file head) fills in the default names.
async function dryRun(opts, tools) {
  const query = buildQuery(opts, true);
  const plan = { tier: opts.tier, url: `http://127.0.0.1:PORT/${opts.page}${query ? '?' + query : ''}`, query };
  if (!opts.tier) {
    plan.out = opts.out;
    plan.capture = opts.capture;
    plan.ffmpeg = [tools.ffmpeg.path, ...legacyFfmpegArgs(opts)];
  } else {
    let meta = {};
    if (opts.data && fs.existsSync(path.join(opts.root, opts.data))) {
      try {
        meta = await readNetworkMeta(path.join(opts.root, opts.data));
      } catch (e) {
        plan.meta_error = e.message;
      }
    }
    const variant = opts.variant || (meta.variants ? Object.keys(meta.variants)[0] : '');
    const info = { id: meta.id || null, batch: meta.batch || null, variant, week: Boolean(meta.timeline && meta.timeline.kind === 'week') };
    const names = resolveNames(opts, info);
    const v = meta.variants && meta.variants[variant];
    plan.name = names.stem;
    plan.frames = v && v.render && v.render.DURATION_FRAMES ? v.render.DURATION_FRAMES : null;
    if (opts.tier === 'final' || opts.tier === 'preview') {
      const plugin = await loadPlugin(opts.capture);
      const format = plugin.name === 'webcodecs' ? 'ivf-i420' : 'mkv-rgb';
      const jobs = Math.min(opts.jobs, plugin.maxJobs);
      // As captureIntermediate names it: one segment is used as it is, several are joined.
      const inter = path.join(path.dirname(names.out), `.${path.basename(names.out)}.work`,
        jobs > 1 ? 'inter.mkv' : `seg00.${format === 'mkv-rgb' ? 'mkv' : 'ivf'}`);
      Object.assign(plan, { capture: plugin.name, jobs, out: names.out, sidecar: names.sidecar });
      if (format === 'mkv-rgb') {
        plan.intermediate_ffmpeg = [tools.ffmpeg.path, ...intermediateArgs(plugin.name === 'raw' ? 'rgba' : 'png',
          { width: WIDTH, height: HEIGHT }, opts.tier === 'preview' ? PREVIEW.fps : SHORTS_FPS, '<segment>.mkv')];
      }
      plan.ffmpeg = [tools.ffmpeg.path, ...(opts.tier === 'final'
        ? finalArgs(inter, format, opts.crfLadder[0], opts.preset, names.out)
        : previewArgs(inter, format, names.out))];
      if (opts.tier === 'final') {
        Object.assign(plan, { crf_ladder: opts.crfLadder, min_kbps: opts.minKbps, key: opts.key });
        if (opts.reviewDir) plan.review = { dir: opts.reviewDir, keep_frames: opts.keepFrames };
      }
    } else {
      plan.out_dir = names.dir;
      plan.times = opts.times === null ? 'stillTimes()' : opts.times.map((T) => ({ T, label: timeLabel(T, info.week) }));
      plan.frames_list = opts.frames;
      if (opts.tier === 'tune' && opts.roundtrip) plan.ffmpeg = [tools.ffmpeg.path, ...roundtripArgs(['-i', '<frames>'], '<roundtrip>.webm')];
    }
  }
  console.log(`url: ${plan.url}`);
  if (plan.intermediate_ffmpeg) console.log(`intermediate: ${plan.intermediate_ffmpeg.join(' ')}`);
  if (plan.ffmpeg) console.log(`ffmpeg: ${plan.ffmpeg.join(' ')}`);
  console.log(`DRYRUN ${JSON.stringify(plan)}`);
}

// For tests/render: the capture step of a final alone, on any frame list,
// through the same session, plug-in, chunking and join as a real final.
// argv as for --tier final; returns the intermediate { file, format }.
export async function captureOnly(argv, frames, workDir) {
  const opts = parseArgs(['--tier', 'final', ...argv]);
  const pw = loadPlaywright();
  const tools = resolveTools({ pwPath: () => pw.chromium.executablePath() });
  const session = new Session(opts, pw, tools);
  try {
    await session.start();
    await fsp.mkdir(workDir, { recursive: true });
    const plugin = await loadPlugin(opts.capture);
    const r = await captureIntermediate(session, { plugin, jobs: opts.jobs, frames, fps: SHORTS_FPS, workDir, keepFrames: null });
    return { file: r.file, format: r.format };
  } finally {
    await session.close();
  }
}

// Each tier returns its summary line (SIDECAR or FILES), printed only once
// the browser is closed, so no late page message can follow it.
async function renderTier(opts, pw, tools) {
  const session = new Session(opts, pw, tools);
  let last;
  try {
    await session.start();
    const { info } = session;
    console.log(`page ready: ${info.v4 ? 'schema 4' : 'legacy'} network${info.id ? ` ${info.id}` : ''}` +
      `${info.variant ? `, variant ${info.variant}` : ''}, totalFrames ${session.totalFrames}, loaded in ${(session.loadMs / 1000).toFixed(1)} s`);
    const tier = { final: tierFinal, preview: tierPreview, stills: tierStills, tune: tierTune }[opts.tier];
    last = await tier(session);
  } finally {
    await session.close();
  }
  console.log(last);
}

async function main() {
  process.on('SIGINT', () => {
    if (aborted) process.exit(130);
    aborted = true;
    console.error('\ninterrupted, cleaning up');
  });
  let opts;
  try {
    opts = parseArgs(process.argv.slice(2));
  } catch (e) {
    if (!(e instanceof UsageError)) throw e;
    console.error(`error: ${e.message}\n\n${USAGE}`);
    process.exit(2);
  }
  if (opts.help) {
    console.log(USAGE);
    return;
  }
  if (opts.serve) return serveOnly(opts);
  let pw = null;
  const playwright = () => {
    if (!pw) pw = loadPlaywright();
    return pw;
  };
  const tools = resolveTools({ pwPath: () => playwright().chromium.executablePath() });
  console.log(`tools: ${Object.entries(tools).map(([k, v]) => `${k} ${v.path} (${v.how})`).join(', ')}`);
  if ((opts.tier === 'stills' || opts.tier === 'tune') && opts.captureGiven) {
    console.error(`  --capture has no effect on --tier ${opts.tier}: its images are the canvas's own PNGs`);
  }
  if (opts.dryRun) return dryRun(opts, tools);
  if (!opts.tier) return render(opts, playwright(), tools);
  return renderTier(opts, playwright(), tools);
}

// import.meta.url is the real path, so a call through a symlinked directory
// must be compared by real path too, or main() would never run and the
// process would exit 0 having rendered nothing.
function invokedPath() {
  if (!process.argv[1]) return null;
  try {
    return fs.realpathSync(process.argv[1]);
  } catch {
    return path.resolve(process.argv[1]);
  }
}

if (invokedPath() === SCRIPT_FILE) {
  main().catch((err) => {
    console.error(`\nerror: ${err.message}`);
    // Cleanup has run by now; exit outright so a leaked handle can never turn
    // a failed render into a hung one.
    process.exit(1);
  });
}
