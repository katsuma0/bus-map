// Unit tests of scripts/render_video.mjs without a browser: options, the page
// query, tool resolution (C2), the ffmpeg arguments (C4, C5), names and
// layouts, the final checks (C-2) and --dry-run.
//
//   node --test tests/render/args.test.mjs

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import zlib from 'node:zlib';

import {
  UsageError, buildQuery, checkFinal, defaultCapture, finalArgs, legacyFfmpegArgs, mp4TopBoxes, parseArgs,
  parseTime, parseTimes, previewArgs, readNetworkMeta, resolveNames, resolveTools, roundtripArgs, sheetLayout,
  splitFrames, timeLabel,
} from '../../scripts/render_video.mjs';
import { ivfFrames, ivfHeader } from '../../scripts/capture/webcodecs.mjs';
import { intermediateArgs } from '../../scripts/capture/common.mjs';
import { REPO, runScript } from './lib.mjs';

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'render-args-'));
const NO_DEFAULTS = { defaultsFile: path.join(tmp, 'missing.json') };
const tier = (t, ...rest) => parseArgs(['--tier', t, ...rest], NO_DEFAULTS);

// ------------------------------------------------------------- legacy

test('no --tier keeps every default of today', () => {
  const o = parseArgs([]);
  assert.equal(o.capture, 'screenshot');
  assert.equal(o.crf, 17);
  assert.equal(o.preset, 'slow');
  assert.equal(o.fps, 30);
  assert.equal(o.start, 0);
  assert.equal(o.end, null);
  assert.equal(o.out, path.join(REPO, 'out', 'tsukuba-buses.mp4'));
  assert.equal(o.pngEvery, 0);
  assert.equal(o.pngDir, path.join(REPO, 'out', 'frames'));
  assert.equal(parseArgs(['--city', 'gta']).out, path.join(REPO, 'out', 'gta.mp4'));
  assert.equal(parseArgs(['--png-dir', '/tmp/p']).pngEvery, 60);
});

test('no --tier: the ffmpeg arguments are exactly those of 302d811', () => {
  const o = parseArgs(['--city', 'gta', '--out', '/tmp/x.mp4']);
  assert.deepEqual(legacyFfmpegArgs(o), [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'image2pipe', '-vcodec', 'png', '-framerate', '30', '-i', '-',
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '17',
    '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p',
    '-pix_fmt', 'yuv420p',
    '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
    '-movflags', '+faststart', '/tmp/x.mp4',
  ]);
});

test('no --tier: the page query is today\'s', () => {
  assert.equal(buildQuery(parseArgs([]), true), 'record=1');
  assert.equal(buildQuery(parseArgs(['--city', 'gta', '--query', '?osm=1']), true), 'record=1&city=gta&osm=1');
  assert.equal(buildQuery(parseArgs(['--city', 'gta']), false), 'city=gta');
});

test('no --tier: raw, webcodecs and the tier options are refused', () => {
  assert.throws(() => parseArgs(['--capture', 'raw']), UsageError);
  assert.throws(() => parseArgs(['--capture', 'webcodecs']), UsageError);
  for (const a of [['--times', '7:00'], ['--sheet'], ['--review-dir', 'x'], ['--min-kbps', '1'], ['--jobs', '2'], ['--name', 'x']]) {
    assert.throws(() => parseArgs(a), UsageError, a.join(' '));
  }
  assert.throws(() => parseArgs(['--capture', 'gif']), UsageError);
  assert.throws(() => parseArgs(['--bogus']), UsageError);
});

// ------------------------------------------------------------- tiers

test('times: H:MM past midnight, seconds, none', () => {
  assert.equal(parseTime('7:56'), 28560);
  assert.equal(parseTime('26:30'), 95400);
  assert.equal(parseTime('133:00'), 478800);
  assert.equal(parseTime('3600'), 3600);
  assert.deepEqual(parseTimes('7:56, 26:30,100'), [28560, 95400, 100]);
  assert.deepEqual(parseTimes('none'), []);
  assert.throws(() => parseTime('7:5'), UsageError);
  assert.throws(() => parseTime('noon'), UsageError);
  assert.deepEqual(tier('stills', '--frames', '0,last,0,15').frames, [0, 'last', 15]);
  assert.throws(() => tier('stills', '--frames', '1,first'), UsageError);
});

