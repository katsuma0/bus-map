#!/usr/bin/env node
// Acceptance tests of the render script (spec F3, part C) that need a browser
// and the v4 renderer (part B):
//
//   C-2  final checks of C4 on a fixture final and on one GTA final
//   C-3  the bitrate ladder steps and records it
//   C-5  stills equal the canvas pixels
//   C-6  capture equivalence on frames 0 and 750 of the Markham day: raw (and
//        canvas, screenshot) exact, webcodecs at least 50 dB
//   C-7  --tier tune --roundtrip writes every file of C5 and config.json
//   C-8  the webcodecs final has no -vf, and its mean U and V are within 1 code
//        value of the canvas final's
//   prev the preview tier (C5): 540x960, 15 fps, every second frame
//
// C-1 is tests/legacy/check.py --part C; C-4 (tool detection on Actions) is
// read from the render job's log ("tools: ... (playwright)"), and its logic is
// unit tested in args.test.mjs.
//
// Inputs, under --root (default: the repo root):
//   tests/fixtures/v4_tiny/{network,week,basemap}.json      (part B's fixture)
//   build/gta-markham/day/network.json.gz + basemap.json.gz (make.py build), else
//   build/stub_v4/gta-markham/day/network.json.gz + data/gta/built/basemap.json.gz
//                                                           (tests/web/stub_gta_v4.py)
// A test whose input is missing is skipped and says so. The GTA final is a
// 90-frame slice around frame 300 unless --full.
//
// Usage: node tests/render/accept.mjs [--root DIR] [--only C-2,C-6] [--full] [--keep]
// Outputs go to build/test_render/.

import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { captureOnly } from '../../scripts/render_video.mjs';
import {
  REPO, canvasAt, canvasFrame, decode, decodePng, meanOf, openBrowser, planes, probe, psnr, rgbDiff, runScript, topBoxes,
} from './lib.mjs';

const args = process.argv.slice(2);
const opt = (name, def) => {
  const k = args.indexOf(`--${name}`);
  return k >= 0 ? args[k + 1] : def;
};
const ROOT = path.resolve(opt('root', REPO));
const ONLY = opt('only', null) ? new Set(opt('only').split(',')) : null;
const FULL = args.includes('--full');
const KEEP = args.includes('--keep');
const OUT = path.join(REPO, 'build', 'test_render');
fs.mkdirSync(OUT, { recursive: true });
os.setPriority(10);

const exists = (p) => fs.existsSync(path.join(ROOT, p));
const TINY = { data: 'tests/fixtures/v4_tiny/network.json', basemap: 'tests/fixtures/v4_tiny/basemap.json' };
const TINY_WEEK = { data: 'tests/fixtures/v4_tiny/week.json', basemap: 'tests/fixtures/v4_tiny/basemap.json' };
const GTA = exists('build/gta-markham/day/network.json.gz')
  ? { data: 'build/gta-markham/day/network.json.gz', basemap: 'build/gta-markham/basemap.json.gz', what: 'make.py build' }
  : { data: 'build/stub_v4/gta-markham/day/network.json.gz', basemap: 'data/gta/built/basemap.json.gz', what: 'stub_gta_v4.py' };
const hasTiny = exists(TINY.data) && exists(TINY.basemap) && fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8').includes('renderFrameV4');
const hasGta = hasTiny && exists(GTA.data) && exists(GTA.basemap);
const q = (inp) => `data=../${inp.data}&basemap=../${inp.basemap}`;
const dataArgs = (inp) => ['--data', inp.data, '--basemap', inp.basemap];

const SIDECAR_KEYS = ['name', 'variant', 'tier', 'frames', 'fps', 'width', 'height', 'duration_s', 'crf', 'kbps',
  'bitrate_floor_met', 'capture', 'ms_per_frame', 'query', 'network_sha256', 'basemap_sha256', 'code_sha256', 'git_commit',
  'chromium', 'ffmpeg', 'runner', 'started_at', 'key'];
