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
//
// Exit status is non-zero when the page logs an error, a request fails, any
// frame fails to render or capture, or ffmpeg does not finish cleanly.

import fs from 'node:fs';
import fsp from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, '..');
const WIDTH = 1080;
const HEIGHT = 1920;
const PROGRESS_EVERY = 60;
const READY_TIMEOUT_MS = 180000;

// The header comment above doubles as the help text.
const USAGE = (() => {
  const lines = fs.readFileSync(fileURLToPath(import.meta.url), 'utf8').split('\n').slice(1);
  const out = [];
  for (const l of lines) {
    if (!l.startsWith('//')) break;
    out.push(l.slice(3));
  }
  return out.join('\n').trim();
})();

let aborted = false;
process.on('SIGINT', () => {
  if (aborted) process.exit(130);
  aborted = true;
  console.error('\ninterrupted, cleaning up');
});

// ------------------------------------------------------------- arguments

class UsageError extends Error {}

function parseArgs(argv) {
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
  };
  const valued = new Set(['root', 'page', 'city', 'query', 'out', 'fps', 'start', 'end', 'capture',
    'png-dir', 'png-every', 'crf', 'preset']);
  const int = (key, v, min) => {
    if (!/^-?\d+$/.test(v) || Number(v) < min) throw new UsageError(`--${key} wants an integer >= ${min}, got ${v}`);
    return Number(v);
  };
  for (let k = 0; k < argv.length; k++) {
    const arg = argv[k];
    if (!arg.startsWith('--')) throw new UsageError(`unexpected argument ${arg}`);
    let key = arg.slice(2);
    let val;
    const eq = key.indexOf('=');
    if (eq >= 0) { val = key.slice(eq + 1); key = key.slice(0, eq); }
    if (key === 'help' || key === 'h') { opts.help = true; continue; }
    if (key === 'serve') { opts.serve = true; continue; }
    if (key === 'bench') {
      if (val === undefined && k + 1 < argv.length && /^\d+$/.test(argv[k + 1])) val = argv[++k];
      opts.bench = val === undefined ? 30 : int(key, val, 1);
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
        if (val !== 'screenshot' && val !== 'canvas') throw new UsageError(`--capture must be screenshot or canvas, got ${val}`);
        opts.capture = val;
        break;
      case 'png-dir': opts.pngDir = path.resolve(val); break;
      case 'png-every': opts.pngEvery = int(key, val, 0); break;
      case 'crf': opts.crf = int(key, val, 0); break;
      case 'preset': opts.preset = val; break;
      default: throw new UsageError(`unknown option --${key}`);
    }
  }
  // The Tsukuba video keeps its historical file name; other cities are named after their config.
  if (opts.out === null) opts.out = path.join(REPO_ROOT, 'out', opts.city ? `${opts.city}.mp4` : 'tsukuba-buses.mp4');
  // Asking for a PNG directory without a cadence means "dump something".
  if (opts.pngEvery === null) opts.pngEvery = opts.pngDir ? PROGRESS_EVERY : 0;
  if (opts.pngDir === null) opts.pngDir = path.join(path.dirname(opts.out), 'frames');
  if (opts.end !== null && opts.end <= opts.start) throw new UsageError(`--end (${opts.end}) must be greater than --start (${opts.start})`);
  return opts;
}

// ----------------------------------------------------------------- tools

// Spec C2: each tool is resolved once at start, first existing path wins, and
// a miss fails with every path tried. On this machine the first choice is the
// old fixed path; on Actions, after playwright install and apt-get install
// ffmpeg, Playwright's own Chromium and /usr/bin/ffmpeg apply with no
// environment variables. A value may be a function so Playwright is only
// asked for its Chromium when the first two choices miss.
function resolveTools({ env = process.env, exists = fs.existsSync, pwPath = null, need = ['chromium', 'ffmpeg', 'ffprobe'] } = {}) {
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

function startServer(root) {
  const server = http.createServer((req, res) => {
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

function pageUrl(port, opts, record) {
  const q = [];
  if (record) q.push('record=1');
  if (opts.city) q.push(`city=${encodeURIComponent(opts.city)}`);
  if (opts.query) q.push(opts.query);
  return `http://127.0.0.1:${port}/${opts.page}${q.length ? '?' + q.join('&') : ''}`;
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

// Both capture modes return { T, drawMs, png }. drawMs is the time spent
// issuing canvas commands in the page; Chromium rasterises them lazily, so
// most of the real drawing cost shows up under capture instead.
const CAPTURE = {
  async screenshot(page, i) {
    const { T, drawMs } = await page.evaluate((n) => {
      const t0 = performance.now();
      const T = window.busmap.renderFrame(n);
      return { T, drawMs: performance.now() - t0 };
    }, i);
    const png = await page.screenshot({
      type: 'png',
      clip: { x: 0, y: 0, width: WIDTH, height: HEIGHT },
      animations: 'disabled',
      caret: 'hide',
      timeout: 30000,
    });
    return { T, drawMs, png };
  },

  async canvas(page, i) {
    const { T, drawMs, data } = await page.evaluate((n) => {
      const t0 = performance.now();
      const T = window.busmap.renderFrame(n);
      const drawMs = performance.now() - t0;
      const c = window.busmap.canvas || document.querySelector('canvas');
      return { T, drawMs, data: c.toDataURL('image/png') };
    }, i);
    return { T, drawMs, png: Buffer.from(data.slice(data.indexOf(',') + 1), 'base64') };
  },
};

function pngSize(buf) {
  if (buf.length < 24 || buf.readUInt32BE(0) !== 0x89504e47) throw new Error('capture did not return a PNG');
  return [buf.readUInt32BE(16), buf.readUInt32BE(20)];
}

// ---------------------------------------------------------------- ffmpeg

function startFfmpeg(opts, ffmpeg) {
  const args = [
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

async function feedFfmpeg(ff, png) {
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

// ------------------------------------------------------------------ main

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
      await feedFfmpeg(ff, frame.png);
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

async function main() {
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
  const pw = loadPlaywright();
  const tools = resolveTools({ pwPath: () => pw.chromium.executablePath() });
  console.log(`tools: ${Object.entries(tools).map(([k, v]) => `${k} ${v.path} (${v.how})`).join(', ')}`);
  await render(opts, pw, tools);
}

main().catch((err) => {
  console.error(`\nerror: ${err.message}`);
  // Cleanup has run by now; exit outright so a leaked handle can never turn
  // a failed render into a hung one.
  process.exit(1);
});
