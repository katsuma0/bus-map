// Helpers shared by the capture plug-ins (spec C3) and scripts/render_video.mjs:
// ffmpeg child processes, the lossless intermediate encoder, and the raw
// frame route that pages post their canvas pixels to.
//
// A plug-in, scripts/capture/<name>.mjs, exports
//
//   export default {
//     name: 'raw',
//     maxJobs: 3,              // 1 for page-serial methods
//     async segment(ctx) -> { file, format: 'mkv-rgb' | 'ivf-i420', frames, drawMs, captureMs },
//   };
//
// segment() draws ctx.frames (ascending frame indices) and writes one
// intermediate segment to ctx.outFile, calls ctx.progress(done, T) as frames
// finish and throws on any page error. ctx holds the spec's fields
//   browser, url, frames, outFile, size {width, height}, fps, ffmpeg (path),
//   pageErrors (shared array), progress, keepFrames (Map frame -> PNG path
//   for the lossless review frames, or null)
// and these additions from the driver:
//   page        a loaded page lent for this segment (do not close it), or null
//   openPage()  opens and loads another page with the same error hooks
//   receiver    { register(sink) -> id, unregister(id), url(id) } for raw posts
//   aborted()   true once the run is interrupted or another segment failed

import fs from 'node:fs';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

export const WIDTH = 1080;
export const HEIGHT = 1920;
export const FRAME_ROUTE = '/__frame/';

export const now = () => performance.now();

export function throwIfPageErrors(pageErrors) {
  if (pageErrors.length) throw new Error(`the page reported errors:\n  ${pageErrors.join('\n  ')}`);
}

export function pngFromDataUrl(s) {
  return Buffer.from(s.slice(s.indexOf(',') + 1), 'base64');
}

export function pngSize(buf) {
  if (buf.length < 24 || buf.readUInt32BE(0) !== 0x89504e47) throw new Error('capture did not return a PNG');
  return [buf.readUInt32BE(16), buf.readUInt32BE(20)];
}

// ---------------------------------------------------------------- ffmpeg

// stdin 'pipe' for an encoder fed by this process, 'ignore' for a file to
// file run. CPU time is sampled from /proc so the timing lines can show what
// the encoder cost; it stays 0 where /proc is missing.
export function startFfmpeg(ffmpeg, args, { stdin = 'pipe' } = {}) {
  const proc = spawn(ffmpeg, args, { stdio: [stdin, 'inherit', 'pipe'] });
  const ff = { proc, args, stderr: '', exited: null, stdinError: null, cpuMs: 0, path: ffmpeg };
  proc.stderr.on('data', (d) => { ff.stderr += d; });
  if (proc.stdin) proc.stdin.on('error', (e) => { ff.stdinError = e; });
  const sample = () => {
    try {
      const f = fs.readFileSync(`/proc/${proc.pid}/stat`, 'utf8').split(') ')[1].split(' ');
      ff.cpuMs = (Number(f[11]) + Number(f[12])) * 10;
    } catch { /* gone or not Linux */ }
  };
  const ticker = setInterval(sample, 250);
  ff.done = new Promise((resolve) => {
    proc.on('error', (e) => {
      clearInterval(ticker);
      ff.stderr += `could not start ${ffmpeg}: ${e.message}\n`;
      ff.exited = { code: -1, signal: null };
      resolve(ff.exited);
    });
    proc.on('close', (code, signal) => {
      clearInterval(ticker);
      ff.exited = { code, signal };
      resolve(ff.exited);
    });
  });
  return ff;
}

export function ffmpegFailure(ff) {
  const why = ff.exited ? `exited with code ${ff.exited.code}${ff.exited.signal ? ` (${ff.exited.signal})` : ''}`
    : `stdin error: ${ff.stdinError.message}`;
  return new Error(`ffmpeg ${why}\n${ff.stderr.trim()}`);
}

export async function feedFfmpeg(ff, buf) {
  if (ff.exited || ff.stdinError) throw ffmpegFailure(ff);
  if (!ff.proc.stdin.write(buf)) {
    // Back-pressure: wait for ffmpeg to catch up, unless it dies meanwhile.
    await Promise.race([once(ff.proc.stdin, 'drain').catch(() => {}), ff.done]);
    if (ff.exited || ff.stdinError) throw ffmpegFailure(ff);
  }
}

