// Helpers for the render script's acceptance tests: a page of their own
// (served and opened independently of scripts/render_video.mjs, so the
// reference pixels do not come from the code under test), ffmpeg decodes to
// raw planes, and the comparisons.

import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
export const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const SCRIPT = path.join(REPO, 'scripts', 'render_video.mjs');
export const FFMPEG = process.env.FFMPEG || '/usr/bin/ffmpeg';
export const FFPROBE = process.env.FFPROBE || '/usr/bin/ffprobe';
const CHROMIUM = process.env.PLAYWRIGHT_CHROMIUM || '/opt/pw-browsers/chromium';
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.woff2': 'font/woff2', '.png': 'image/png', '.gz': 'application/gzip',
};

function loadPlaywright() {
  const globalLib = path.join(path.dirname(path.dirname(process.execPath)), 'lib');
  try {
    return require(require.resolve('playwright'));
  } catch {
    return require(require.resolve('playwright', { paths: [REPO, '/opt/node-tools', globalLib] }));
  }
}

export async function openBrowser(root) {
  const server = http.createServer(async (req, res) => {
    try {
      const p = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
      let file = path.join(root, path.normalize(p));
      if (file !== root && !file.startsWith(root + path.sep)) throw new Error('outside root');
      let st = await fsp.stat(file);
      if (st.isDirectory()) {
        file = path.join(file, 'index.html');
        st = await fsp.stat(file);
      }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream', 'Content-Length': st.size });
      fs.createReadStream(file).pipe(res);
    } catch {
      res.writeHead(404);
      res.end('not found');
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const pw = loadPlaywright();
  const exe = fs.existsSync(CHROMIUM) ? CHROMIUM : pw.chromium.executablePath();
  const browser = await pw.chromium.launch({ executablePath: exe, headless: true, args: ['--disable-dev-shm-usage'] });
  return {
    async open(query) {
      const context = await browser.newContext({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1, colorScheme: 'dark' });
      const page = await context.newPage();
      page.errors = [];
      page.on('pageerror', (e) => page.errors.push(e.message));
      page.on('console', (m) => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) page.errors.push(m.text()); });
      await page.goto(`http://127.0.0.1:${port}/web/index.html?record=1&${query}`, { waitUntil: 'load' });
      await page.waitForFunction(() => Boolean(window.busmap && window.busmap.ready));
      const ok = await page.evaluate(() => window.busmap.ready.then(() => true, (e) => e.message));
      if (ok !== true) throw new Error(`${query}: ${ok}`);
      return page;
    },
    async close() {
      await browser.close().catch(() => {});
      server.closeAllConnections();
      server.close();
    },
  };
}

const dataUrlBuf = (s) => Buffer.from(s.slice(s.indexOf(',') + 1), 'base64');

// The canvas PNG after renderAt(T) or renderFrame(i).
export async function canvasAt(page, T) {
  return dataUrlBuf(await page.evaluate((t) => { window.busmap.renderAt(t); return window.busmap.canvas.toDataURL('image/png'); }, T));
}

export async function canvasFrame(page, i) {
  return dataUrlBuf(await page.evaluate((n) => { window.busmap.renderFrame(n); return window.busmap.canvas.toDataURL('image/png'); }, i));
}

// Raw frames of any input ffmpeg reads, as one Buffer; args go between -i and the output.
export function decode(input, pixFmt, extra = [], inputArgs = []) {
  const r = spawnSync(FFMPEG, ['-hide_banner', '-loglevel', 'error', ...inputArgs, '-i', input, ...extra,
    '-f', 'rawvideo', '-pix_fmt', pixFmt, '-'], { maxBuffer: 2 ** 31 });
  if (r.status !== 0) throw new Error(`ffmpeg decode of ${input} failed: ${r.stderr}`);
  return r.stdout;
}

// A PNG buffer decoded to raw planes (via a temp file, which image2 needs).
export function decodePng(buf, pixFmt, extra = [], tmpDir) {
  const f = path.join(tmpDir, `.decode-${process.pid}-${Math.random().toString(36).slice(2)}.png`);
  fs.writeFileSync(f, buf);
  try {
    return decode(f, pixFmt, extra);
  } finally {
    fs.rmSync(f, { force: true });
  }
}

export function probe(file) {
  return JSON.parse(execFileSync(FFPROBE, ['-v', 'error', '-show_streams', '-show_format', '-of', 'json', file], { encoding: 'utf8' }));
}

export function topBoxes(file) {
  const buf = fs.readFileSync(file);
  const out = [];
  for (let o = 0; o + 8 <= buf.length && out.length < 64;) {
    let len = buf.readUInt32BE(o);
    if (len === 1) len = Number(buf.readBigUInt64BE(o + 8));
    else if (len === 0) len = buf.length - o;
    out.push(buf.toString('latin1', o + 4, o + 8));
    if (len < 8) break;
    o += len;
  }
  return out;
}

// PSNR of two equal-length 8-bit planes.
export function psnr(a, b) {
  if (a.length !== b.length) throw new Error(`plane sizes ${a.length} and ${b.length}`);
  let se = 0;
  for (let i = 0; i < a.length; i++) {
    const d = a[i] - b[i];
    se += d * d;
  }
  if (se === 0) return Infinity;
  return 10 * Math.log10((255 * 255) / (se / a.length));
}

// Y, U, V planes of one yuv420p frame of w x h.
export function planes(buf, w, h, frame = 0) {
  const ys = w * h;
  const cs = (w >> 1) * (h >> 1);
  const o = frame * (ys + 2 * cs);
  return { y: buf.subarray(o, o + ys), u: buf.subarray(o + ys, o + ys + cs), v: buf.subarray(o + ys + cs, o + ys + 2 * cs) };
}

export function meanOf(buf) {
  let s = 0;
  for (let i = 0; i < buf.length; i++) s += buf[i];
  return s / buf.length;
}

// Where two equal raw RGB frames differ: count and bounding box.
export function rgbDiff(a, b, w) {
  if (a.length !== b.length) return { count: -1 };
  let count = 0, x0 = Infinity, y0 = Infinity, x1 = -1, y1 = -1, max = 0;
  for (let i = 0; i < a.length; i += 3) {
    const d = Math.max(Math.abs(a[i] - b[i]), Math.abs(a[i + 1] - b[i + 1]), Math.abs(a[i + 2] - b[i + 2]));
    if (d) {
      count++;
      max = Math.max(max, d);
      const p = i / 3, x = p % w, y = Math.floor(p / w);
      x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
    }
  }
  return count ? { count, max, box: [x0, y0, x1, y1] } : { count: 0 };
}

// Runs the render script; returns { code, stdout, stderr, sidecar, files }.
export function runScript(args, { root } = {}) {
  const r = spawnSync(process.execPath, [SCRIPT, ...(root ? ['--root', root] : []), ...args],
    { encoding: 'utf8', maxBuffer: 2 ** 28 });
  const lines = (r.stdout || '').trim().split('\n');
  const last = lines[lines.length - 1] || '';
  const tag = (t) => (last.startsWith(`${t} `) ? JSON.parse(last.slice(t.length + 1)) : null);
  return { code: r.status, stdout: r.stdout, stderr: r.stderr, sidecar: tag('SIDECAR'), files: tag('FILES'), dryrun: tag('DRYRUN') };
}