const CODE_KEYS = ['app.js', 'index.html', 'color.js', 'themes.json', 'presets/shorts.json', 'render_video.mjs'];

const results = [];
let browser = null;
const sha256 = (f) => crypto.createHash('sha256').update(fs.readFileSync(f)).digest('hex');
const dims = (f) => { const s = probe(f).streams[0]; return `${s.width}x${s.height}`; };

async function page(inp) {
  if (!browser) browser = await openBrowser(ROOT);
  return browser.open(q(inp));
}

function fresh(name) {
  const d = path.join(OUT, name);
  fs.rmSync(d, { recursive: true, force: true });
  fs.mkdirSync(d, { recursive: true });
  return d;
}

// Every C-2 property, read with ffprobe here rather than through the script.
function c2(file, frames, notes) {
  const info = probe(file);
  const v = info.streams.filter((s) => s.codec_type === 'video');
  const s = v[0] || {};
  const want = { width: 1080, height: 1920, r_frame_rate: '30/1', nb_frames: String(frames), profile: 'High', pix_fmt: 'yuv420p',
    color_space: 'bt709', color_transfer: 'bt709', color_primaries: 'bt709', color_range: 'tv' };
  const bad = Object.entries(want).filter(([k, w]) => String(s[k]) !== String(w)).map(([k, w]) => `${k} ${s[k]} (want ${w})`);
  if (v.length !== 1) bad.push(`${v.length} video streams`);
  if (info.streams.some((x) => x.codec_type === 'audio')) bad.push('an audio stream');
  const boxes = topBoxes(file);
  if (!(boxes.indexOf('moov') >= 0 && boxes.indexOf('moov') < boxes.indexOf('mdat'))) bad.push(`boxes ${boxes.join(' ')}`);
  notes.push(`${path.basename(file)}: ${bad.length ? `FAIL ${bad.join(', ')}` : `C-2 ok (${frames} frames, ${Math.round(info.format.bit_rate / 1000)} kbps)`}`);
  return bad.length === 0;
}

function sidecarOk(sc, file, expect, notes) {
  const bad = [];
  const onDisk = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (JSON.stringify(onDisk) !== JSON.stringify(sc)) bad.push('the SIDECAR line differs from the file');
  for (const k of SIDECAR_KEYS) if (!(k in sc)) bad.push(`missing ${k}`);
  for (const k of CODE_KEYS) if (!(k in (sc.code_sha256 || {}))) bad.push(`code_sha256 missing ${k}`);
  for (const [k, w] of Object.entries(expect)) if (JSON.stringify(sc[k]) !== JSON.stringify(w)) bad.push(`${k} ${JSON.stringify(sc[k])} (want ${JSON.stringify(w)})`);
  notes.push(`sidecar ${path.basename(file)}: ${bad.length ? `FAIL ${bad.join('; ')}` : 'every 2.13 key'}`);
  return bad.length === 0;
}

async function samePixels(pngFile, refPng, tmp) {
  const a = decode(pngFile, 'rgb24');
  const b = decodePng(refPng, 'rgb24', [], tmp);
  return rgbDiff(a, b, 1080);
}

// ------------------------------------------------------------------ C-2

