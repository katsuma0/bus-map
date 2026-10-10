#!/usr/bin/env node
// B18, the camera, on the v4 fixture (and a GTA network when one is built):
//
//  path     the shorts preset turns it on; 'auto' takes the path from meta.id
//           (test-tiny: pull-out-west, as make.py camera_path says), the same in
//           every variant; camera=0 turns it off
//  loop     the camera at phase 1 is the camera at phase 0, and the virtual
//           frame N equals frame 0 with it on (day, week and the rush xfade)
//  speed    sampled over every frame: the fastest point of the frame moves at
//           most CAMERA_MAX_SPEED x CAMERA_AMP frame widths a second (well
//           under 1%), its speed changes smoothly (no step between frames
//           bigger than 2% of the peak) and is never constant; the zoom pushes
//           in at frame 0 and is the fitted frame at mid loop
//  amounts  day and week: zoom 6 to 10%, drift 2 to 4% of the frame; the
//           25 s rush is held to the same speed by the cap
//  core     frame 0 keeps the pivot (the fit box centre, below the card) within
//           2% of the frame width of where the fitted frame has it
//  headroom the cached base covers the visible rectangle at every phase
//  layers   at one still, camera on and off: the same HUD boxes and counts, the
//           same inside flags, and every dot where the camera puts it
//  every    each of the six paths, the sprite, scaled and bounded trail modes
//           and CAMERA_ZOOM 0 load without errors and keep frame N equal to
//           frame 0; with zoom 0 the cached base still moves with the drift
//  sharp    against CAMERA_BASE 'vector' (the base drawn afresh every frame) at
//           the fastest phase and at frame 0: the cached base keeps at least
//           85% of the edge energy, is within 38 dB, and flickers no more
//
// Usage: node tests/web/v4_camera.mjs [--no-gta]

import fs from 'node:fs';
import path from 'node:path';
import { openBrowser, ROOT, TINY, TINY_WEEK, check } from './browser.mjs';

const failures = [];
const ok = (cond, what) => check(failures, cond, what);
const NO_GTA = process.argv.includes('--no-gta');

const PAGE_HELPERS = `
  window.__img = (draw) => { draw(); const c = busmap.canvas; return c.getContext('2d').getImageData(0, 0, c.width, c.height).data; };
  window.__diff = (a, b) => { let n = 0, max = 0; for (let k = 0; k < a.length; k++) { const d = Math.abs(a[k] - b[k]); if (d) { n++; if (d > max) max = d; } } return { n, max }; };
`;

// Speed, smoothness and amplitudes from cameraAt over every frame of the loop.
async function motion(page) {
  return page.evaluate(() => {
    const bm = window.busmap;
    const N = bm.totalFrames, W = bm.canvas.width, H = bm.canvas.height, fps = bm.config.FPS;
    const corners = [[0, 0], [W, 0], [0, H], [W, H]];
    const v = [];
    let prev = bm.cameraAt(0);
    for (let i = 1; i <= N; i++) {
      const m = bm.cameraAt(i / N);
      let d = 0;
      for (const [qx, qy] of corners) {
        const bx = (qx - prev.e) / prev.zoom, by = (qy - prev.f) / prev.zoom;
        d = Math.max(d, Math.hypot(m.zoom * bx + m.e - qx, m.zoom * by + m.f - qy));
      }
      v.push(d);
      prev = m;
    }
    let jump = 0;
    for (let i = 1; i < v.length; i++) jump = Math.max(jump, Math.abs(v[i] - v[i - 1]));
    const peak = Math.max(...v), slow = Math.min(...v);
    const zs = [];
    for (let i = 0; i < N; i++) zs.push(bm.cameraAt(i / N).zoom);
    const c0 = bm.cameraAt(0), c1 = bm.cameraAt(1), mid = bm.cameraAt(0.5);
    const cam = bm.camera;
    const [px, py] = cam.pivot;
    // The headroom: every phase's visible base rectangle inside the cache.
    const b = cam.base;
    let outside = 0;
    for (let i = 0; i < N; i++) {
      const m = bm.cameraAt(i / N);
      const x0 = -m.e / m.zoom, y0 = -m.f / m.zoom, x1 = (W - m.e) / m.zoom, y1 = (H - m.f) / m.zoom;
      if (x0 < b.x0 || y0 < b.y0 || x1 > b.x0 + b.w / b.k || y1 > b.y0 + b.h / b.k) outside++;
    }
    return {
      N, cam, cfg: { max: bm.config.CAMERA_MAX_SPEED, amp: bm.config.CAMERA_AMP, path: bm.config.CAMERA_PATH, on: bm.config.CAMERA },
      peak: peak * fps / W, slow: slow * fps / W, jump: jump / peak,
      zmax: Math.max(...zs), z0: c0.zoom, zmid: mid.zoom,
      same01: c0.zoom === c1.zoom && c0.e === c1.e && c0.f === c1.f,
      pivotShift: Math.hypot(c0.zoom * px + c0.e - px, c0.zoom * py + c0.f - py) / W,
      outside, W,
    };
  });
}