// Closes stdin (if any), waits, and throws unless ffmpeg exited cleanly.
export async function finishFfmpeg(ff) {
  if (ff.proc.stdin && !ff.proc.stdin.destroyed) ff.proc.stdin.end();
  const exit = await ff.done;
  if (exit.code !== 0) throw ffmpegFailure(ff);
  if (ff.stderr.trim()) console.error(ff.stderr.trim());
  return ff;
}

export function killFfmpeg(ff) {
  if (ff && !ff.exited) {
    if (ff.proc.stdin) ff.proc.stdin.destroy();
    ff.proc.kill('SIGKILL');
  }
}

export async function runFfmpeg(ffmpeg, args) {
  return finishFfmpeg(startFfmpeg(ffmpeg, args, { stdin: 'ignore' }));
}

// The lossless RGB intermediate (C3): x264's RGB encoder at qp 0 keeps every
// pixel of the canvas, so the final encode and its bitrate ladder can run
// from it as often as needed. ultrafast because the file only lives until the
// final is written; the slow preset belongs to the final encode.
// input: 'png' (image2pipe of PNGs) or 'rgba' (raw canvas pixels).
export function intermediateArgs(input, size, fps, out) {
  const inArgs = input === 'png'
    ? ['-f', 'image2pipe', '-vcodec', 'png']
    : ['-f', 'rawvideo', '-pix_fmt', 'rgba', '-video_size', `${size.width}x${size.height}`];
  return [
    '-hide_banner', '-loglevel', 'error', '-y',
    ...inArgs, '-framerate', String(fps), '-i', '-',
    '-c:v', 'libx264rgb', '-qp', '0', '-preset', 'ultrafast', '-pix_fmt', 'rgb24',
    '-f', 'matroska', out,
  ];
}

// --------------------------------------------------------- plug-in glue

// The page a segment draws on: the driver's first page when it lends one
// (ctx.page), else a new one. close() only closes what this opened.
export async function segmentPage(ctx) {
  if (ctx.page) return { page: ctx.page, close: async () => {} };
  const { page } = await ctx.openPage();
  return { page, close: () => page.context().close().catch(() => {}) };
}

export function isAborted(ctx) {
  return Boolean(ctx.aborted && ctx.aborted());
}

// Shared loop of the PNG plug-ins (canvas, screenshot): capture(page, i)
// returns { T, drawMs, png } and keepPng(page, i, png) the exact canvas PNG
// of a frame the review wants.
export async function pngSegment(ctx, capture, keepPng) {
  const { page, close } = await segmentPage(ctx);
  const ff = startFfmpeg(ctx.ffmpeg, intermediateArgs('png', ctx.size, ctx.fps, ctx.outFile));
  let drawMs = 0;
  let captureMs = 0;
  let done = 0;
  try {
    for (const i of ctx.frames) {
      if (isAborted(ctx)) throw new Error('interrupted');
      const a = now();
      let f;
      try {
        f = await capture(page, i);
      } catch (e) {
        throw new Error(`frame ${i} failed: ${e.message}`);
      }
      const b = now();
      const [w, h] = pngSize(f.png);
      if (w !== ctx.size.width || h !== ctx.size.height) {
        throw new Error(`frame ${i} is ${w}x${h}, expected ${ctx.size.width}x${ctx.size.height}`);
      }
      throwIfPageErrors(ctx.pageErrors);
      if (ctx.keepFrames && ctx.keepFrames.has(i)) {
        await fs.promises.writeFile(ctx.keepFrames.get(i), await keepPng(page, i, f.png));
      }
      await feedFfmpeg(ff, f.png);
      drawMs += f.drawMs;
      captureMs += (b - a) - f.drawMs;
      done++;
      ctx.progress(done, f.T);
    }
    await finishFfmpeg(ff);
  } finally {
    killFfmpeg(ff);
    await close();
  }
  return { file: ctx.outFile, format: 'mkv-rgb', frames: done, drawMs, captureMs };
}

// ------------------------------------------------------------ raw frames

// Frames posted by pages: POST /__frame/<sink>/<frame>, body raw RGBA. The
// server hands each one to its sink, which answers only once the frame is in
// its consumer, so a page never runs more than a couple of frames ahead.
export class FrameReceiver {
  constructor() {
    this.sinks = new Map();
    this.next = 0;
  }