async function testC2() {
  const notes = [];
  let ok = true;
  // The fixture final, all 1500 frames, raw on two pages (so the join is in it).
  const d = fresh('c2-tiny');
  const out = path.join(d, 'test-tiny-day.mp4');
  const key = 'a'.repeat(64);
  const r = runScript(['--tier', 'final', ...dataArgs(TINY), '--capture', 'raw', '--jobs', '2', '--out', out,
    '--review-dir', path.join(d, 'review'), '--key', key, '--min-kbps', '0'], { root: ROOT });
  if (r.code !== 0 || !r.sidecar) return { ok: false, notes: [`fixture final failed (exit ${r.code}):\n${r.stderr.slice(-2000)}`] };
  ok = c2(out, 1500, notes) && ok;
  ok = sidecarOk(r.sidecar, path.join(d, 'test-tiny-day.json'), {
    name: 'test-tiny-day', variant: 'day', tier: 'final', frames: 1500, fps: 30, width: 1080, height: 1920, duration_s: 50,
    capture: 'raw', key, network_sha256: sha256(path.join(ROOT, TINY.data)), basemap_sha256: sha256(path.join(ROOT, TINY.basemap)),
    bitrate_floor_met: true, crf: 18,
  }, notes) && ok;
  const review = ['f0000.png', 'f0300.png', 'f1499.png', 'thumb-f0300.jpg'];
  const missing = review.filter((f) => !fs.existsSync(path.join(d, 'review', f)));
  if (missing.length) {
    ok = false;
    notes.push(`FAIL review files missing: ${missing.join(', ')}`);
  } else {
    const thumb = dims(path.join(d, 'review', 'thumb-f0300.jpg'));
    const p = await page(TINY);
    const diff = await samePixels(path.join(d, 'review', 'f0300.png'), await canvasFrame(p, 300), d);
    await p.context().close();
    if (thumb !== '270x480' || diff.count) ok = false;
    notes.push(`review: ${review.join(', ')}; thumb ${thumb}; f0300.png ${diff.count ? `differs from the canvas: ${JSON.stringify(diff)}` : 'is the canvas frame 300'}`);
  }
  if (!KEEP) fs.rmSync(out, { force: true });

  // One GTA final.
  if (!hasGta) {
    notes.push(`GTA final SKIPPED: ${GTA.data} missing under ${ROOT}`);
    return { ok, notes, partial: true };
  }
  const g = fresh('c2-gta');
  const gout = path.join(g, 'gta-markham-day.mp4');
  const slice = FULL ? [] : ['--start', '270', '--end', '360'];
  const n = FULL ? 1500 : 90;
  const gr = runScript(['--tier', 'final', ...dataArgs(GTA), '--capture', 'raw', '--jobs', '2', '--out', gout, ...slice,
    '--review-dir', path.join(g, 'review')], { root: ROOT });
  if (gr.code !== 0 || !gr.sidecar) return { ok: false, notes: [...notes, `GTA final failed (exit ${gr.code}):\n${gr.stderr.slice(-2000)}`] };
  ok = c2(gout, n, notes) && ok;
  ok = sidecarOk(gr.sidecar, path.join(g, 'gta-markham-day.json'), {
    name: 'gta-markham-day', variant: 'day', frames: n, capture: 'raw', key: null, network_sha256: sha256(path.join(ROOT, GTA.data)),
  }, notes) && ok;
  const kept = fs.readdirSync(path.join(g, 'review')).sort();
  notes.push(`GTA (${GTA.what}) ${FULL ? 'full' : 'frames 270..359'}: crf ${gr.sidecar.crf}, ${gr.sidecar.kbps} kbps, floor met ${gr.sidecar.bitrate_floor_met}, ` +
    `${gr.sidecar.ms_per_frame} ms/frame capture; review ${kept.join(', ')}`);
  if (!kept.includes('thumb-f0300.jpg') || !kept.includes(FULL ? 'f1499.png' : 'f0359.png')) ok = false;
  if (!KEEP) fs.rmSync(gout, { force: true });
  return { ok, notes };
}

// ------------------------------------------------------------------ C-3

