// 議事録の録音（iPhoneのSafariで動かす）。
//
// 1. マイクの音を16kHzに落とし、話の切れ目（息つぎ）ごとに切ってWAVにする
// 2. WAVはまずiPhoneの中（IndexedDB）に保存する
// 3. PCへ送り、「受け取った」と返事が来たらiPhoneの中の控えを消す
//    電波が切れても控えは残り、つながり直したら自動で送り直す
"use strict";

const RATE = 16000;
const FRAME = 320; // 20ミリ秒
const PAUSE_MS = 600; // これだけ黙ったら切る
const SHORT_PAUSE_MS = 250; // 長く話し続けているときは、短い切れ目でも切る
const LONG_SEGMENT_MS = 8000;
const MIN_SEGMENT_MS = 1500;
const MAX_SEGMENT_MS = 20000;
const SILENT_SEGMENT_MS = 10000; // 誰も話していなくても、録音として10秒ごとに送る
const MIN_THRESHOLD = 0.006;

// ---- iPhoneの中の控え（IndexedDB）----
// IndexedDB が使えない・壊れたときはメモリに持つ（ページを閉じるまでは残る）。
// Safariは裏に回ったあと接続が切れていることがあるので、失敗したら開き直して1回だけやり直す。
const Store = (() => {
  let dbPromise = null;
  const memory = new Map();

  function open() {
    if (!dbPromise) {
      dbPromise = new Promise((resolve) => {
        try {
          const req = indexedDB.open("minutes-recorder", 1);
          req.onupgradeneeded = () => req.result.createObjectStore("pending", { keyPath: "key" });
          req.onsuccess = () => resolve(req.result);
          req.onerror = () => resolve(null);
        } catch (e) {
          resolve(null);
        }
      });
    }
    return dbPromise;
  }

  async function run(mode, fn) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const db = await open();
      if (!db) break;
      try {
        return await new Promise((resolve, reject) => {
          const tx = db.transaction("pending", mode);
          const req = fn(tx.objectStore("pending"));
          tx.oncomplete = () => resolve(req.result);
          tx.onerror = () => reject(tx.error);
          tx.onabort = () => reject(tx.error);
        });
      } catch (e) {
        dbPromise = null; // 次は開き直す
      }
    }
    throw new Error("IndexedDBが使えません");
  }

  return {
    async put(item) {
      try {
        await run("readwrite", (s) => s.put(item));
      } catch (e) {
        memory.set(item.key, item);
      }
    },
    async remove(key) {
      memory.delete(key);
      try { await run("readwrite", (s) => s.delete(key)); } catch (e) { /* メモリ側だけだった */ }
    },
    async all() {
      let saved = [];
      try { saved = (await run("readonly", (s) => s.getAll())) || []; } catch (e) { /* メモリ側だけ */ }
      const merged = new Map(saved.map((i) => [i.key, i]));
      memory.forEach((item, key) => merged.set(key, item));
      return Array.from(merged.values()).sort((a, b) => a.segId - b.segId);
    },
  };
})();

// ---- PCへ送る係 ----
const UPLOAD_TIMEOUT_MS = 30000; // 返事が来ないまま固まった通信で、後ろの切れ端まで止めないため

const Uploader = (() => {
  let running = false;
  let wake = null;
  const rejected = new Set(); // PCが受け付けなかったもの（形式の誤りなど）。控えは消さずに残す
  const listeners = [];

  function notify(count, error) {
    listeners.forEach((fn) => fn(count, error));
  }

  async function sendOne(item) {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), UPLOAD_TIMEOUT_MS);
    try {
      const res = await fetch(`/minutes/${item.meetingId}/segments?seg=${item.segId}`, {
        method: "POST",
        headers: { "Content-Type": "audio/wav" },
        body: item.wav,
        signal: abort.signal,
      });
      if (res.ok) return "ok";
      return res.status >= 400 && res.status < 500 ? "rejected" : "retry";
    } finally {
      clearTimeout(timer);
    }
  }

  async function loop() {
    if (running) return;
    running = true;
    let backoff = 1000;
    try {
      for (;;) {
        const items = (await Store.all()).filter((i) => !rejected.has(i.key));
        notify(items.length, null);
        if (!items.length) break;
        const item = items[0];
        let result;
        try {
          result = await sendOne(item);
        } catch (e) {
          result = "retry";
        }
        if (result === "ok") {
          await Store.remove(item.key);
          backoff = 1000;
        } else if (result === "rejected") {
          rejected.add(item.key);
          notify(items.length - 1, "PCが受け付けなかった音声があります（iPhoneの中に残しています）。");
        } else {
          notify(items.length, "PCに届きません。つながり次第、自動で送り直します。");
          await new Promise((r) => { wake = r; setTimeout(r, backoff); });
          backoff = Math.min(backoff * 2, 10000);
        }
      }
    } catch (e) {
      notify(null, `送信が止まりました（${e.message || e}）。ページを開き直すと続きから送ります。`);
    } finally {
      running = false;
    }
  }

  return {
    kick() {
      if (wake) { wake(); wake = null; }
      loop();
    },
    onChange(fn) { listeners.push(fn); },
    async pendingFor(meetingId) {
      return (await Store.all()).filter((i) => i.meetingId === meetingId && !rejected.has(i.key)).length;
    },
  };
})();