  register(sink) {
    const id = this.next++;
    this.sinks.set(id, sink);
    return id;
  }

  unregister(id) {
    this.sinks.delete(id);
  }

  // true when the request was a frame post (answered here), false otherwise.
  handle(req, res) {
    if (!req.url.startsWith(FRAME_ROUTE)) return false;
    const pathname = req.url.split('?')[0];
    const m = /^\/__frame\/(\d+)\/(\d+)$/.exec(pathname);
    const sink = m && this.sinks.get(Number(m[1]));
    if (req.method !== 'POST' || !sink) {
      req.resume();
      reply(res, 404, `no sink for ${pathname}`);
      return true;
    }
    const q = new URL(req.url, 'http://127.0.0.1').searchParams;
    const chunks = [];
    let len = 0;
    req.on('data', (c) => { chunks.push(c); len += c.length; });
    req.on('end', () => sink.accept(Number(m[2]), chunks, len, res, {
      T: Number(q.get('T')), draw: Number(q.get('draw')) || 0, read: Number(q.get('read')) || 0,
    }));
    req.on('error', () => res.destroy());
    return true;
  }
}

export function reply(res, status, text) {
  res.writeHead(status, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end(text);
}

// Writes the frames of one list to a consumer in list order, whatever order
// the posts arrive in. write(chunks) is awaited per frame; onFrame(n, info)
// is called after each.
export class RawSink {
  constructor(frames, bytes, write, onFrame, isAborted = () => false) {
    this.frames = frames;
    this.bytes = bytes;
    this.write = write;
    this.onFrame = onFrame;
    this.isAborted = isAborted;
    this.pos = 0;
    this.pending = new Map();
    this.busy = false;
    this.error = null;
  }

  accept(n, chunks, len, res, timing) {
    if (this.error || this.isAborted()) return reply(res, 500, this.error ? this.error.message : 'interrupted');
    if (len !== this.bytes) return reply(res, 400, `frame ${n} is ${len} bytes, expected ${this.bytes}`);
    this.pending.set(n, { chunks, res, ...timing });
    this.pump();
  }

  async pump() {
    if (this.busy) return;
    this.busy = true;
    try {
      while (this.pos < this.frames.length && this.pending.has(this.frames[this.pos])) {
        const n = this.frames[this.pos];
        const f = this.pending.get(n);
        this.pending.delete(n);
        const t0 = now();
        await this.write(f.chunks);
        this.pos++;
        reply(f.res, 200, 'ok');
        this.onFrame(n, { T: f.T, draw: f.draw, read: f.read, write: now() - t0 });
      }
    } catch (e) {
      this.error = e;
      for (const f of this.pending.values()) reply(f.res, 500, e.message);
      this.pending.clear();
    } finally {
      this.busy = false;
    }
  }
}

// Runs in the page (passed to page.evaluate). Draws each frame, reads the
// canvas back with getImageData and posts it as a Blob, keeping `depth`
// posts in flight so drawing the next frame overlaps the transfer. Frames in
// `keep` are also returned as PNG data URLs (exact canvas pixels).
export async function pageRawLoop({ frames, url, depth, keep }) {
  const bm = window.busmap;
  const c = bm.canvas || document.querySelector('canvas');
  const g = c.getContext('2d');
  const keepSet = new Set(keep || []);
  const pngs = {};
  const post = async (n, body, T, draw, read) => {
    const r = await fetch(`${url}/${n}?T=${T}&draw=${draw.toFixed(2)}&read=${read.toFixed(2)}`, { method: 'POST', body });
    if (!r.ok) throw new Error(`frame ${n}: ${r.status} ${await r.text()}`);
  };
  const inflight = [];
  for (const n of frames) {
    const t0 = performance.now();
    const T = bm.renderFrame(n);
    const t1 = performance.now();
    const body = new Blob([g.getImageData(0, 0, c.width, c.height).data]);
    if (keepSet.has(n)) pngs[n] = c.toDataURL('image/png');
    const t2 = performance.now();
    const p = post(n, body, T, t1 - t0, t2 - t1);
    // Rejections are collected below; this only keeps them from being reported as unhandled meanwhile.
    p.catch(() => {});
    inflight.push(p);
    if (inflight.length >= depth) await inflight.shift();
  }
  await Promise.all(inflight);
  return pngs;
}