async function testC3() {
  const notes = [];
  const d = fresh('c3');
  const base = ['--tier', 'final', ...dataArgs(TINY), '--start', '0', '--end', '30'];
  // No CRF can reach this floor: every step is tried and CRF 10 is kept.
  const hi = runScript([...base, '--out', path.join(d, 'hi.mp4'), '--min-kbps', '1000000'], { root: ROOT });
  if (hi.code !== 0 || !hi.sidecar) return { ok: false, notes: [`exit ${hi.code}: ${hi.stderr.slice(-1500)}`] };
  const ladder = hi.sidecar.ladder || [];
  let ok = JSON.stringify(ladder.map((x) => x.crf)) === '[18,16,14,12,10]' && hi.sidecar.crf === 10
    && hi.sidecar.bitrate_floor_met === false && /WARNING: no CRF/.test(hi.stderr)
    && ladder.every((x, k) => k === 0 || x.kbps > ladder[k - 1].kbps);
  notes.push(`floor 1000000: ladder ${ladder.map((x) => `${x.crf}:${x.kbps}`).join(' ')}, kept crf ${hi.sidecar.crf}, ` +
    `bitrate_floor_met ${hi.sidecar.bitrate_floor_met}, warning ${/WARNING: no CRF/.test(hi.stderr)}`);
  // A floor just above CRF 18's bitrate: one step, CRF 16 wins (x264 is deterministic, so the
  // bitrates repeat).
  const floor = ladder[0].kbps + 1;
  const mid = runScript([...base, '--out', path.join(d, 'mid.mp4'), '--min-kbps', String(floor)], { root: ROOT });
  const want = ladder.find((x) => x.kbps >= floor);
  const okMid = mid.code === 0 && mid.sidecar && mid.sidecar.crf === want.crf && mid.sidecar.bitrate_floor_met === true
    && mid.sidecar.ladder.length === ladder.indexOf(want) + 1;
  notes.push(`floor ${floor}: ladder ${mid.sidecar ? mid.sidecar.ladder.map((x) => `${x.crf}:${x.kbps}`).join(' ') : '?'}, ` +
    `kept crf ${mid.sidecar && mid.sidecar.crf} (want ${want.crf}), floor met ${mid.sidecar && mid.sidecar.bitrate_floor_met}`);
  ok = ok && okMid && c2(path.join(d, 'mid.mp4'), 30, notes);
  return { ok, notes };
}

// ------------------------------------------------------------------ C-5

const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
// The file label of a still, written out here rather than imported (C5).
function label(T, week) {
  const s = Math.floor(T);
  const t = week ? s % 86400 : s;
  const hhmm = `${String(Math.floor(t / 3600)).padStart(2, '0')}${String(Math.floor((t % 3600) / 60)).padStart(2, '0')}`;
  return week ? `${DAYS[Math.floor(s / 86400) % 7]}-${hhmm}` : hhmm;
}

// times: seconds as passed with --times, or null for the default stillTimes().
async function stillsCase(inp, stem, { variant = null, times = null, frames = [] }, notes) {
  const d = fresh(`c5-${stem}`);
  const extra = [...(variant ? ['--variant', variant] : []), ...(times ? ['--times', times.join(',')] : []),
    ...(frames.length ? ['--frames', frames.join(',')] : [])];
  const r = runScript(['--tier', 'stills', ...dataArgs(inp), '--out-dir', d, '--name', stem, '--sheet', ...extra], { root: ROOT });
  if (r.code !== 0 || !r.files) {
    notes.push(`${stem}: FAIL exit ${r.code}: ${r.stderr.slice(-1500)}`);
    return false;
  }
  const p = await browser.open(`${q(inp)}${variant ? `&variant=${variant}` : ''}`);
  const { st, week, N } = await p.evaluate(() => ({
    st: window.busmap.stillTimes(), week: window.busmap.meta.timeline.kind === 'week', N: window.busmap.totalFrames,
  }));
  const want = [
    ...(times || Object.values(st)).map((T) => ({ T, file: `${stem}-${label(T, week)}.png` })),
    ...frames.map((f) => (f === 'last' ? N - 1 : f)).map((i) => ({ i, file: `${stem}-f${String(i).padStart(4, '0')}.png` })),
  ];
  const names = r.files.map((f) => path.basename(f));
  let ok = JSON.stringify(names) === JSON.stringify([...want.map((w) => w.file), `${stem}.sheet.jpg`]);
  if (!ok) notes.push(`${stem}: FAIL files ${names.join(', ')} (want ${want.map((w) => w.file).join(', ')}, ${stem}.sheet.jpg)`);
  let same = 0;
  for (const w of want) {
    const file = path.join(d, w.file);
    if (!fs.existsSync(file)) continue;
    const ref = w.i === undefined ? await canvasAt(p, w.T) : await canvasFrame(p, w.i);
    const diff = await samePixels(file, ref, d);
    if (diff.count) {
      ok = false;
      notes.push(`${stem}: FAIL ${w.file} differs from the canvas: ${JSON.stringify(diff)}`);
    } else {
      same++;
    }
  }
  await p.context().close();
  const sd = dims(path.join(d, `${stem}.sheet.jpg`));
  const [sw, sh] = sd.split('x').map(Number);
  if (Math.max(sw, sh) > 1568) ok = false;
  notes.push(`${stem}: ${same} of ${want.length} PNGs equal the canvas pixels (${want.map((w) => w.file.slice(stem.length + 1, -4)).join(' ')}); sheet ${sd}`);
  return ok;
}

