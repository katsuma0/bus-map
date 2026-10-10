// --capture raw (spec C3): the page reads its canvas back with getImageData
// and posts the RGBA bytes to this script's server, which writes them to the
// lossless RGB intermediate as rawvideo, so nothing is PNG encoded or
// decoded. Ported from the faster-capture scout's pageCaptureLoop. The driver
// runs one segment per page on contiguous chunks of the frame range, each
// with its own encoder, and joins the segments afterwards.

import fs from 'node:fs';
import {
  RawSink, feedFfmpeg, finishFfmpeg, intermediateArgs, isAborted, killFfmpeg, pageRawLoop, pngFromDataUrl,
  segmentPage, startFfmpeg, throwIfPageErrors,
} from './common.mjs';

// Posts in flight per page: one being transferred while the next frame draws.
const UPLOAD_DEPTH = 2;

export default {
  name: 'raw',
  maxJobs: 3,
  async segment(ctx) {
    const { frames, size } = ctx;
    const { page, close } = await segmentPage(ctx);
    const ff = startFfmpeg(ctx.ffmpeg, intermediateArgs('rgba', size, ctx.fps, ctx.outFile));
    let done = 0;
    let drawMs = 0;
    let captureMs = 0;
    const sink = new RawSink(frames, size.width * size.height * 4,
      async (chunks) => { for (const c of chunks) await feedFfmpeg(ff, c); },
      (n, f) => {
        done++;
        drawMs += f.draw;
        captureMs += f.read;
        ctx.progress(done, f.T);
      },
      () => isAborted(ctx));
    const id = ctx.receiver.register(sink);
    const range = `frames ${frames[0]}..${frames[frames.length - 1]}`;
    try {
      const keep = ctx.keepFrames ? frames.filter((i) => ctx.keepFrames.has(i)) : [];
      let pngs;
      try {
        pngs = await page.evaluate(pageRawLoop, { frames, url: ctx.receiver.url(id), depth: UPLOAD_DEPTH, keep });
      } catch (e) {
        throw new Error(`${range} failed: ${sink.error ? sink.error.message : e.message}`);
      }
      if (sink.error) throw new Error(`${range} failed: ${sink.error.message}`);
      throwIfPageErrors(ctx.pageErrors);
      if (done !== frames.length) throw new Error(`${range}: ${done} of ${frames.length} frames reached the encoder`);
      for (const i of keep) await fs.promises.writeFile(ctx.keepFrames.get(i), pngFromDataUrl(pngs[i]));
      await finishFfmpeg(ff);
    } finally {
      ctx.receiver.unregister(id);
      killFfmpeg(ff);
      await close();
    }
    return { file: ctx.outFile, format: 'mkv-rgb', frames: done, drawMs, captureMs };
  },
};
