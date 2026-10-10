// --capture webcodecs (spec C3): the page encodes VP9 at quantizer 0 in
// BT.709 limited-range I420 (page_encoder.js) and hands the chunks back in
// batches; this side writes them as an IVF file, the intermediate. The IVF
// carries no colour tags, so the final encode must not run it through a
// scale filter (C4).

import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isAborted, pngFromDataUrl, segmentPage, throwIfPageErrors } from './common.mjs';

const PAGE_ENCODER = path.join(path.dirname(fileURLToPath(import.meta.url)), 'page_encoder.js');
// Frames per page.evaluate: enough to amortise the round trip, few enough
// that the chunks of one batch stay a few MB of base64.
const BATCH = 15;
const KEY_EVERY = 300;

export function ivfHeader(w, h, fps, count) {
  const b = Buffer.alloc(32);
  b.write('DKIF', 0);
  b.writeUInt16LE(0, 4);
  b.writeUInt16LE(32, 6);
  b.write('VP90', 8);
  b.writeUInt16LE(w, 12);
  b.writeUInt16LE(h, 14);
  b.writeUInt32LE(fps, 16); // time base fps/1: one tick per frame
  b.writeUInt32LE(1, 20);
  b.writeUInt32LE(count, 24);
  return b;
}

// Base64 batch from pageEncoder -> IVF frame records, numbered from counter.n.
export function ivfFrames(b64, counter) {
  const buf = Buffer.from(b64, 'base64');
  const parts = [];
  for (let o = 0; o < buf.length;) {
    const len = buf.readUInt32LE(o);
    const head = Buffer.alloc(12);
    head.writeUInt32LE(len, 0);
    head.writeBigUInt64LE(BigInt(counter.n++), 4);
    parts.push(head, buf.subarray(o + 5, o + 5 + len));
    o += 5 + len;
  }
  return Buffer.concat(parts);
}

export default {
  name: 'webcodecs',
  maxJobs: 1,
  async segment(ctx) {
    const { frames, size } = ctx;
    const { page, close } = await segmentPage(ctx);
    const fh = await fsp.open(ctx.outFile, 'w');
    const counter = { n: 0 };
    let drawMs = 0;
    let captureMs = 0;
    let done = 0;
    try {
      await page.addScriptTag({ content: await fsp.readFile(PAGE_ENCODER, 'utf8') });
      const config = await page.evaluate((o) => window.pageEncoder.configure(o),
        { quantizer: 0, fps: ctx.fps, keyEvery: KEY_EVERY });
      if (config.width !== size.width || config.height !== size.height) {
        throw new Error(`canvas is ${config.width}x${config.height}, expected ${size.width}x${size.height}`);
      }
      throwIfPageErrors(ctx.pageErrors);
      await fh.write(ivfHeader(size.width, size.height, ctx.fps, 0));
      for (let k = 0; k < frames.length; k += BATCH) {
        if (isAborted(ctx)) throw new Error('interrupted');
        const batch = frames.slice(k, k + BATCH);
        const keep = ctx.keepFrames ? batch.filter((i) => ctx.keepFrames.has(i)) : [];
        const a = performance.now();
        let r;
        try {
          r = await page.evaluate(([f, kf]) => window.pageEncoder.encodeFrames(f, kf), [batch, keep]);
        } catch (e) {
          throw new Error(`frames ${batch[0]}..${batch[batch.length - 1]} failed: ${e.message}`);
        }
        const wall = performance.now() - a;
        throwIfPageErrors(ctx.pageErrors);
        await fh.write(ivfFrames(r.b64, counter));
        for (const i of keep) await fs.promises.writeFile(ctx.keepFrames.get(i), pngFromDataUrl(r.pngs[i]));
        drawMs += r.draw;
        captureMs += wall - r.draw;
        done += batch.length;
        ctx.progress(done, r.T);
      }
      const last = await page.evaluate(() => window.pageEncoder.finish());
      throwIfPageErrors(ctx.pageErrors);
      if (last.n) await fh.write(ivfFrames(last.b64, counter));
      if (counter.n !== frames.length) throw new Error(`the page encoder returned ${counter.n} chunks for ${frames.length} frames`);
      // The frame count in the header, now that it is known; ffmpeg does not
      // need it, other IVF readers do.
      await fh.write(ivfHeader(size.width, size.height, ctx.fps, counter.n), 0, 32, 0);
    } finally {
      await fh.close();
      await close();
    }
    return { file: ctx.outFile, format: 'ivf-i420', frames: done, drawMs, captureMs };
  },
};
