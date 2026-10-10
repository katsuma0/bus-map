#!/usr/bin/env node
// Request log of legacy page loads (spec F2, check step 4).
//
// Serves --root over HTTP the way scripts/render_video.mjs does, opens the
// record view of each legacy config in headless Chromium, renders the first
// frame of its reference slice so lazily loaded fonts are fetched too, waits
// for the network to go quiet and writes every request the page made.
//
// Usage: node tests/legacy/requests.mjs --root DIR --out FILE --configs JSON
//   --configs  [{"id": "tsukuba", "city": null, "frame": 200}, ...]
//
// Exit status is non-zero when a page fails to load or reports an error.

import fs from 'node:fs';
import fsp from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const CHROMIUM = process.env.PLAYWRIGHT_CHROMIUM || '/opt/pw-browsers/chromium';
const WIDTH = 1080;
const HEIGHT = 1920;
const QUIET_MS = 750;
const READY_TIMEOUT_MS = 180000;

const MIME = new Map(Object.entries({
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.ttf': 'font/ttf',
  '.png': 'image/png',
  '.gz': 'application/gzip',
}));

function parseArgs(argv) {
  const opts = { root: null, out: null, configs: null };
  for (let k = 0; k < argv.length; k++) {
    const key = argv[k].replace(/^--/, '');
    if (!(key in opts) || k + 1 >= argv.length) throw new Error(`bad argument ${argv[k]}`);
    opts[key] = argv[++k];
  }
  if (!opts.root || !opts.out || !opts.configs) throw new Error('--root, --out and --configs are required');
  opts.root = path.resolve(opts.root);
  opts.out = path.resolve(opts.out);
  opts.configs = JSON.parse(opts.configs);
  return opts;
}

// Same search order as scripts/render_video.mjs: the checkout, then the
// image's /opt/node-tools, then the global npm root.
function loadPlaywright() {
  const globalLib = path.join(path.dirname(path.dirname(process.execPath)), 'lib');
  const roots = [REPO_ROOT, '/opt/node-tools', globalLib];
  try {
    return require(require.resolve('playwright'));
  } catch {
    return require(require.resolve('playwright', { paths: roots }));
  }
}

function startServer(root) {
  const server = http.createServer(async (req, res) => {
    let file;
    try {
      const pathname = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
      file = path.join(root, path.normalize(pathname));
      if (file !== root && !file.startsWith(root + path.sep)) throw new Error('outside root');
      let st = await fsp.stat(file);
      if (st.isDirectory()) {
        file = path.join(file, 'index.html');
        st = await fsp.stat(file);
      }
      res.writeHead(200, {
        'Content-Type': MIME.get(path.extname(file).toLowerCase()) || 'application/octet-stream',
        'Content-Length': st.size,
        'Cache-Control': 'no-store',
      });
      fs.createReadStream(file).pipe(res);
    } catch {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('not found');
    }
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port }));
  });
}

// Resolves once no request has been in flight for QUIET_MS.
function quiet(pending, ms, timeoutMs) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    let since = Date.now();
    const tick = setInterval(() => {
      if (pending.size) since = Date.now();
      if (Date.now() - since >= ms) { clearInterval(tick); resolve(); }
      if (Date.now() - t0 > timeoutMs) { clearInterval(tick); reject(new Error(`requests still pending: ${[...pending].join(', ')}`)); }
    }, 50);
  });
}

// Chromium sometimes reports a .gz fetch as net::ERR_ABORTED once the page's
// DecompressionStream is done with the body; scripts/render_video.mjs ignores
// that error for the same reason.
function failed(x) {
  return (x.status !== null && x.status !== 200) || (x.failure !== null && x.failure !== 'net::ERR_ABORTED')
    || (x.status === null && x.failure === null);
}

async function logConfig(browser, port, cfg) {
  const q = ['record=1'];
  if (cfg.city) q.push(`city=${encodeURIComponent(cfg.city)}`);
  const url = `http://127.0.0.1:${port}/web/index.html?${q.join('&')}`;
  const context = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT }, deviceScaleFactor: 1, colorScheme: 'dark' });
  const page = await context.newPage();
  const origin = `http://127.0.0.1:${port}`;
  const requests = [];
  const errors = [];
  const pending = new Set();
  const rel = (u) => (u.startsWith(origin) ? u.slice(origin.length) : u);
  page.on('request', (r) => {
    if (!/^https?:/.test(r.url())) return;
    pending.add(r);
    requests.push({ method: r.method(), path: rel(r.url()), type: r.resourceType(), status: null, failure: null, settled: false });
  });
  // Status is the response's, when one arrived; failure is Chromium's error text.
  const settle = async (r, failure) => {
    const resp = await r.response().catch(() => null);
    const hit = requests.find((x) => x.path === rel(r.url()) && !x.settled);
    if (hit) Object.assign(hit, { status: resp ? resp.status() : null, failure, settled: true });
    pending.delete(r);
  };
  page.on('requestfinished', (r) => settle(r, null));
  page.on('requestfailed', (r) => settle(r, (r.failure() && r.failure().errorText) || 'unknown'));
  page.on('pageerror', (e) => errors.push(`page error: ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(`console.error: ${m.text()}`); });

  await page.goto(url, { waitUntil: 'load', timeout: 60000 });
  await page.waitForFunction(() => Boolean(window.busmap && window.busmap.ready), null, { timeout: 30000 });
  await Promise.race([
    page.evaluate(() => window.busmap.ready.then(() => true)),
    new Promise((_, reject) => setTimeout(() => reject(new Error('busmap.ready timed out')), READY_TIMEOUT_MS)),
  ]);
  await page.evaluate((n) => { window.busmap.renderFrame(n); return document.fonts.ready.then(() => true); }, cfg.frame);
  await quiet(pending, QUIET_MS, 60000);
  await context.close();
  for (const x of requests) delete x.settled;
  requests.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return { url: rel(url), requests, errors };
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const pw = loadPlaywright();
  const { server, port } = await startServer(opts.root);
  const browser = await pw.chromium.launch({ executablePath: CHROMIUM, headless: true, args: ['--disable-dev-shm-usage'] });
  const out = {};
  let anyFailed = false;
  try {
    for (const cfg of opts.configs) {
      const r = await logConfig(browser, port, cfg);
      out[cfg.id] = r;
      const bad = r.requests.filter(failed);
      if (r.errors.length || bad.length) anyFailed = true;
      console.log(`  ${cfg.id}: ${r.requests.length} requests${bad.length ? `, ${bad.length} failed` : ''}${r.errors.length ? `, ${r.errors.length} errors` : ''}`);
    }
  } finally {
    await browser.close().catch(() => {});
    server.closeAllConnections();
    server.close();
  }
  await fsp.mkdir(path.dirname(opts.out), { recursive: true });
  await fsp.writeFile(opts.out, JSON.stringify(out, null, 1) + '\n');
  if (anyFailed) {
    console.error('some pages reported errors or failed requests; see ' + opts.out);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(`error: ${err.message}`);
  process.exit(1);
});