async function testC5() {
  const notes = [];
  if (!browser) browser = await openBrowser(ROOT);
  let ok = await stillsCase(TINY, 'tiny-day', { times: [28560, 45000, 95400], frames: [0, 15, 'last'] }, notes);
  ok = await stillsCase(TINY, 'tiny-rush', { variant: 'rush' }, notes) && ok;
  if (exists(TINY_WEEK.data)) ok = await stillsCase(TINY_WEEK, 'tiny-week', { frames: [0] }, notes) && ok;
  if (hasGta) ok = await stillsCase(GTA, 'gta-markham-day', { frames: [0, 750] }, notes) && ok;
  else notes.push(`GTA SKIPPED: ${GTA.data} missing`);
  return { ok, notes, partial: !hasGta };
}

// ------------------------------------------------------------------ C-6

async function testC6() {
  const notes = [];
  if (!hasGta) return { ok: true, skipped: true, notes: [`SKIPPED: ${GTA.data} missing under ${ROOT}`] };
  const d = fresh('c6');
  const p = await page(GTA);
  const ref = { 0: await canvasFrame(p, 0), 750: await canvasFrame(p, 750) };
  await p.context().close();
  const refRgb = { 0: decodePng(ref[0], 'rgb24', [], d), 750: decodePng(ref[750], 'rgb24', [], d) };
  const FRAME = 1080 * 1920 * 3;
  let ok = true;
  // raw on two pages: frame 750 is the first frame of the second page's
  // chunk, as in a 1500-frame final with --jobs 2.
  for (const [cap, jobs] of [['raw', '2'], ['canvas', '1'], ['screenshot', '1']]) {
    const inter = await captureOnly(['--root', ROOT, ...dataArgs(GTA), '--capture', cap, '--jobs', jobs], [0, 750], path.join(d, cap));
    const raw = decode(inter.file, 'rgb24');
    const parts = [];
    for (const [k, f] of [[0, 0], [1, 750]]) {
      const diff = rgbDiff(raw.subarray(k * FRAME, (k + 1) * FRAME), refRgb[f], 1080);
      if (diff.count || raw.length !== 2 * FRAME) ok = false;
      parts.push(`frame ${f} ${diff.count ? `differs ${JSON.stringify(diff)}` : 'exact'}`);
    }
    notes.push(`${cap} (${inter.format}, ${jobs} page${jobs === '1' ? '' : 's'}): ${parts.join(', ')}`);
  }
  const inter = await captureOnly(['--root', ROOT, ...dataArgs(GTA), '--capture', 'webcodecs'], [0, 750], path.join(d, 'webcodecs'));
  const yuv = decode(inter.file, 'yuv420p');
  const parts = [];
  for (const [k, f] of [[0, 0], [1, 750]]) {
    const want = planes(decodePng(ref[f], 'yuv420p', ['-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p'], d), 1080, 1920);
    const got = planes(yuv, 1080, 1920, k);
    const all = psnr(Buffer.concat([got.y, got.u, got.v]), Buffer.concat([want.y, want.u, want.v]));
    const py = psnr(got.y, want.y), pu = psnr(got.u, want.u), pv = psnr(got.v, want.v);
    if (!(all >= 50)) ok = false;
    parts.push(`frame ${f} PSNR ${all.toFixed(2)} dB (Y ${py.toFixed(2)}, U ${pu.toFixed(2)}, V ${pv.toFixed(2)})`);
  }
  notes.push(`webcodecs (${inter.format}): ${parts.join(', ')}; need >= 50 dB`);
  if (!KEEP) fs.rmSync(d, { recursive: true, force: true });
  return { ok, notes };
}