async function loopSeam(page) {
  await page.evaluate(PAGE_HELPERS);
  return page.evaluate(() => {
    const bm = window.busmap, N = bm.totalFrames;
    const f0 = __img(() => bm.renderFrame(0));
    const fN = __img(() => bm.renderFrame(N));
    const fl = __img(() => bm.renderFrame(N - 1));
    return { dN0: __diff(fN, f0), dl0: __diff(fl, f0) };
  });
}

async function variantChecks(h, query, tag, kind) {
  const page = await h.open(query);
  const r = await motion(page);
  ok(r.cfg.on === true, `${tag}: the shorts preset leaves the camera off`);
  ok(r.cfg.path === 'pull-out-west', `${tag}: auto path ${r.cfg.path}, want pull-out-west for test-tiny`);
  ok(r.same01, `${tag}: the camera at phase 1 differs from phase 0`);
  ok(r.peak <= r.cfg.max * r.cfg.amp * 1.02 && r.peak < 0.01, `${tag}: fastest point ${(r.peak * 100).toFixed(3)}% of the width a second, cap ${(r.cfg.max * 100).toFixed(2)}%`);
  ok(r.jump <= 0.02, `${tag}: the speed jumps by ${(r.jump * 100).toFixed(2)}% of its peak between two frames`);
  ok(r.slow < 0.8 * r.peak, `${tag}: the speed stays within 20% of its peak (${(r.slow * 100).toFixed(3)} vs ${(r.peak * 100).toFixed(3)})`);
  ok(r.z0 === r.zmax && r.z0 > 1, `${tag}: frame 0 is not the push-in (zoom ${r.z0} of max ${r.zmax})`);
  ok(Math.abs(r.zmid - 1) < 1e-9, `${tag}: mid loop zoom ${r.zmid}, want the fitted frame`);
  ok(r.pivotShift <= 0.02, `${tag}: frame 0 moves the core ${(r.pivotShift * 100).toFixed(2)}% of the width`);
  ok(r.outside === 0, `${tag}: the cached base misses the frame at ${r.outside} phases`);
  if (kind === 'long') {
    ok(r.cam.zoom >= 0.06 && r.cam.zoom <= 0.1, `${tag}: zoom amplitude ${r.cam.zoom.toFixed(4)}, want 0.06 to 0.10`);
    ok(r.cam.drift / r.W >= 0.02 && r.cam.drift / r.W <= 0.04, `${tag}: drift ${(r.cam.drift / r.W).toFixed(4)} of the width, want 0.02 to 0.04`);
  } else {
    ok(r.cam.scale < 1 && Math.abs(r.peak - r.cfg.max) < 0.0002, `${tag}: the short rush is not held at the cap (scale ${r.cam.scale}, peak ${r.peak})`);
  }
  const seam = await loopSeam(page);
  ok(seam.dN0.n === 0, `${tag}: with the camera the virtual frame N differs from frame 0 in ${seam.dN0.n} channels`);
  ok(seam.dl0.max <= (kind === 'short' ? 2 : 255) && seam.dl0.n > 0, `${tag}: frame N - 1 to 0 ${JSON.stringify(seam.dl0)}`);
  console.log(`  ${tag.padEnd(6)} ${r.cfg.path} zoom ${(r.cam.zoom * 100).toFixed(2)}% drift ${(r.cam.drift / r.W * 100).toFixed(2)}% `
    + `cap scale ${r.cam.scale.toFixed(3)}; fastest ${(r.peak * 100).toFixed(3)}%/s, slowest ${(r.slow * 100).toFixed(3)}%/s, `
    + `largest step ${(r.jump * 100).toFixed(2)}% of peak; core moves ${(r.pivotShift * 100).toFixed(2)}% at frame 0`);
  if (page.errors.length) failures.push(`${tag}: page errors ${page.errors.join('; ')}`);
  await page.context().close();
}