test('each tier takes only its own options', () => {
  assert.throws(() => tier('final', '--sheet'), UsageError);
  assert.throws(() => tier('final', '--crf', '20'), UsageError);
  assert.throws(() => tier('final', '--fps', '25'), UsageError);
  assert.throws(() => tier('stills', '--min-kbps', '1'), UsageError);
  assert.throws(() => tier('stills', '--out', 'x.mp4'), UsageError);
  assert.throws(() => tier('preview', '--review-dir', 'x'), UsageError);
  assert.throws(() => tier('tune', '--times', '7:00'), /needs --out-dir/);
  assert.throws(() => tier('bogus'), UsageError);
  assert.throws(() => tier('final', '--keep-frames', '0'), /needs --review-dir/);
  // A caller may pass its capture defaults to every tier.
  assert.equal(tier('stills', '--capture', 'raw', '--jobs', '2').capture, 'raw');
  assert.equal(tier('tune', '--out-dir', 'x', '--clip-frames', '0').clipFrames, 0);
});

test('final defaults (C3, C4, C6)', () => {
  const o = tier('final');
  assert.equal(o.capture, 'canvas');
  assert.deepEqual(o.crfLadder, [18, 16, 14, 12, 10]);
  assert.equal(o.minKbps, 8000);
  assert.equal(o.preset, 'slow');
  assert.equal(o.jobs, Math.max(1, Math.min(3, os.availableParallelism() - 1)));
  assert.equal(o.keepFrames, null);
  assert.deepEqual(tier('final', '--review-dir', 'r').keepFrames, [0, 300, 'last']);
  assert.deepEqual(tier('final', '--crf-ladder', '20,12').crfLadder, [20, 12]);
  assert.throws(() => tier('final', '--crf-ladder', '18,60'), UsageError);
  assert.equal(tier('final', '--key', 'ABCDEF01').key, 'abcdef01');
  assert.throws(() => tier('final', '--key', 'xyz'), UsageError);
});

test('the capture default comes from cities/defaults.json', () => {
  const f = path.join(tmp, 'defaults.json');
  assert.equal(defaultCapture(path.join(tmp, 'none.json')), 'canvas');
  fs.writeFileSync(f, JSON.stringify({ render: { capture: 'raw', jobs: 1 } }));
  assert.equal(defaultCapture(f), 'raw');
  assert.equal(parseArgs(['--tier', 'final'], { defaultsFile: f }).capture, 'raw');
  assert.equal(parseArgs(['--tier', 'final', '--capture', 'webcodecs'], { defaultsFile: f }).capture, 'webcodecs');
  fs.writeFileSync(f, JSON.stringify({ render: {} }));
  assert.equal(defaultCapture(f), 'canvas');
  fs.writeFileSync(f, JSON.stringify({ render: { capture: 'gif' } }));
  assert.throws(() => defaultCapture(f), UsageError);
});

test('page query additions (2.12), in order and encoded', () => {
  const o = tier('final', '--data', 'build/gta-markham/day/network.json.gz', '--basemap', './build/gta-markham/basemap.json.gz',
    '--variant', 'rush', '--render-json', '{"CARD_LINES":1,"FRAME_ZOOM":1.05}', '--brandhex', 'brampton:zum:e31837;ttc:ed1c24',
    '--query', 'safe=1');
  assert.equal(buildQuery(o, true), 'record=1&data=../build/gta-markham/day/network.json.gz'
    + '&basemap=../build/gta-markham/basemap.json.gz&variant=rush'
    + `&render=${encodeURIComponent('{"CARD_LINES":1,"FRAME_ZOOM":1.05}')}`
    + `&brandhex=${encodeURIComponent('brampton:zum:e31837;ttc:ed1c24')}&safe=1`);
  // What the page reads back with URLSearchParams.
  const q = new URLSearchParams(buildQuery(o, true));
  assert.deepEqual(JSON.parse(q.get('render')), { CARD_LINES: 1, FRAME_ZOOM: 1.05 });
  assert.equal(q.get('brandhex'), 'brampton:zum:e31837;ttc:ed1c24');
  assert.equal(q.get('data'), '../build/gta-markham/day/network.json.gz');
  assert.throws(() => tier('final', '--render-json', '[1]'), UsageError);
  assert.throws(() => tier('final', '--render-json', '{bad'), UsageError);
  assert.throws(() => tier('final', '--brandhex', 'ttc:ed1c2'), UsageError);
  assert.throws(() => tier('final', '--brandhex', 'ttc'), UsageError);
  assert.throws(() => tier('final', '--data', '/abs/network.json'), UsageError);
  assert.throws(() => tier('final', '--data', 'build/../../etc/passwd'), UsageError);
  assert.throws(() => tier('final', '--variant', 'Day!'), UsageError);
  assert.throws(() => tier('final', '--name', 'a/b'), UsageError);
});