// ------------------------------------------------------------------ C-7

function tuneFiles(dir, st, week, frames, clip, roundtrip) {
  const lab = (T) => label(T, week);
  const want = ['config.json', 'timing.json'];
  for (const T of Object.values(st || {})) want.push(...['still', 'bg', 'vehicles', 'boxes'].map((p) => `${p}-${lab(T)}.${p === 'still' || p === 'bg' ? 'png' : 'json'}`));
  for (const i of frames) {
    const n = String(i).padStart(4, '0');
    want.push(`frame-${n}.png`, `bgframe-${n}.png`, `boxes-f${n}.json`, `vehicles-f${n}.json`);
  }
  for (let k = 0; k < clip; k++) want.push(`clip-${String(k).padStart(3, '0')}.png`);
  if (roundtrip) {
    if (st) want.push(`rt-still-${lab(st.am)}.png`);
    for (let k = 0; k < clip; k++) want.push(`rt-clip-${String(k).padStart(3, '0')}.png`);
  }
  const have = new Set(fs.readdirSync(dir));
  return { missing: want.filter((f) => !have.has(f)), extra: [...have].filter((f) => !want.includes(f)), count: want.length };
}

async function tuneCase(inp, name, extra, frames, clip, roundtrip, notes, withTimes = true) {
  const d = fresh(`c7-${name}`);
  const r = runScript(['--tier', 'tune', ...dataArgs(inp), '--out-dir', d, ...(roundtrip ? ['--roundtrip'] : []), ...extra], { root: ROOT });
  if (r.code !== 0 || !r.files) {
    notes.push(`${name}: FAIL exit ${r.code}: ${r.stderr.slice(-1500)}`);
    return false;
  }
  const v = extra.includes('--variant') ? extra[extra.indexOf('--variant') + 1] : null;
  if (!browser) browser = await openBrowser(ROOT);
  const p = await browser.open(`${q(inp)}${v ? `&variant=${v}` : ''}`);
  const st = withTimes ? await p.evaluate(() => window.busmap.stillTimes()) : null;
  const week = await p.evaluate(() => window.busmap.meta.timeline.kind === 'week');
  await p.context().close();
  const f = tuneFiles(d, st, week, frames, clip, roundtrip);
  const timing = JSON.parse(fs.readFileSync(path.join(d, 'timing.json'), 'utf8'));
  const config = JSON.parse(fs.readFileSync(path.join(d, 'config.json'), 'utf8'));
  const sizes = [];
  if (clip) sizes.push(['clip', dims(path.join(d, 'clip-000.png')), '540x960']);
  if (roundtrip && clip) sizes.push(['rt-clip', dims(path.join(d, 'rt-clip-000.png')), '720x1280']);
  if (roundtrip && st) sizes.push(['rt-still', dims(path.join(d, `rt-still-${label(st.am, week)}.png`)), '720x1280']);
  let jsonOk = true;
  for (const x of fs.readdirSync(d)) {
    if (!/^(vehicles|boxes)-.*\.json$/.test(x)) continue;
    const a = JSON.parse(fs.readFileSync(path.join(d, x), 'utf8'));
    if (!Array.isArray(a)) jsonOk = false;
    else if (x.startsWith('vehicles-') && (a.length % 3 !== 0 || !a.every(Number.isFinite))) jsonOk = false;
    else if (x.startsWith('boxes-') && !a.every((b) => typeof b.name === 'string' && Number.isFinite(b.x0) && Number.isFinite(b.size))) jsonOk = false;
  }
  let ok = f.missing.length === 0 && f.extra.length === 0 && sizes.every((s) => s[1] === s[2]) && jsonOk
    && Number.isFinite(timing.ms_per_frame) && config.HUD_LAYOUT === 'shorts';
  if (extra.includes('--render-json')) {
    for (const [k, val] of Object.entries(JSON.parse(extra[extra.indexOf('--render-json') + 1]))) {
      if (config[k] !== val) {
        ok = false;
        notes.push(`${name}: FAIL config.json ${k} ${config[k]}, want ${val}`);
      }
    }
  }
  notes.push(`${name}: ${f.count} expected files ${f.missing.length ? `MISSING ${f.missing.slice(0, 8).join(', ')}` : 'all present'}` +
    `${f.extra.length ? `, EXTRA ${f.extra.slice(0, 8).join(', ')}` : ''}; ${sizes.map((s) => `${s[0]} ${s[1]}`).join(', ') || 'no clip'}; ` +
    `ms_per_frame ${timing.ms_per_frame}; ${(timing.total_ms / 1000).toFixed(1)} s${jsonOk ? '' : '; FAIL vehicles or boxes JSON'}`);
  if (!KEEP && ok) fs.rmSync(d, { recursive: true, force: true });
  return ok;
}