// Camera on and off at one still: the HUD and the numbers do not move, the
// inside flags are the same, and each dot is the camera's image of its fitted
// position.
async function layerChecks(h) {
  const grab = (page) => page.evaluate(() => {
    const bm = window.busmap;
    const T = bm.stillTimes().am;
    bm.renderAt(T);
    return { T, u: bm.progressAt(T), boxes: bm.hudBoxes(), count: bm.countAt(T), veh: Array.from(bm.lastVehicles) };
  });
  const on = await h.open(TINY);
  const off = await h.open(`${TINY}&camera=0`);
  const a = await grab(on), b = await grab(off);
  const cm = await on.evaluate((u) => window.busmap.cameraAt(u), a.u);
  ok(JSON.stringify(a.boxes) === JSON.stringify(b.boxes), 'layers: the HUD boxes move with the camera');
  ok(JSON.stringify(a.count) === JSON.stringify(b.count), `layers: countAt differs with the camera ${JSON.stringify(a.count)} vs ${JSON.stringify(b.count)}`);
  ok(a.veh.length === b.veh.length && a.veh.length > 0, `layers: ${a.veh.length / 3} vehicles with the camera, ${b.veh.length / 3} without`);
  let worst = 0, flags = 0;
  for (let i = 0; i + 2 < Math.min(a.veh.length, b.veh.length); i += 3) {
    worst = Math.max(worst, Math.abs(a.veh[i] - (cm.zoom * b.veh[i] + cm.e)), Math.abs(a.veh[i + 1] - (cm.zoom * b.veh[i + 1] + cm.f)));
    if (a.veh[i + 2] !== b.veh[i + 2]) flags++;
  }
  ok(worst < 0.01, `layers: a dot is ${worst.toFixed(4)} px off the camera's image of its fitted position`);
  ok(flags === 0, `layers: ${flags} inside flags change with the camera`);
  const offCam = await off.evaluate(() => ({ cam: window.busmap.camera, on: window.busmap.config.CAMERA }));
  ok(offCam.cam === null && offCam.on === false, 'layers: camera=0 leaves a camera on');
  console.log(`  layers: ${a.veh.length / 3} dots within ${worst.toFixed(4)} px of z x + e, flags equal, HUD boxes and counts equal`);
  for (const p of [on, off]) {
    if (p.errors.length) failures.push(`layers: page errors ${p.errors.join('; ')}`);
    await p.context().close();
  }
}

// The base map alone (no HUD, trails or dots) at phase u, as luminance.
async function baseAt(h, query, u) {
  const page = await h.open(`${query}&hud=none&trailalpha=0&haloalpha=0&dotcore=0`);
  const lum = await page.evaluate((u) => {
    const bm = window.busmap, c = bm.canvas;
    bm.setCamera(u);
    bm.renderAt(bm.stillTimes().am);
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    const out = new Array(c.width * c.height);
    for (let i = 0, j = 0; i < d.length; i += 4, j++) out[j] = Math.round(0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]);
    return out;
  }, u);
  if (page.errors.length) failures.push(`base at ${u}: page errors ${page.errors.join('; ')}`);
  await page.context().close();
  return lum;
}