// ---- 16kHzへの変換 ----
// 間引く前に、16kHzでは表せない高い音（8kHz以上）をきちんと削る（窓付きsincの低域通過フィルタ）。
// 削らずに間引くと、高い音が低い音に化けて声に雑音が混ざり、文字起こしの精度が落ちる。
const RESAMPLE_TAPS = 63;
const RESAMPLE_CUTOFF_HZ = 7000;

function makeLowPass(inRate) {
  const half = (RESAMPLE_TAPS - 1) / 2;
  const fc = RESAMPLE_CUTOFF_HZ / inRate;
  const h = new Float32Array(RESAMPLE_TAPS);
  let sum = 0;
  for (let i = 0; i < RESAMPLE_TAPS; i++) {
    const n = i - half;
    const sinc = n === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * n) / (Math.PI * n);
    const blackman = 0.42 - 0.5 * Math.cos((2 * Math.PI * i) / (RESAMPLE_TAPS - 1)) + 0.08 * Math.cos((4 * Math.PI * i) / (RESAMPLE_TAPS - 1));
    h[i] = sinc * blackman;
    sum += h[i];
  }
  return h.map((v) => v / sum);
}

function makeResampler(inRate) {
  const ratio = inRate / RATE;
  const h = makeLowPass(inRate);
  const half = (RESAMPLE_TAPS - 1) / 2;
  let buf = new Float32Array(half); // 録り始めの前は無音として扱う
  let pos = half; // 次に出す音の、buf の中での位置
  return (input) => {
    const joined = new Float32Array(buf.length + input.length);
    joined.set(buf);
    joined.set(input, buf.length);
    const out = [];
    while (Math.round(pos) + half < joined.length) {
      const center = Math.round(pos);
      let acc = 0;
      for (let k = 0; k < RESAMPLE_TAPS; k++) acc += joined[center - half + k] * h[k];
      out.push(acc);
      pos += ratio;
    }
    const drop = Math.max(0, Math.floor(pos) - half);
    buf = joined.slice(drop);
    pos -= drop;
    return Float32Array.from(out);
  };
}

if (typeof module !== "undefined") module.exports = { makeResampler }; // テスト用

function toWav(samples) {
  const view = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const text = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
  text(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
  text(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, RATE, true); view.setUint32(28, RATE * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, "data"); view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const v = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
  }
  return view.buffer;
}

// ---- 話の切れ目で切る係 ----
function makeSegmenter(onSegment, onLevel) {
  let chunks = [];
  let length = 0;
  let startMs = 0;
  let hasSpeech = false;
  let silentFrames = 0;
  let floor = 0.01;
  let pending = new Float32Array(0);

  function emit() {
    if (!length) return;
    const all = new Float32Array(length);
    let offset = 0;
    chunks.forEach((c) => { all.set(c, offset); offset += c.length; });
    onSegment(startMs, all);
    chunks = []; length = 0; hasSpeech = false; silentFrames = 0;
  }

  function frame(samples) {
    if (!length) startMs = Date.now() - Math.round((samples.length / RATE) * 1000);
    chunks.push(samples);
    length += samples.length;
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    const rms = Math.sqrt(sum / samples.length);
    floor = rms < floor ? rms : floor * 1.0008; // 周りの雑音の大きさをゆっくり追う
    const threshold = Math.max(MIN_THRESHOLD, floor * 3);
    onLevel(Math.min(1, rms / 0.15));
    if (rms > threshold) { hasSpeech = true; silentFrames = 0; } else { silentFrames++; }

    const segMs = (length / RATE) * 1000;
    const silentMs = silentFrames * 20;
    if ((hasSpeech && segMs >= MIN_SEGMENT_MS && silentMs >= PAUSE_MS)
      || (hasSpeech && segMs >= LONG_SEGMENT_MS && silentMs >= SHORT_PAUSE_MS)
      || segMs >= MAX_SEGMENT_MS
      || (!hasSpeech && segMs >= SILENT_SEGMENT_MS)) {
      emit();
    }
  }

  return {
    push(samples) {
      const buf = new Float32Array(pending.length + samples.length);
      buf.set(pending);
      buf.set(samples, pending.length);
      let i = 0;
      for (; i + FRAME <= buf.length; i += FRAME) frame(buf.slice(i, i + FRAME));
      pending = buf.slice(i);
    },
    flush() {
      if (pending.length) { chunks.push(pending); length += pending.length; pending = new Float32Array(0); }
      emit();
    },
  };
}