async function testC7() {
  const notes = [];
  let ok = await tuneCase(TINY, 'tiny-day', ['--frames', '0,15,45', '--render-json', '{"TRAIL_MINUTES":12}'], [0, 15, 45], 90, true, notes);
  ok = await tuneCase(TINY, 'tiny-card-arm', ['--times', 'none', '--frames', '0,15,45', '--clip-frames', '0'], [0, 15, 45], 0, false, notes, false) && ok;
  if (exists(TINY_WEEK.data)) ok = await tuneCase(TINY_WEEK, 'tiny-week', ['--clip-frames', '30'], [], 30, true, notes) && ok;
  if (hasGta) ok = await tuneCase(GTA, 'gta-markham-day', ['--render-json', '{"BUS_HALO_R":12}'], [], 90, true, notes) && ok;
  else notes.push(`GTA SKIPPED: ${GTA.data} missing`);
  return { ok, notes, partial: !hasGta };
}

// ------------------------------------------------------------------ C-8

async function testC8() {
  const notes = [];
  const inp = hasGta ? GTA : TINY;
  const d = fresh('c8');
  const base = ['--tier', 'final', ...dataArgs(inp), '--start', '0', '--end', '30', '--min-kbps', '0'];
  const dry = runScript([...base, '--capture', 'webcodecs', '--dry-run', '--out', path.join(d, 'wc.mp4')], { root: ROOT });
  const noVf = dry.dryrun && !dry.dryrun.ffmpeg.includes('-vf') && !dry.dryrun.ffmpeg.join(' ').includes('out_color_matrix');
  notes.push(`dry run of the webcodecs final: ${noVf ? 'no -vf' : `FAIL ${dry.dryrun ? dry.dryrun.ffmpeg.join(' ') : dry.stderr}`}`);
  const means = {};
  for (const cap of ['webcodecs', 'canvas']) {
    const out = path.join(d, `${cap}.mp4`);
    const r = runScript([...base, '--capture', cap, '--out', out], { root: ROOT });
    if (r.code !== 0) return { ok: false, notes: [...notes, `${cap} final failed: ${r.stderr.slice(-1500)}`] };
    const yuv = decode(out, 'yuv420p');
    const n = yuv.length / (1080 * 1920 * 1.5);
    let u = 0, v = 0;
    for (let k = 0; k < n; k++) {
      const pl = planes(yuv, 1080, 1920, k);
      u += meanOf(pl.u);
      v += meanOf(pl.v);
    }
    means[cap] = { u: u / n, v: v / n, frames: n };
  }
  const du = Math.abs(means.webcodecs.u - means.canvas.u);
  const dv = Math.abs(means.webcodecs.v - means.canvas.v);
  const ok = noVf && du <= 1 && dv <= 1;
  notes.push(`${hasGta ? 'GTA Markham' : 'fixture'} frames 0..29: mean U webcodecs ${means.webcodecs.u.toFixed(3)} canvas ${means.canvas.u.toFixed(3)} ` +
    `(diff ${du.toFixed(3)}), mean V ${means.webcodecs.v.toFixed(3)} / ${means.canvas.v.toFixed(3)} (diff ${dv.toFixed(3)}); need <= 1`);
  if (!KEEP) fs.rmSync(d, { recursive: true, force: true });
  return { ok, notes, partial: !hasGta };
}