async function everyPath(h) {
  const variants = [
    ...['pull-out-east', 'pull-out-north', 'pull-out-west', 'pull-out-south', 'drift-orbit', 'drift-sway'].map((p) => [`campath=${p}`, p]),
    ['trailmode=sprite&colorby=', 'sprite trails'], ['trailscale=0.5&colorby=', 'scaled trails'],
    [`render=${encodeURIComponent(JSON.stringify({ TRAIL_BLEND: 'bounded', ROUTE_BLEND: 'bounded' }))}&colorby=`, 'bounded'],
    ['camzoom=0', 'zoom 0'],
  ];
  const off = await h.open(`${TINY}&camera=0`);
  await off.evaluate(PAGE_HELPERS);
  for (const [q, name] of variants) {
    const page = await h.open(`${TINY}&${q}`);
    const seam = await loopSeam(page);
    const r = await page.evaluate(() => ({ path: window.busmap.config.CAMERA_PATH, u: window.busmap.progressAt(window.busmap.stillTimes().am) }));
    ok(seam.dN0.n === 0, `${name}: frame N differs from frame 0 in ${seam.dN0.n} channels`);
    if (name.startsWith('pull') || name.startsWith('drift')) ok(r.path === name, `${name}: CAMERA_PATH is ${r.path}`);
    if (page.errors.length) failures.push(`${name}: page errors ${page.errors.join('; ')}`);
    await page.context().close();
  }
  await off.context().close();
  // With no zoom every phase has z 1, so frame 0's snap to the cache grid must
  // key on the phase: a quarter of the way round the cached base sits where a
  // fresh vector base does (pinned at frame 0 it was 40 dB off).
  const q0 = `${TINY}&camzoom=0`;
  const a = await baseAt(h, q0, 0.25), b = await baseAt(h, `${q0}&cambase=vector`, 0.25);
  let mse = 0;
  for (let i = 0; i < a.length; i++) mse += (a[i] - b[i]) ** 2;
  const psnr = 10 * Math.log10(255 * 255 / Math.max(mse / a.length, 1e-9));
  ok(psnr >= 50, `zoom 0: the cached base at u = 0.25 is ${psnr.toFixed(1)} dB from the vector base, want 50`);
  console.log(`  every: ${variants.length} paths and trail modes keep the loop; zoom 0 base ${psnr.toFixed(1)} dB from vector at u = 0.25`);
}

