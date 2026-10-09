// 議事録の画面: 録音ボタンと、文字起こし・議事録の表示。
// 表示はPCから届く出来事（Server-Sent Events）で書き換える。つなぎ直すと最初に全体が届く。
"use strict";

(() => {
  const app = document.getElementById("minutes-app");
  if (!app) return;
  const meetingId = Number(app.dataset.meetingId);
  const $ = (sel) => app.querySelector(sel);
  const { createRecorder, Uploader } = window.MinutesRecorder;

  // ---- 表示 ----
  const transcript = $("#transcript");
  const lines = new Map();

  function showNotice(el, text) {
    if (!el) return;
    el.hidden = !text;
    const body = el.querySelector("p");
    if (body) body.textContent = text || "";
  }

  function upsertSegment(seg) {
    if (seg.status === "done" && !seg.text) {
      const old = lines.get(seg.seg_id);
      if (old) { old.remove(); lines.delete(seg.seg_id); }
      return; // 無音の切れ端は出さない
    }
    let row = lines.get(seg.seg_id);
    if (!row) {
      row = document.createElement("div");
      row.className = "line";
      row.innerHTML = "<time></time><span></span>";
      const next = [...lines.keys()].filter((k) => k > seg.seg_id).sort((a, b) => a - b)[0];
      transcript.insertBefore(row, next ? lines.get(next) : null);
      lines.set(seg.seg_id, row);
    }
    const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
    row.querySelector("time").textContent = seg.time;
    row.querySelector("span").textContent =
      seg.status === "pending" ? "（文字起こし中…）" : seg.status === "failed" ? "（文字起こしに失敗）" : seg.text;
    row.dataset.status = seg.status;
    const empty = $("#transcript-empty");
    if (empty) empty.hidden = lines.size > 0;
    if (nearBottom) transcript.scrollTop = transcript.scrollHeight;
  }

  function renderDraft(draft, editing) {
    app.querySelectorAll("[data-section]").forEach((el) => {
      const name = el.dataset.section;
      const text = (draft && draft[name]) || "";
      el.querySelector(".section-body").textContent = text || "（まだなし）";
      el.classList.toggle("editing", name === editing);
      el.classList.toggle("blank", !text);
    });
    const badge = $("#editing-badge");
    if (badge) badge.hidden = !editing;
  }

  function onStatus(status, error) {
    if (status !== app.dataset.status && (status === "done" || status === "failed")) {
      location.reload(); // 清書が済んだら、清書の表示に切り替える
      return;
    }
    app.dataset.status = status;
    showNotice($("#finishing-notice"), status === "finishing" ? "清書しています（録音全体を文字起こしし直してから、Sonnet 5.5 がまとめます。数分かかります）。" : "");
    if (error) showNotice($("#ai-notice"), error);
  }

  function connect() {
    const source = new EventSource(`/minutes/${meetingId}/events`);
    source.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      if (ev.type === "snapshot") {
        ev.segments.forEach(upsertSegment);
        renderDraft(ev.draft, null);
        onStatus(ev.status, ev.error);
      } else if (ev.type === "segment") {
        upsertSegment(ev);
      } else if (ev.type === "draft") {
        renderDraft(ev.draft, ev.editing);
        if (!ev.editing) showNotice($("#ai-notice"), "");
      } else if (ev.type === "status") {
        onStatus(ev.status, ev.error);
      } else if (ev.type === "notice") {
        showNotice($("#ai-notice"), ev.message);
      }
    };
  }

  // ---- 送信待ちの表示 ----
  Uploader.onChange((count, error) => {
    const el = $("#upload-state");
    if (el && count !== null) el.textContent = count ? `PCへ送信待ち ${count}件` : "すべてPCに保存済み";
    showNotice($("#upload-notice"), error);
  });
  Uploader.kick(); // 前に送れなかった控えがあれば送る

  // ---- 録音 ----
  const recBox = $("#recorder");
  if (!recBox) { connect(); return; }

  if (!window.isSecureContext || !navigator.mediaDevices) {
    const https = location.hostname.endsWith(".ts.net") ? `https://${location.hostname}${location.pathname}` : "";
    showNotice($("#mic-notice"), https
      ? `マイクはhttpsのページでしか使えません。こちらで開き直してください: ${https}`
      : "マイクはhttpsのページでしか使えません。Tailscaleの https://〜.ts.net のアドレスで開き直してください。");
    $("#rec-start").disabled = true;
  }

  const startBtn = $("#rec-start");
  const stopBtn = $("#rec-stop");
  const resumeBtn = $("#rec-resume");
  const timer = $("#rec-timer");
  const level = $("#rec-level");
  let recorder = null;
  let recording = false;
  let startedAt = 0;
  let hiddenAt = null;
  let wakeLock = null;

  const clock = (ms) => {
    const s = Math.floor(ms / 1000);
    return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  };
  setInterval(() => { if (recording) timer.textContent = clock(Date.now() - startedAt); }, 500);

  async function keepScreenOn() {
    try {
      if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen");
    } catch (e) { /* 画面の自動ロックを切る設定をお願いする（画面の注意書き）*/ }
  }

  function onInterrupted() {
    if (!recording) return;
    resumeBtn.hidden = false;
    showNotice($("#mic-notice"), "録音が止まっています。「録音を再開」を押してください。");
  }

  // ノイズ除去のオン・オフ。録り比べられるよう、選んだ方をこのiPhoneに覚えておく
  const noiseBox = $("#noise-suppression");
  // 既定は「使う」。わざと切ったときだけ、切ったことを覚えておく
  noiseBox.checked = true;
  try { noiseBox.checked = localStorage.getItem("minutes-noise-suppression") !== "off"; } catch (e) { /* 覚えられなくても動く */ }
  noiseBox.addEventListener("change", () => {
    try { localStorage.setItem("minutes-noise-suppression", noiseBox.checked ? "on" : "off"); } catch (e) { /* 同上 */ }
  });

  // オンライン会議の相手の声（PCの音）も録るか。画面共有ができるブラウザ（PCのChrome・Edge）でだけ出す
  const systemBox = $("#system-audio");
  const systemRow = $("#system-audio-row");
  const canShareAudio = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
  systemRow.hidden = !canShareAudio;
  try { systemBox.checked = canShareAudio && localStorage.getItem("minutes-system-audio") === "on"; } catch (e) { /* 覚えられなくても動く */ }
  const showSystemHint = () => { $("#system-audio-hint").hidden = !systemBox.checked; };
  systemBox.addEventListener("change", () => {
    try { localStorage.setItem("minutes-system-audio", systemBox.checked ? "on" : "off"); } catch (e) { /* 同上 */ }
    showSystemHint();
  });
  showSystemHint();

  async function begin() {
    recorder = createRecorder(meetingId, {
      onLevel: (v) => { level.style.width = `${Math.round(v * 100)}%`; },
      onInterrupted,
      noiseSuppression: noiseBox.checked,
      systemAudio: systemBox.checked,
    });
    noiseBox.disabled = true; // 録音の途中では切り替えない
    systemBox.disabled = true;
    await recorder.start();
    recording = true;
    resumeBtn.hidden = true;
    showNotice($("#mic-notice"), "");
    await keepScreenOn();
  }

  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    try {
      await begin();
      startedAt = Date.now();
      startBtn.hidden = true;
      stopBtn.hidden = false;
    } catch (e) {
      startBtn.disabled = false;
      // 途中まで開いたマイクや画面共有を閉じておく（開いたままだと次に押したとき取り合いになる）
      if (recorder) recorder.stop().catch(() => {});
      const reason = e.message || e;
      showNotice($("#mic-notice"), systemBox.checked
        ? `録音を始められませんでした（${reason}）。画面の共有とマイクの使用を許可してください。`
        : `マイクを使えませんでした（${reason}）。設定アプリ → Safari → マイク を「許可」にしてください。`);
      noiseBox.disabled = false;
      systemBox.disabled = false;
    }
  });

  resumeBtn.addEventListener("click", async () => {
    // 新しい録音を先に始める（iOSはボタンを押した直後でないとマイクを開かせないため）。
    // 止まった方は、手元に残っている音を切れ端にしてから閉じる。
    const old = recorder;
    try {
      await begin();
      if (old) old.stop();
    } catch (e) {
      showNotice($("#mic-notice"), `録音を再開できませんでした（${e.message || e}）。`);
    }
  });

  stopBtn.addEventListener("click", async () => {
    if (!confirm("録音を止めて、清書を始めますか？")) return;
    stopBtn.disabled = true;
    recording = false;
    if (recorder) await recorder.stop();
    if (wakeLock) wakeLock.release().catch(() => {});
    recBox.hidden = true;
    showNotice($("#finishing-notice"), "残りの音声をPCへ送っています…");
    // 全部PCに届いてから清書を始める。届かない間は待ち続ける（控えはiPhoneに残っている）
    for (;;) {
      const left = await Uploader.pendingFor(meetingId);
      if (!left) break;
      showNotice($("#finishing-notice"), `残りの音声をPCへ送っています（あと${left}件）。このページを開いたままにしてください。`);
      Uploader.kick();
      await new Promise((r) => setTimeout(r, 1500));
    }
    // 清書の開始を頼む。届かなければ少し待って頼み直す
    for (;;) {
      try {
        const res = await fetch(`/minutes/${meetingId}/finish`, { method: "POST" });
        if (res.ok) break;
      } catch (e) { /* 通信が切れている */ }
      showNotice($("#finishing-notice"), "PCに清書を頼めませんでした。つながり次第、頼み直します。このページを開いたままにしてください。");
      await new Promise((r) => setTimeout(r, 3000));
    }
  });

  document.addEventListener("visibilitychange", async () => {
    if (!recording) return;
    if (document.visibilityState === "hidden") { hiddenAt = new Date(); return; }
    await keepScreenOn();
    if (!recorder.healthy()) {
      const since = hiddenAt ? hiddenAt.toTimeString().slice(0, 5) : "";
      onInterrupted();
      showNotice($("#mic-notice"), `画面が消えていた間（${since}〜）は録音できていません。「録音を再開」を押してください。`);
    }
  });

  window.addEventListener("beforeunload", (e) => {
    if (recording) { e.preventDefault(); e.returnValue = ""; }
  });

  connect();
})();