// ------------------------------------------------------------------ preview

async function testPreview() {
  const notes = [];
  const d = fresh('preview');
  const out = path.join(d, 'test-tiny-rush.preview.mp4');
  const r = runScript(['--tier', 'preview', ...dataArgs(TINY), '--variant', 'rush', '--out', out], { root: ROOT });
  if (r.code !== 0 || !r.sidecar) return { ok: false, notes: [`exit ${r.code}: ${r.stderr.slice(-1500)}`] };
  const s = probe(out).streams[0];
  const ok = s.width === 540 && s.height === 960 && s.r_frame_rate === '15/1' && s.nb_frames === '375'
    && r.sidecar.tier === 'preview' && fs.existsSync(path.join(d, 'test-tiny-rush.preview.json'));
  notes.push(`rush preview: ${s.width}x${s.height} ${s.r_frame_rate} ${s.nb_frames} frames (want 540x960 15/1 375), ${r.sidecar.kbps} kbps, sidecar ${r.sidecar.name}.preview.json`);
  return { ok, notes };
}

// ------------------------------------------------------------------ main

const TESTS = [['C-2', testC2], ['C-3', testC3], ['C-5', testC5], ['C-6', testC6], ['C-7', testC7], ['C-8', testC8], ['prev', testPreview]];
console.log(`root ${ROOT}; fixture ${hasTiny ? 'yes' : 'MISSING'}; GTA ${hasGta ? `${GTA.data} (${GTA.what})` : 'MISSING'}`);
let failed = 0;
let skipped = 0;
let passed = 0;
try {
  for (const [name, fn] of TESTS) {
    if (ONLY && !ONLY.has(name)) continue;
    if (!hasTiny) {
      console.log(`SKIP ${name}: no v4 renderer or fixture under ${ROOT} (part B)`);
      skipped++;
      continue;
    }
    const t0 = Date.now();
    let r;
    try {
      r = await fn();
    } catch (e) {
      r = { ok: false, notes: [`threw: ${e.stack || e.message}`] };
    }
    const status = r.skipped ? 'SKIP' : r.ok ? (r.partial ? 'PASS (partly skipped)' : 'PASS') : 'FAIL';
    if (!r.ok) failed++;
    else if (r.skipped) skipped++;
    else passed++;
    console.log(`${status} ${name} (${((Date.now() - t0) / 1000).toFixed(0)} s)`);
    for (const n of r.notes) console.log(`     ${n.replace(/\n/g, '\n     ')}`);
    results.push({ name, status, notes: r.notes });
  }
} finally {
  if (browser) await browser.close();
}
fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 1) + '\n');
console.log(`render acceptance: ${passed} passed, ${skipped} skipped, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
