// Shared Playwright setup for the renderer tests: serves the repo root the
// way scripts/render_video.mjs does (no Content-Encoding, so the page inflates
// .gz itself), opens web/index.html in record mode and collects page errors.
//
//   const h = await openBrowser();
//   const page = await h.open('data=../tests/fixtures/v4_tiny/network.json&basemap=...');
//   const png = await h.still(page, 28800);       // renderAt(T), canvas pixels
//   await h.close();

import fs from 'node:fs';
import fsp from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const CHROMIUM = process.env.PLAYWRIGHT_CHROMIUM || '/opt/pw-browsers/chromium';
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.css': 'text/css; charset=utf-8', '.woff2': 'font/woff2',
  '.png': 'image/png', '.gz': 'application/gzip',
};

export const TINY = 'data=../tests/fixtures/v4_tiny/network.json&basemap=../tests/fixtures/v4_tiny/basemap.json';
export const TINY_WEEK = 'data=../tests/fixtures/v4_tiny/week.json&basemap=../tests/fixtures/v4_tiny/basemap.json';

function loadPlaywright() {
  const globalLib = path.join(path.dirname(path.dirname(process.execPath)), 'lib');
  try {
    return require(require.resolve('playwright'));
  } catch {
    return require(require.resolve('playwright', { paths: [ROOT, '/opt/node-tools', globalLib] }));
  }
}

function startServer(root) {
  const server = http.createServer(async (req, res) => {
    try {
      const pathname = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
      let file = path.join(root, path.normalize(pathname));
      if (file !== root && !file.startsWith(root + path.sep)) throw new Error('outside root');
      let st = await fsp.stat(file);
      if (st.isDirectory()) {
        file = path.join(file, 'index.html');
        st = await fsp.stat(file);
      }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream', 'Content-Length': st.size, 'Cache-Control': 'no-store' });
      fs.createReadStream(file).pipe(res);
    } catch {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('not found');
    }
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

export async function openBrowser() {
  const pw = loadPlaywright();
  const server = await startServer(ROOT);
  const port = server.address().port;
  const browser = await pw.chromium.launch({ executablePath: CHROMIUM, headless: true, args: ['--disable-dev-shm-usage'] });
  const pages = [];
  const h = {
    port,
    browser,
    // Opens the record view with the given query. errors collects page errors
    // and console.error lines; warnings the console.warn lines.
    async open(query, { allowErrors = false } = {}) {
      const context = await browser.newContext({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1, colorScheme: 'dark' });
      const page = await context.newPage();
      page.errors = [];
      page.warnings = [];
      page.on('pageerror', (e) => page.errors.push(`page error: ${e.message}`));
      page.on('console', (m) => {
        if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) page.errors.push(m.text());
        else if (m.type() === 'warning') page.warnings.push(m.text());
      });
      page.on('response', (r) => { if (r.status() >= 400) page.errors.push(`HTTP ${r.status()} ${r.url()}`); });
      pages.push(page);
      await page.goto(`http://127.0.0.1:${port}/web/index.html?record=1&${query}`, { waitUntil: 'load' });
      await page.waitForFunction(() => Boolean(window.busmap && window.busmap.ready));
      const ok = await page.evaluate(() => window.busmap.ready.then(() => true, (e) => `FAILED: ${e.message}`));
      if (ok !== true) {
        if (allowErrors) { page.failed = ok; return page; }
        throw new Error(`${query}: ${ok}\n  ${page.errors.join('\n  ')}`);
      }
      if (page.errors.length && !allowErrors) throw new Error(`${query}: page errors\n  ${page.errors.join('\n  ')}`);
      return page;
    },
    // Canvas pixels after renderAt(T) (a still) or renderFrame(i).
    async still(page, T) {
      return page.evaluate((t) => { window.busmap.renderAt(t); return window.busmap.canvas.toDataURL('image/png'); }, T).then(dataUrl);
    },
    async frame(page, i) {
      return page.evaluate((n) => { window.busmap.renderFrame(n); return window.busmap.canvas.toDataURL('image/png'); }, i).then(dataUrl);
    },
    async close() {
      await browser.close().catch(() => {});
      server.closeAllConnections();
      server.close();
    },
  };
  return h;
}

function dataUrl(s) {
  return Buffer.from(s.slice(s.indexOf(',') + 1), 'base64');
}

// RGBA pixels of the canvas, for comparisons in the page (no PNG decoder in Node).
export async function pixelsAfter(page, fnSource, arg) {
  return page.evaluate(([src, a]) => {
    // eslint-disable-next-line no-new-func
    new Function('busmap', 'arg', src)(window.busmap, a);
    const c = window.busmap.canvas;
    const g = c.getContext('2d');
    return Array.from(g.getImageData(0, 0, c.width, c.height).data);
  }, [fnSource, arg]);
}

export function check(failures, cond, what) {
  if (!cond) failures.push(what);
  return cond;
}
