#!/usr/bin/env node
// B-8: Toronto day at the am peak, at most 450 ms a frame including capture
// with the canvas method (toDataURL PNG, as render_video.mjs --capture canvas),
// on the sandbox. It takes build/gta-toronto/day/ when make.py has built it;
// until then the stub network of tests/web/stub_gta_v4.py (the legacy GTA
// trips in Toronto's trim box, about 60,000, the size of the real day file).
//
// Usage: node tests/web/v4_perf.mjs [--data PATH] [--frames N] [--limit MS]

import fs from 'node:fs';
import path from 'node:path';
import { openBrowser, ROOT } from './browser.mjs';

const args = process.argv.slice(2);
const opt = (name, def) => {
  const k = args.indexOf(`--${name}`);
  return k >= 0 ? args[k + 1] : def;
};
// The real Toronto build when make.py has made it, else the stub.
const real = fs.existsSync(path.join(ROOT, 'build/gta-toronto/day/network.json.gz'));
const data = opt('data', real ? 'build/gta-toronto/day/network.json.gz' : 'build/stub_v4/gta-toronto/day/network.json.gz');
const basemap = opt('basemap', real ? 'build/gta-toronto/basemap.json.gz' : 'data/gta/built/basemap.json.gz');
const frames = Number(opt('frames', 60));
const limit = Number(opt('limit', 450));

if (!fs.existsSync(path.join(ROOT, data))) {
  console.log(`SKIP ${data} missing: run python3 -I tests/web/stub_gta_v4.py --only gta-toronto`);
  process.exit(0);
}
const h = await openBrowser();
let code = 0;
try {
  const page = await h.open(`data=../${data}&basemap=../${basemap}`);
  const r = await page.evaluate(async (n) => {
    const bm = window.busmap;
    // The am peak is frame 0 of the day video (the window starts there), so
    // the frames timed cover the card (0 to 44) and the plain HUD after it.
    const i0 = Math.round(bm.progressAt(bm.stillTimes().am) * bm.totalFrames);
    bm.renderFrame(i0);
    bm.canvas.toDataURL('image/png');
    const out = { i0, frames: [], draw: 0, vehicles: 0, lv: 0 };
    for (let k = 0; k < n; k++) {
      const t0 = performance.now();
      bm.renderFrame(i0 + k);
      const t1 = performance.now();
      bm.canvas.toDataURL('image/png');
      const t2 = performance.now();
      out.frames.push(t2 - t0);
      out.draw += t1 - t0;
      const l0 = performance.now();
      const v = bm.lastVehicles;
      out.lv += performance.now() - l0;
      out.vehicles += v.length / 3;
    }
    return out;
  }, frames);
  const sorted = r.frames.slice().sort((a, b) => a - b);
  const mean = r.frames.reduce((a, b) => a + b, 0) / frames;
  console.log(`frames ${r.i0}..${r.i0 + frames - 1}: mean ${mean.toFixed(0)} ms/frame (draw ${(r.draw / frames).toFixed(0)}, ` +
    `median ${sorted[frames >> 1].toFixed(0)}, max ${sorted[frames - 1].toFixed(0)}), ` +
    `${(r.vehicles / frames).toFixed(0)} vehicles a frame, lastVehicles read ${(r.lv / frames).toFixed(2)} ms`);
  if (mean > limit) {
    console.log(`FAIL mean ${mean.toFixed(0)} ms/frame > ${limit}`);
    code = 1;
  }
  if (page.errors.length) {
    console.log(`FAIL page errors: ${page.errors.join('; ')}`);
    code = 1;
  }
} finally {
  await h.close();
}
console.log(`perf: ${code ? 'FAIL' : 'PASS'}`);
process.exitCode = code;