// ------------------------------------------------------------- tools (C2, C-4)

test('tool paths: env, then the sandbox path, then Playwright or PATH', () => {
  const has = (...paths) => (p) => paths.includes(p);
  let calls = 0;
  const pw = () => { calls++; return '/home/runner/.cache/ms-playwright/chromium-1194/chrome-linux/chrome'; };
  // This machine: the first existing choice is today's path, and Playwright is never asked.
  let t = resolveTools({ env: { PATH: '/usr/bin' }, exists: has('/opt/pw-browsers/chromium', '/usr/bin/ffmpeg', '/usr/bin/ffprobe'), pwPath: pw });
  assert.deepEqual(t.chromium, { path: '/opt/pw-browsers/chromium', how: 'default' });
  assert.deepEqual(t.ffmpeg, { path: '/usr/bin/ffmpeg', how: 'default' });
  assert.equal(calls, 0);
  // Actions after playwright install and apt-get, no environment variables.
  t = resolveTools({ env: { PATH: '/usr/local/bin:/usr/bin' },
    exists: has('/home/runner/.cache/ms-playwright/chromium-1194/chrome-linux/chrome', '/usr/bin/ffmpeg', '/usr/bin/ffprobe'), pwPath: pw });
  assert.deepEqual(t.chromium, { path: '/home/runner/.cache/ms-playwright/chromium-1194/chrome-linux/chrome', how: 'playwright' });
  assert.equal(t.ffprobe.how, 'default');
  // Environment variables win; PATH is the last resort.
  t = resolveTools({ env: { PLAYWRIGHT_CHROMIUM: '/c', FFMPEG: '/f', PATH: '/a:/b' }, exists: has('/c', '/f', '/b/ffprobe') });
  assert.deepEqual([t.chromium.how, t.ffmpeg.how, t.ffprobe.path], ['$PLAYWRIGHT_CHROMIUM', '$FFMPEG', '/b/ffprobe']);
  // A miss names every path tried.
  assert.throws(() => resolveTools({ env: { PATH: '/a' }, exists: () => false, pwPath: () => { throw new Error('no browsers'); } }),
    (e) => /chromium not found; tried \/opt\/pw-browsers\/chromium, playwright \(no browsers\)/.test(e.message)
      && /ffmpeg not found; tried \/usr\/bin\/ffmpeg, \/a\/ffmpeg/.test(e.message) && /ffprobe not found/.test(e.message));
});

// ------------------------------------------------------------- ffmpeg arguments

test('final encode arguments are C4 exactly; the IVF gets no filter', () => {
  const tail = ['-c:v', 'libx264', '-profile:v', 'high', '-level:v', '4.1', '-preset', 'slow', '-crf', '16',
    '-g', '15', '-bf', '2', '-flags', '+cgop', '-x264-params', 'keyint=15:min-keyint=15:scenecut=0',
    '-pix_fmt', 'yuv420p', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
    '-r', '30', '-an', '-movflags', '+faststart', '-f', 'mp4', 'o.mp4'];
  assert.deepEqual(finalArgs('i.mkv', 'mkv-rgb', 16, 'slow', 'o.mp4'),
    ['-hide_banner', '-loglevel', 'error', '-y', '-i', 'i.mkv', '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p', ...tail]);
  const ivf = finalArgs('i.ivf', 'ivf-i420', 16, 'slow', 'o.mp4');
  assert.deepEqual(ivf, ['-hide_banner', '-loglevel', 'error', '-y', '-i', 'i.ivf', ...tail]);
  assert.ok(!ivf.includes('-vf') && !ivf.join(' ').includes('out_color_matrix'), 'C-8: no -vf for the IVF');
});