// The cached base against a fresh vector render at the same camera, over 12
// consecutive frames of the fastest phase and the 12 after frame 0, at one
// time with the HUD off, so the camera is all that moves.
async function sharpChecks(h, query, tag) {
  const measure = async (base) => {
    const page = await h.open(`${query}&hud=none&cambase=${base}`);
    const r = await page.evaluate(() => {
      const bm = window.busmap, N = bm.totalFrames, W = bm.canvas.width, H = bm.canvas.height;
      const T = bm.stillTimes().am;
      const lum = () => {
        const d = bm.canvas.getContext('2d').getImageData(0, 0, W, H).data;
        const out = new Float32Array(W * H);
        for (let i = 0, j = 0; i < d.length; i += 4, j++) out[j] = 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
        return out;
      };
      // The fastest phase: where the zoom changes most from one frame to the next.
      let fast = 0, best = 0;
      for (let i = 0; i < N; i++) {
        const d = Math.abs(bm.cameraAt((i + 1) / N).zoom - bm.cameraAt(i / N).zoom);
        if (d > best) { best = d; fast = i; }
      }
      const runs = {};
      for (const [name, i0] of [['fast', fast - 6], ['start', 0]]) {
        const seq = [];
        for (let i = i0; i < i0 + 12; i++) {
          bm.setCamera(i / N);
          bm.renderAt(T);
          seq.push(lum());
        }
        runs[name] = seq;
      }
      bm.setCamera(null);
      window.__runs = runs;
      return { fast };
    });
    return { page, ...r };
  };
  const cache = await measure('cache');
  const vector = await measure('vector');
  // Pull both sets into one page to compare; 12 frames of 1080 x 1920 floats
  // each way is too much for one evaluate, so the metrics run frame by frame.
  const stats = {};
  for (const run of ['fast', 'start']) {
    const s = { cache: { flicker: 0, grad: 0 }, vector: { flicker: 0, grad: 0 }, mse: 0 };
    for (const [name, page] of [['cache', cache.page], ['vector', vector.page]]) {
      const r = await page.evaluate((run) => {
        const seq = window.__runs[run], W = 1080, y0 = 300, y1 = 1500;
        let flick = 0, n = 0, grad = 0;
        for (let t = 1; t < seq.length - 1; t++) {
          const a = seq[t - 1], b = seq[t], c = seq[t + 1];
          for (let i = y0 * W; i < y1 * W; i++) { flick += Math.abs(c[i] - 2 * b[i] + a[i]); n++; }
        }
        for (const a of seq) for (let y = y0; y < y1; y++) for (let x = 1; x < W - 1; x++) { const i = y * W + x; grad += Math.abs(a[i + 1] - a[i - 1]) + Math.abs(a[i + W] - a[i - W]); }
        return { flicker: flick / n, grad: grad / seq.length };
      }, run);
      s[name] = r;
    }
    // PSNR of the cached frames against the vector frames, one frame at a time:
    // the vector frame crosses over as base64 bytes, which is quick.
    for (let t = 0; t < 12; t++) {
      const b = await vector.page.evaluate(([run, t]) => {
        const a = window.__runs[run][t].subarray(300 * 1080, 1500 * 1080);
        const u = new Uint8Array(a.length);
        for (let i = 0; i < a.length; i++) u[i] = Math.round(a[i]);
        let s = '';
        for (let i = 0; i < u.length; i += 32768) s += String.fromCharCode.apply(null, u.subarray(i, i + 32768));
        return btoa(s);
      }, [run, t]);
      const e = await cache.page.evaluate(([run, t, b]) => {
        const a = window.__runs[run][t].subarray(300 * 1080, 1500 * 1080);
        const bin = atob(b);
        let e = 0;
        for (let i = 0; i < a.length; i++) { const d = Math.round(a[i]) - bin.charCodeAt(i); e += d * d; }
        return e / a.length;
      }, [run, t, b]);
      s.mse += e / 12;
    }
    s.psnr = 10 * Math.log10(255 * 255 / Math.max(s.mse, 1e-9));
    stats[run] = s;
    ok(s.cache.grad >= 0.85 * s.vector.grad, `${tag} ${run}: the cached base keeps ${(100 * s.cache.grad / s.vector.grad).toFixed(1)}% of the edge energy`);
    ok(s.cache.flicker <= s.vector.flicker * 1.05 + 0.005, `${tag} ${run}: the cached base flickers ${s.cache.flicker.toFixed(4)}, vector ${s.vector.flicker.toFixed(4)}`);
    ok(s.psnr >= 38, `${tag} ${run}: cached base ${s.psnr.toFixed(1)} dB from the vector render`);
    console.log(`  sharp ${tag} ${run.padEnd(5)}: edges ${(100 * s.cache.grad / s.vector.grad).toFixed(1)}% of vector, flicker ${s.cache.flicker.toFixed(4)} `
      + `vs vector ${s.vector.flicker.toFixed(4)}, ${s.psnr.toFixed(1)} dB`);
  }
  for (const p of [cache.page, vector.page]) {
    if (p.errors.length) failures.push(`${tag} sharp: page errors ${p.errors.join('; ')}`);
    await p.context().close();
  }
}

const h = await openBrowser();
try {
  await variantChecks(h, TINY, 'day', 'long');
  await variantChecks(h, TINY_WEEK, 'week', 'long');
  await variantChecks(h, `${TINY}&variant=rush`, 'rush', 'short');
  await layerChecks(h);
  await everyPath(h);
  let sharp = [TINY, 'tiny'];
  for (const id of NO_GTA ? [] : ['gta-toronto', 'gta-markham']) {
    const net = `build/${id}/day/network.json.gz`, bm = `build/${id}/basemap.json.gz`;
    if (fs.existsSync(path.join(ROOT, net)) && fs.existsSync(path.join(ROOT, bm))) { sharp = [`data=../${net}&basemap=../${bm}`, id]; break; }
  }
  await sharpChecks(h, ...sharp);
} finally {
  await h.close();
}
for (const f of failures) console.log(`FAIL ${f}`);
console.log(`v4 camera: ${failures.length} failure(s)`);
process.exitCode = failures.length ? 1 : 0;