// ---- 録音そのもの ----
// noiseSuppression: iPhoneのノイズ除去を使うか。前もってノイズを消すと精度が落ちるという研究もあるが、
// 2026-10-03 に実際の環境で録り比べたら、使った方が文字起こしが良かったので既定は使う。
// systemAudio: PCのオンライン会議用。相手の声（PCで鳴っている音）を画面共有の仕組みで取り込み、
// 自分のマイクの声と混ぜて1本にする。PCのChrome・Edgeで使える（iPhoneでは使えない）。
function createRecorder(meetingId, { onLevel, onInterrupted, noiseSuppression = true, systemAudio = false }) {
  let ctx = null;
  let stream = null;
  let display = null;
  let node = null;
  let segmenter = null;
  let lastSegId = 0;

  const saving = new Set(); // 保存の途中のもの。止めるときに待つ

  function saveSegment(startMs, samples) {
    const segId = Math.max(startMs, lastSegId + 1);
    lastSegId = segId;
    const item = { key: `${meetingId}:${segId}`, meetingId, segId, wav: toWav(samples), createdAt: Date.now() };
    // Store.put は失敗してもメモリに残すので、ここで切れ端が消えることはない
    const job = Store.put(item).then(() => Uploader.kick(), () => Uploader.kick());
    saving.add(job);
    job.finally(() => saving.delete(job));
  }

  async function start() {
    // iOSはボタンを押した直後でないと音を扱わせてくれないので、待ち（await）より前に作る
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    ctx.resume().catch(() => {});
    if (systemAudio) {
      // 画面共有もボタンを押した直後でないと頼めないので、マイクより先に頼む。
      // Chromeは映像なしの共有を許さないので映像も受け取るが、使わない。
      display = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true, systemAudio: "include" });
      if (!display.getAudioTracks().length) {
        display.getTracks().forEach((t) => t.stop());
        display = null;
        throw new Error("PCの音が共有されていません。共有する画面を選ぶときに「システム音声も共有する」をオンにしてください");
      }
    }
    stream = await navigator.mediaDevices.getUserMedia({
      // オンライン会議では相手の声がスピーカーから入り込むので、エコー除去を使う
      audio: { channelCount: 1, echoCancellation: systemAudio, noiseSuppression, autoGainControl: true },
    });
    await ctx.audioWorklet.addModule("/static/minutes/worklet.js");
    const source = ctx.createMediaStreamSource(stream);
    node = new AudioWorkletNode(ctx, "capture-processor");
    const resample = makeResampler(ctx.sampleRate);
    segmenter = makeSegmenter((s, samples) => saveSegment(s, samples), onLevel);
    node.port.onmessage = (e) => segmenter.push(resample(e.data));
    source.connect(node);
    if (display) {
      // 同じ入口に2つつなぐと足し合わされる（自分の声＋相手の声）
      ctx.createMediaStreamSource(new MediaStream(display.getAudioTracks())).connect(node);
      display.getTracks().forEach((t) => t.addEventListener("ended", onInterrupted));
    }
    // 出力につながないと動かないブラウザがあるので、音量0でつないでおく
    const mute = ctx.createGain();
    mute.gain.value = 0;
    node.connect(mute).connect(ctx.destination);
    stream.getAudioTracks().forEach((t) => t.addEventListener("ended", onInterrupted));
    ctx.addEventListener("statechange", () => { if (ctx && ctx.state !== "running") onInterrupted(); });
    if (ctx.state !== "running") await ctx.resume();
  }

  async function stop() {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (display) display.getTracks().forEach((t) => t.stop());
    // マイクを止めたあと、途中まで届いている音を受け取りきってから最後の切れ端を作る
    await new Promise((r) => setTimeout(r, 200));
    if (segmenter) segmenter.flush();
    if (ctx) await ctx.close().catch(() => {});
    ctx = null; stream = null; display = null; node = null; segmenter = null;
    await Promise.all(Array.from(saving));
  }

  function healthy() {
    const live = (s) => s.getAudioTracks().every((t) => t.readyState === "live");
    return !!(ctx && ctx.state === "running" && stream && live(stream) && (!systemAudio || (display && live(display))));
  }

  return { start, stop, healthy };
}

window.MinutesRecorder = { createRecorder, Uploader };
