// 話して入力（iPhoneのSafariで動かす）。
//
// 1. 議事録と同じ録音の係で、話の切れ目ごとに16kHzのWAVにする
// 2. 切れ端を1つずつ順に /dictate へ送り、返ってきた文字をすぐ欄の最後に足す
// 3. 止めたら、残りの切れ端が文字になるのを待って「コピー」「ひとりメモにする」を使えるようにする
//
// 声の入っていない切れ端は送らない。録音はPCでも文字にしたらすぐ消える（/dictate）。
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const startBtn = $("talk-start");
  const stopBtn = $("talk-stop");
  const timer = $("talk-timer");
  const level = $("talk-level");
  const state = $("talk-state");
  const text = $("talk-text");
  const copyBtn = $("talk-copy");
  const memoBtn = $("talk-memo");
  const clearBtn = $("talk-clear");
  const errorBox = $("talk-error");

  const RETRY_MS = 2000;
  const MAX_TRIES = 5;

  let recorder = null;
  let recording = false;
  let startedAt = 0;
  let tick = null;
  let wakeLock = null;
  let queue = []; // 送る順番待ちの切れ端 { at, ms, wav }
  let sending = false;
  let pieces = []; // 文字になった切れ端 { at, ms, text }
  let edited = false; // 欄を手で直したか. 直したら、ひとりメモには欄の文字をそのまま使う
  let lost = 0; // 文字にできなかった切れ端の数

  function showError(message) {
    errorBox.querySelector("p").textContent = message;
    errorBox.hidden = !message;
  }

  function refresh() {
    const waiting = queue.length + (sending ? 1 : 0);
    const has = text.value.trim().length > 0;
    copyBtn.disabled = !has;
    memoBtn.disabled = !has || recording || waiting > 0;
    clearBtn.disabled = recording || waiting > 0 || !has;
    if (recording) {
      state.textContent = waiting ? `録っています。文字にしています…（あと${waiting}つ）` : "録っています。話の切れ目ごとに文字が出ます。";
    } else if (waiting) {
      state.textContent = `残りを文字にしています…（あと${waiting}つ）`;
    } else if (has) {
      state.textContent = lost ? `できました。文字にできなかった部分が${lost}か所あります。` : "できました。直してからコピーもできます。";
    } else {
      state.textContent = "「話す」を押してから話してください。録っている間は画面をつけたままにしてください（消えると止まります）。";
    }
  }

  function append(piece) {
    pieces.push(piece);
    // 手で直していても、欄の最後に足す（直した部分は消さない）
    const atEnd = text.selectionStart === text.value.length;
    text.value += piece.text;
    if (atEnd) text.scrollTop = text.scrollHeight;
  }

  async function sendOne(item) {
    const res = await fetch("/dictate", { method: "POST", headers: { "Content-Type": "audio/wav" }, body: item.wav });
    if (res.ok) return { ok: true, text: (await res.text()).trim() };
    return { ok: false, retry: res.status >= 500, message: (await res.text()).trim() };
  }

  async function pump() {
    if (sending) return;
    sending = true;
    try {
      while (queue.length) {
        const item = queue[0];
        let done = false;
        for (let tries = 1; !done && tries <= MAX_TRIES; tries++) {
          try {
            const result = await sendOne(item);
            if (result.ok) {
              if (result.text) append({ at: item.at, ms: item.ms, text: result.text });
              done = true;
              showError("");
            } else if (!result.retry) {
              lost++;
              showError(result.message || "PCが受け付けませんでした。");
              done = true;
            }
          } catch (e) {
            showError("PCに届きません。つながり次第、送り直します。");
          }
          if (!done) await new Promise((r) => setTimeout(r, RETRY_MS));
        }
        if (!done) lost++;
        queue.shift();
        refresh();
      }
    } finally {
      sending = false;
      refresh();
    }
  }

  function onSegment(at, wav, ms, hasSpeech) {
    if (!hasSpeech) return;
    queue.push({ at, ms, wav });
    refresh();
    pump();
  }

  function showTimer() {
    const sec = Math.floor((Date.now() - startedAt) / 1000);
    timer.textContent = `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
  }

  async function keepScreenOn() {
    try {
      if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen");
    } catch (e) { /* 使えなくても録音はできる */ }
  }

  async function start() {
    showError("");
    recorder = window.MinutesRecorder.createRecorder(null, {
      onSegment,
      onLevel: (v) => { level.firstElementChild.style.width = `${Math.round(v * 100)}%`; },
      onInterrupted: () => { if (recording) stop("録音が止まりました（画面が消えた・ほかのアプリがマイクを使った など）。続けるときはもう一度「話す」を押してください。"); },
    });
    try {
      await recorder.start();
    } catch (e) {
      recorder = null;
      showError(`マイクを使えませんでした（${e.message || e}）。Tailscaleのhttpsのアドレスで開いているか、マイクを許可したかを確かめてください。`);
      return;
    }
    recording = true;
    startedAt = Date.now();
    startBtn.hidden = true;
    stopBtn.hidden = false;
    timer.hidden = false;
    level.hidden = false;
    showTimer();
    tick = setInterval(showTimer, 500);
    await keepScreenOn();
    refresh();
  }

  async function stop(message) {
    if (!recording) return;
    recording = false;
    clearInterval(tick);
    const current = recorder;
    recorder = null;
    await current.stop(); // 最後の切れ端もここで onSegment に来る
    if (wakeLock) wakeLock.release().catch(() => {});
    wakeLock = null;
    startBtn.hidden = false;
    stopBtn.hidden = true;
    level.hidden = true;
    if (message) showError(message);
    refresh();
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(text.value);
    } catch (e) {
      text.select();
      document.execCommand("copy");
    }
    copyBtn.textContent = "コピーしました";
    setTimeout(() => { copyBtn.innerHTML = '<i data-lucide="copy"></i>コピー'; if (window.lucide) lucide.createIcons(); }, 2000);
  }

  async function saveMemo() {
    if (!pieces.length && !text.value.trim()) return;
    const body = edited || !pieces.length
      ? [{ at: pieces.length ? pieces[0].at : Date.now(), ms: pieces.reduce((n, p) => n + p.ms, 0), text: text.value }]
      : pieces;
    memoBtn.disabled = true;
    memoBtn.textContent = "送っています…";
    try {
      const res = await fetch("/memos/from-text", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pieces: body }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || res.status);
      memoBtn.dataset.saved = "1";
      location.href = data.url;
    } catch (e) {
      showError(`ひとりメモにできませんでした（${e.message || e}）。文字は残っているので、もう一度押してください。`);
      memoBtn.innerHTML = '<i data-lucide="sticky-note"></i>ひとりメモにする';
      if (window.lucide) lucide.createIcons();
      refresh();
    }
  }

  function clear() {
    if (text.value.trim() && !confirm("文字を消して最初からにしますか？")) return;
    pieces = []; queue = []; text.value = ""; edited = false; lost = 0;
    timer.hidden = true;
    showError("");
    refresh();
  }

  startBtn.addEventListener("click", start);
  stopBtn.addEventListener("click", () => stop(""));
  copyBtn.addEventListener("click", copy);
  memoBtn.addEventListener("click", saveMemo);
  clearBtn.addEventListener("click", clear);
  text.addEventListener("input", () => { edited = true; refresh(); });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && recording && recorder && !recorder.healthy()) {
      stop("画面が消えていた間は録れていません。続けるときはもう一度「話す」を押してください。");
    }
  });
  window.addEventListener("beforeunload", (e) => {
    if (recording || queue.length || (text.value.trim() && !memoBtn.dataset.saved)) { e.preventDefault(); e.returnValue = ""; }
  });
  refresh();
})();
