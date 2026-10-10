// Page side of render_video.mjs --capture webcodecs (spec C3), injected with
// page.addScriptTag after busmap.ready. Ported from the in-page-encode scout.
//
// The main thread draws a frame, snapshots the canvas as a VideoFrame and
// hands it to a worker; the worker reads the pixels back, converts them to
// I420 with the BT.709 limited range matrix the ffmpeg path uses, and feeds a
// WebCodecs VideoEncoder (VP9; this Chromium has no H.264 encoder). Encoded
// chunks travel back to Node in batches as one base64 string. Drawing frame
// i+1 overlaps with converting and encoding frame i, and nothing of the size
// of a raw frame crosses the DevTools pipe.
//
// Chromium would convert an RGB VideoFrame itself, but with BT.601 weights
// (the encoder tags its output smpte170m), and fixing that later in ffmpeg
// costs a requantisation. Converting here keeps the luma within about
// 70 dB PSNR of the PNG path. The stream carries no colour tags, which is why
// the final encode reads it without a scale filter (spec C4).
(() => {
  'use strict';

  function workerMain() {
    // BT.709, limited range, 16.16 fixed point. Luma weights are Kr, Kg, Kb
    // times 219/255; chroma rows times 224/255 and each sums to zero so a grey
    // pixel lands on 128 exactly. Chroma is the 2x2 box average.
    const YR = 11966, YG = 40254, YB = 4064;
    const UR = -6596, UG = -22189, UB = 28785;
    const VR = 28785, VG = -26145, VB = -2640;
    const YOFF = (16 << 16) + 32768;
    const COFF = ((128 << 16) + 32768) * 4;
    function toI420(src, w, h, dst, bgr) {
      const ri = bgr ? 2 : 0, bi = bgr ? 0 : 2;
      const cw = w >> 1, ys = w * h;
      const U = ys, V = ys + cw * (h >> 1);
      for (let y = 0; y < h; y += 2) {
        let p0 = y * w * 4, p1 = p0 + w * 4, y0 = y * w, y1 = y0 + w, c = (y >> 1) * cw;
        for (let x = 0; x < w; x += 2, p0 += 8, p1 += 8, y0 += 2, y1 += 2, c++) {
          const r0 = src[p0 + ri], g0 = src[p0 + 1], b0 = src[p0 + bi];
          const r1 = src[p0 + 4 + ri], g1 = src[p0 + 5], b1 = src[p0 + 4 + bi];
          const r2 = src[p1 + ri], g2 = src[p1 + 1], b2 = src[p1 + bi];
          const r3 = src[p1 + 4 + ri], g3 = src[p1 + 5], b3 = src[p1 + 4 + bi];
          dst[y0] = (YR * r0 + YG * g0 + YB * b0 + YOFF) >> 16;
          dst[y0 + 1] = (YR * r1 + YG * g1 + YB * b1 + YOFF) >> 16;
          dst[y1] = (YR * r2 + YG * g2 + YB * b2 + YOFF) >> 16;
          dst[y1 + 1] = (YR * r3 + YG * g3 + YB * b3 + YOFF) >> 16;
          const r = r0 + r1 + r2 + r3, g = g0 + g1 + g2 + g3, b = b0 + b1 + b2 + b3;
          dst[U + c] = (UR * r + UG * g + UB * b + COFF) >> 18;
          dst[V + c] = (VR * r + VG * g + VB * b + COFF) >> 18;
        }
      }
    }

    let enc = null, kind = '', quantizer = 0, rgb = null, yuv = null, W = 0, H = 0;
    const BT709 = { matrix: 'bt709', primaries: 'bt709', transfer: 'bt709', fullRange: false };
    self.onmessage = async (e) => {
      const m = e.data;
      try {
        if (m.type === 'configure') {
          W = m.config.width;
          H = m.config.height;
          kind = m.kind;
          quantizer = m.quantizer;
          yuv = new Uint8Array(W * H * 3 / 2);
          enc = new VideoEncoder({
            output: (chunk) => {
              const a = new ArrayBuffer(chunk.byteLength);
              chunk.copyTo(a);
              self.postMessage({ type: 'chunk', key: chunk.type === 'key', ts: chunk.timestamp, data: a }, [a]);
            },
            error: (err) => self.postMessage({ type: 'error', message: `VideoEncoder: ${err.message || err}` }),
          });
          enc.configure(m.config);
          self.postMessage({ type: 'configured' });
        } else if (m.type === 'frame') {
          const t0 = performance.now();
          const vf = m.frame;
          const n = vf.allocationSize();
          if (!rgb || rgb.length !== n) rgb = new Uint8Array(n);
          await vf.copyTo(rgb);
          const bgr = vf.format === 'BGRX' || vf.format === 'BGRA';
          vf.close();
          toI420(rgb, W, H, yuv, bgr);
          const f = new VideoFrame(yuv, {
            format: 'I420', codedWidth: W, codedHeight: H,
            timestamp: m.ts, duration: m.dur, colorSpace: BT709,
          });
          enc.encode(f, { keyFrame: m.key, [kind]: { quantizer } });
          f.close();
          self.postMessage({ type: 'converted', ms: performance.now() - t0 });
        } else if (m.type === 'flush') {
          await enc.flush();
          enc.close();
          self.postMessage({ type: 'flushed' });
        }
      } catch (err) {
        self.postMessage({ type: 'error', message: `worker: ${err.message || err}` });
      }
    };
  }

  const MAX_IN_FLIGHT = 4;
  let worker = null;
  let chunks = [];
  let inFlight = 0;
  let convMs = 0;
  let failure = null;
  let wake = null;
  let fps = 30;
  let keyEvery = 300;
  let submitted = 0;
  let flushed = false;
  let configured = false;

  function onMessage(e) {
    const m = e.data;
    if (m.type === 'chunk') {
      chunks.push(m);
      inFlight--;
    } else if (m.type === 'converted') {
      convMs += m.ms;
    } else if (m.type === 'configured') {
      configured = true;
    } else if (m.type === 'flushed') {
      flushed = true;
    } else if (m.type === 'error') {
      failure = new Error(m.message);
    }
    if (wake) { const w = wake; wake = null; w(m); }
  }

  function next() {
    return new Promise((resolve) => { wake = resolve; });
  }

  async function until(pred) {
    while (!pred()) {
      if (failure) throw failure;
      await next();
    }
    if (failure) throw failure;
  }

  // Chunks as one base64 string: per chunk u32 length, u8 key flag, then the bytes.
  function takeChunks() {
    const parts = [];
    for (const c of chunks) {
      const h = new DataView(new ArrayBuffer(5));
      h.setUint32(0, c.data.byteLength, true);
      h.setUint8(4, c.key ? 1 : 0);
      parts.push(h.buffer, c.data);
    }
    const n = chunks.length;
    chunks = [];
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve({ n, b64: fr.result.slice(fr.result.indexOf(',') + 1) });
      fr.onerror = () => reject(fr.error);
      fr.readAsDataURL(new Blob(parts));
    });
  }

  window.pageEncoder = {
    // Returns the encoder config actually used.
    async configure({ quantizer, fps: rate, keyEvery: kf }) {
      const canvas = window.busmap.canvas;
      fps = rate;
      keyEvery = kf;
      const config = {
        codec: 'vp09.00.51.08',
        width: canvas.width,
        height: canvas.height,
        framerate: fps,
        bitrateMode: 'quantizer',
        latencyMode: 'quality',
        hardwareAcceleration: 'prefer-software',
      };
      const support = await VideoEncoder.isConfigSupported(config);
      if (!support.supported) throw new Error(`VideoEncoder does not support ${JSON.stringify(config)}`);
      const src = `(${workerMain.toString()})()`;
      worker = new Worker(URL.createObjectURL(new Blob([src], { type: 'text/javascript' })));
      worker.onmessage = onMessage;
      worker.onerror = (e) => { failure = new Error(`worker: ${e.message}`); if (wake) wake(); };
      worker.postMessage({ type: 'configure', config, kind: 'vp9', quantizer });
      await until(() => configured);
      return config;
    },

    // Draws and submits frames; returns timings, whatever chunks are ready,
    // and the canvas PNG of each frame listed in keep (review frames).
    async encodeFrames(frames, keep) {
      const busmap = window.busmap;
      const canvas = busmap.canvas;
      const keepSet = new Set(keep || []);
      const pngs = {};
      const dur = Math.round(1e6 / fps);
      let draw = 0, snap = 0, wait = 0, lastT = null;
      convMs = 0;
      for (const i of frames) {
        const a = performance.now();
        lastT = busmap.renderFrame(i);
        const b = performance.now();
        if (keepSet.has(i)) pngs[i] = canvas.toDataURL('image/png');
        const ts = Math.round(submitted * 1e6 / fps);
        const vf = new VideoFrame(canvas, { timestamp: ts, duration: dur });
        const c = performance.now();
        await until(() => inFlight < MAX_IN_FLIGHT);
        const d = performance.now();
        worker.postMessage({ type: 'frame', frame: vf, ts, dur, key: submitted % keyEvery === 0 }, [vf]);
        inFlight++;
        submitted++;
        draw += b - a;
        snap += c - b;
        wait += d - c;
      }
      const t0 = performance.now();
      const out = await takeChunks();
      return { T: lastT, draw, snap, wait, conv: convMs, pack: performance.now() - t0, pngs, ...out };
    },

    async finish() {
      worker.postMessage({ type: 'flush' });
      await until(() => flushed);
      worker.terminate();
      return takeChunks();
    },
  };
})();