test('preview, round trip and intermediate arguments (C3, C5)', () => {
  const p = previewArgs('i.mkv', 'mkv-rgb', 'o.mp4');
  const vf = p[p.indexOf('-vf') + 1];
  assert.equal(vf, 'scale=540:960:flags=area,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p');
  assert.deepEqual(p.slice(p.indexOf('-c:v'), p.indexOf('-c:v') + 6), ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26']);
  assert.equal(p[p.indexOf('-r') + 1], '15');
  assert.ok(!previewArgs('i.ivf', 'ivf-i420', 'o.mp4').join(' ').includes('out_color_matrix'));
  const rt = roundtripArgs(['-i', 'x.png'], 'o.webm').join(' ');
  assert.ok(rt.includes('-vf scale=720:1280:flags=area -c:v libvpx-vp9 -b:v 1500k -row-mt 1'));
  const ia = intermediateArgs('rgba', { width: 1080, height: 1920 }, 30, 's.mkv').join(' ');
  assert.ok(ia.includes('-f rawvideo -pix_fmt rgba -video_size 1080x1920 -framerate 30 -i -'));
  assert.ok(ia.includes('-c:v libx264rgb -qp 0'), 'lossless RGB intermediate');
  assert.ok(intermediateArgs('png', { width: 1080, height: 1920 }, 30, 's.mkv').join(' ').includes('-f image2pipe -vcodec png'));
});

// ------------------------------------------------------------- names and layouts

test('still labels: hhmm, past midnight, week days', () => {
  assert.equal(timeLabel(28860, false), '0801');
  assert.equal(timeLabel(95400, false), '2630');
  assert.equal(timeLabel(45000.7, false), '1230');
  assert.equal(timeLabel(28860, true), 'mon-0801');
  assert.equal(timeLabel(6 * 86400 + 82800, true), 'sun-2300');
  assert.equal(timeLabel(2 * 86400 + 45000, true), 'wed-1230');
  assert.equal(timeLabel(604800 + 10800, true), 'mon-0300');
});

test('sheet: 270x480 tiles, five across, never past 1568 px', () => {
  assert.deepEqual(sheetLayout(5), { cols: 5, rows: 1, tw: 270, th: 480, width: 1350, height: 480 });
  assert.deepEqual(sheetLayout(3), { cols: 3, rows: 1, tw: 270, th: 480, width: 810, height: 480 });
  assert.deepEqual(sheetLayout(7), { cols: 5, rows: 2, tw: 270, th: 480, width: 1350, height: 960 });
  for (let n = 1; n <= 40; n++) {
    const L = sheetLayout(n);
    assert.ok(Math.max(L.width, L.height) <= 1568 && L.cols * L.rows >= n, `${n}: ${JSON.stringify(L)}`);
  }
});

test('contiguous chunks per page', () => {
  const f = Array.from({ length: 1500 }, (_, i) => i);
  const c = splitFrames(f, 2);
  assert.equal(c.length, 2);
  assert.deepEqual([c[0][0], c[0][749], c[1][0], c[1][749]], [0, 749, 750, 1499]);
  assert.deepEqual(splitFrames([0, 750], 2), [[0], [750]]);
  assert.deepEqual(splitFrames([5], 3), [[5]]);
  assert.equal(splitFrames(f, 3).map((x) => x.length).join(), '500,500,500');
});

test('output names: --name, --out, else <id>-<variant>', () => {
  const info = { id: 'gta-markham', batch: 'gta', variant: 'day' };
  let n = resolveNames(tier('final'), info);
  assert.equal(n.stem, 'gta-markham-day');
  assert.equal(n.out, path.join(REPO, 'out', 'shorts', 'gta', 'gta-markham-day.mp4'));
  assert.equal(n.sidecar, path.join(REPO, 'out', 'shorts', 'gta', 'gta-markham-day.json'));
  n = resolveNames(tier('final', '--out', '/x/y/z.mp4'), info);
  assert.deepEqual([n.stem, n.out, n.sidecar], ['z', '/x/y/z.mp4', '/x/y/z.json']);
  n = resolveNames(tier('preview'), { ...info, variant: 'rush' });
  assert.equal(n.out, path.join(REPO, 'out', 'shorts', 'gta', 'gta-markham-rush.preview.mp4'));
  assert.equal(n.sidecar, path.join(REPO, 'out', 'shorts', 'gta', 'gta-markham-rush.preview.json'));
  assert.equal(resolveNames(tier('preview', '--out', '/p/a-day.preview.mp4'), info).stem, 'a-day');
  assert.equal(resolveNames(tier('final', '--out', '/p/a.mp4', '--name', 'b'), info).stem, 'b');
  assert.equal(resolveNames(tier('stills'), info).dir, path.join(REPO, 'build', 'gta-markham', 'stills'));
  assert.equal(resolveNames(tier('stills', '--out-dir', '/s'), info).dir, '/s');
});

// ------------------------------------------------------------- checks (C-2)

const GOOD = {
  streams: [{ codec_type: 'video', width: 1080, height: 1920, r_frame_rate: '30/1', nb_frames: '1500', profile: 'High',
    pix_fmt: 'yuv420p', color_space: 'bt709', color_transfer: 'bt709', color_primaries: 'bt709', color_range: 'tv' }],
  format: {},
};
const WANT = { width: 1080, height: 1920, fps: 30, frames: 1500, profile: 'High' };

test('final checks accept a good MP4 and name each fault', () => {
  assert.deepEqual(checkFinal(GOOD, ['ftyp', 'moov', 'mdat'], WANT), []);
  const bad = structuredClone(GOOD);
  Object.assign(bad.streams[0], { profile: 'Main', color_range: 'pc', nb_frames: '1499', color_space: 'smpte170m' });
  bad.streams.push({ codec_type: 'audio' });
  const p = checkFinal(bad, ['ftyp', 'mdat', 'moov'], WANT).join('\n');
  for (const s of ['profile is Main', 'color_range is pc', 'nb_frames is 1499', 'color_space is smpte170m', '1 audio stream', 'moov is not before mdat']) {
    assert.ok(p.includes(s), `${s} in\n${p}`);
  }
});

test('top-level MP4 boxes, including 64-bit sizes', () => {
  const box = (type, len) => { const b = Buffer.alloc(len); b.writeUInt32BE(len, 0); b.write(type, 4, 'latin1'); return b; };
  const big = Buffer.alloc(24);
  big.writeUInt32BE(1, 0);
  big.write('mdat', 4, 'latin1');
  big.writeBigUInt64BE(24n, 8);
  const f = path.join(tmp, 'boxes.mp4');
  fs.writeFileSync(f, Buffer.concat([box('ftyp', 24), box('moov', 100), big, box('free', 8)]));
  assert.deepEqual(mp4TopBoxes(f), ['ftyp', 'moov', 'mdat', 'free']);
});

// ------------------------------------------------------------- IVF, meta, dry run

test('IVF header and frame records', () => {
  const h = ivfHeader(1080, 1920, 30, 7);
  assert.equal(h.toString('latin1', 0, 4), 'DKIF');
  assert.equal(h.toString('latin1', 8, 12), 'VP90');
  assert.deepEqual([h.readUInt16LE(12), h.readUInt16LE(14), h.readUInt32LE(16), h.readUInt32LE(20), h.readUInt32LE(24)], [1080, 1920, 30, 1, 7]);
  const chunk = (n, key) => { const b = Buffer.alloc(5 + n, 9); b.writeUInt32LE(n, 0); b[4] = key; return b; };
  const counter = { n: 5 };
  const out = ivfFrames(Buffer.concat([chunk(3, 1), chunk(2, 0)]).toString('base64'), counter);
  assert.equal(counter.n, 7);
  assert.equal(out.length, 12 + 3 + 12 + 2);
  assert.deepEqual([out.readUInt32LE(0), Number(out.readBigUInt64LE(4)), out.readUInt32LE(15), Number(out.readBigUInt64LE(19))], [3, 5, 2, 6]);
});

test('network meta from the head of a gzip file', async () => {
  const net = { meta: { id: 'x-y', batch: 'b', note: 'a "quoted} brace', variants: { rush: {}, day: {} } }, routes: [{ id: 'r' }] };
  const f = path.join(tmp, 'n.json.gz');
  fs.writeFileSync(f, zlib.gzipSync(JSON.stringify(net)));
  assert.deepEqual(await readNetworkMeta(f), net.meta);
  fs.writeFileSync(path.join(tmp, 'n.json'), JSON.stringify(net));
  assert.deepEqual(await readNetworkMeta(path.join(tmp, 'n.json')), net.meta);
  fs.writeFileSync(path.join(tmp, 'bad.json'), '{"routes": []}');
  await assert.rejects(readNetworkMeta(path.join(tmp, 'bad.json')), /no complete "meta"/);
});

test('--dry-run prints the URL and the final arguments, renders nothing', () => {
  const root = path.join(tmp, 'root');
  fs.mkdirSync(path.join(root, 'build', 'gta-markham', 'day'), { recursive: true });
  fs.writeFileSync(path.join(root, 'build', 'gta-markham', 'day', 'network.json.gz'), zlib.gzipSync(JSON.stringify({
    meta: { schema: 4, id: 'gta-markham', batch: 'gta', variants: { day: { render: { DURATION_FRAMES: 1500 } }, rush: { render: { DURATION_FRAMES: 750 } } } },
  })));
  const base = ['--tier', 'final', '--dry-run', '--data', 'build/gta-markham/day/network.json.gz', '--basemap', 'build/gta-markham/basemap.json.gz'];
  let r = runScript([...base, '--capture', 'webcodecs', '--key', 'ab12'], { root });
  assert.equal(r.code, 0, r.stderr);
  assert.ok(r.dryrun, r.stdout);
  assert.equal(r.dryrun.url, 'http://127.0.0.1:PORT/web/index.html?record=1&data=../build/gta-markham/day/network.json.gz&basemap=../build/gta-markham/basemap.json.gz');
  assert.equal(r.dryrun.name, 'gta-markham-day');
  assert.equal(r.dryrun.frames, 1500);
  assert.equal(r.dryrun.out, path.join(REPO, 'out', 'shorts', 'gta', 'gta-markham-day.mp4'));
  assert.ok(!r.dryrun.ffmpeg.includes('-vf'), 'C-8: the webcodecs final has no -vf');
  assert.equal(r.dryrun.ffmpeg[r.dryrun.ffmpeg.indexOf('-crf') + 1], '18');
  r = runScript([...base, '--variant', 'rush', '--capture', 'raw', '--jobs', '2'], { root });
  assert.equal(r.dryrun.name, 'gta-markham-rush');
  assert.equal(r.dryrun.frames, 750);
  assert.equal(r.dryrun.jobs, 2);
  assert.ok(r.dryrun.ffmpeg.includes('scale=out_color_matrix=bt709:out_range=tv,format=yuv420p'));
  assert.ok(r.dryrun.intermediate_ffmpeg.join(' ').includes('-pix_fmt rgba'));
  r = runScript(['--dry-run', '--city', 'gta']);
  assert.equal(r.code, 0, r.stderr);
  assert.equal(r.dryrun.query, 'record=1&city=gta');
  assert.deepEqual(r.dryrun.ffmpeg.slice(1), legacyFfmpegArgs(parseArgs(['--city', 'gta'])));
  assert.equal(runScript(['--tier', 'final', '--sheet']).code, 2);
});

test('a call through a symlinked directory still runs the script', () => {
  const link = path.join(tmp, 'repolink');
  fs.symlinkSync(REPO, link);
  const script = path.join(link, 'scripts', 'render_video.mjs');
  let r = spawnSync(process.execPath, [script, '--capture', 'bogus'], { encoding: 'utf8' });
  assert.equal(r.status, 2, r.stderr);
  assert.match(r.stderr, /--capture must be/);
  r = spawnSync(process.execPath, [script, '--dry-run', '--city', 'gta'], { encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /^DRYRUN /m);
});
