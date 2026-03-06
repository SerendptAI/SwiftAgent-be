/**
 * SwiftAgent Voice Call Widget
 * Self-contained, embeddable voice call widget for customer support.
 *
 * Usage:
 *   <script src="https://api.swiftagents.org/static/widget/widget.js" data-company-id="YOUR_ID"></script>
 */
(function () {
  "use strict";

  // ── Configuration ──────────────────────────────────────────────
  const scriptTag = document.currentScript;
  const COMPANY_ID = scriptTag?.getAttribute("data-company-id") || "";
  const API_BASE =
    scriptTag?.getAttribute("data-api-base") ||
    scriptTag?.src?.replace(/\/static\/widget\/widget\.js.*$/, "") ||
    "";

  if (!COMPANY_ID) {
    console.error("[SwiftAgent] Missing data-company-id attribute");
    return;
  }

  const SESSION_KEY = `sa_session_${COMPANY_ID}`;

  function getSessionId() {
    let id = localStorage.getItem(SESSION_KEY);
    if (!id) {
      id = "sess_" + crypto.randomUUID();
      localStorage.setItem(SESSION_KEY, id);
    }
    return id;
  }

  // ── Inject Styles ──────────────────────────────────────────────
  const STYLES = `
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap');

    .sa-widget * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }

    /* ── Top Banner ── */
    .sa-banner {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      z-index: 99998;
      background: #E5A100;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 24px;
      min-height: 48px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.15);
    }
    .sa-banner-text {
      font-family: 'Space Mono', monospace;
      font-size: 12px;
      font-weight: 700;
      color: #000;
      letter-spacing: 0.5px;
      text-transform: uppercase;
      flex: 1;
      margin-right: 16px;
    }
    .sa-banner-btn {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 18px;
      background: #fff;
      border: 2px solid #000;
      border-radius: 4px;
      cursor: pointer;
      font-family: 'Space Mono', monospace;
      font-size: 12px;
      font-weight: 700;
      color: #000;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      white-space: nowrap;
      transition: background 0.2s, transform 0.15s;
    }
    .sa-banner-btn:hover {
      background: #f0f0f0;
      transform: scale(1.02);
    }
    .sa-banner-btn svg {
      width: 16px;
      height: 16px;
      fill: currentColor;
    }
    .sa-banner-ongoing {
      display: none;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      background: #fff;
      border: 2px solid #000;
      border-radius: 4px;
      font-family: 'Space Mono', monospace;
      font-size: 12px;
      font-weight: 700;
      color: #000;
      letter-spacing: 0.5px;
    }
    .sa-banner-ongoing svg {
      width: 16px;
      height: 16px;
      fill: currentColor;
    }
    .sa-banner-ongoing.sa-active {
      display: flex;
    }

    /* ── FAB (phone button) ── */
    .sa-fab {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 99999;
      width: 56px;
      height: 56px;
      border-radius: 50%;
      background: #fff;
      border: 2px solid #e0e0e0;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 16px rgba(0,0,0,0.12);
      transition: transform 0.2s, box-shadow 0.2s;
    }
    .sa-fab:hover {
      transform: scale(1.08);
      box-shadow: 0 6px 24px rgba(0,0,0,0.18);
    }
    .sa-fab svg {
      width: 24px;
      height: 24px;
      fill: #333;
    }

    /* ── Call Overlay ── */
    .sa-overlay {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      z-index: 100000;
      background: rgba(0,0,0,0.5);
      display: none;
      align-items: center;
      justify-content: center;
      animation: sa-fade-in 0.2s ease-out;
    }
    .sa-overlay.sa-active {
      display: flex;
    }
    @keyframes sa-fade-in {
      from { opacity: 0; }
      to   { opacity: 1; }
    }

    .sa-call-panel {
      width: 90%;
      max-width: 660px;
      background: #fafafa;
      border-radius: 20px;
      overflow: hidden;
      box-shadow: 0 16px 64px rgba(0,0,0,0.3);
      display: flex;
      flex-direction: column;
      animation: sa-scale-up 0.25s ease-out;
    }
    @keyframes sa-scale-up {
      from { opacity: 0; transform: scale(0.95); }
      to   { opacity: 1; transform: scale(1); }
    }

    /* call header */
    .sa-call-header {
      text-align: center;
      padding: 32px 24px 16px;
    }
    .sa-call-company {
      font-size: 20px;
      font-weight: 600;
      color: #1a1a1a;
      margin-bottom: 4px;
    }
    .sa-call-status {
      font-size: 14px;
      color: #888;
    }

    /* call body — large white area */
    .sa-call-body {
      flex: 1;
      min-height: 280px;
      background: #fff;
      margin: 0 16px;
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .sa-call-transcript {
      padding: 24px;
      text-align: center;
      color: #666;
      font-size: 14px;
      line-height: 1.6;
      max-height: 280px;
      overflow-y: auto;
    }

    /* call controls */
    .sa-call-controls {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 20px;
      padding: 24px;
    }
    .sa-ctrl-btn {
      width: 48px;
      height: 48px;
      border-radius: 50%;
      border: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.15s, opacity 0.15s;
      background: #ececec;
    }
    .sa-ctrl-btn:hover {
      transform: scale(1.1);
    }
    .sa-ctrl-btn svg {
      width: 22px;
      height: 22px;
      fill: #333;
    }
    .sa-ctrl-btn.sa-muted {
      opacity: 0.5;
    }

    /* hangup button */
    .sa-hangup {
      width: 56px;
      height: 56px;
      border-radius: 50%;
      background: #E53935;
      border: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.15s, background 0.15s;
    }
    .sa-hangup:hover {
      transform: scale(1.1);
      background: #C62828;
    }
    .sa-hangup svg {
      width: 26px;
      height: 26px;
      fill: #fff;
    }
  `;

  const styleEl = document.createElement("style");
  styleEl.textContent = STYLES;
  document.head.appendChild(styleEl);

  // push page content down so banner doesn't cover it
  document.body.style.marginTop = (parseInt(getComputedStyle(document.body).marginTop) || 0) + 48 + "px";

  // ── Build DOM ──────────────────────────────────────────────────

  // top banner
  const banner = document.createElement("div");
  banner.className = "sa-banner";
  banner.innerHTML = `
    <span class="sa-banner-text">If you have any questions or inquiries, please feel free to get on a call with our Swift Agent.</span>
    <button class="sa-banner-btn" id="sa-banner-cta">
      <svg viewBox="0 0 24 24"><path d="M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z"/></svg>
      Request a Call
    </button>
    <div class="sa-banner-ongoing" id="sa-banner-ongoing">
      <svg viewBox="0 0 24 24"><path d="M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z"/></svg>
      ONGOING..<span id="sa-timer-badge">00:00</span>
    </div>
  `;
  document.body.prepend(banner);

  // call overlay
  const overlay = document.createElement("div");
  overlay.className = "sa-overlay";
  overlay.id = "sa-overlay";
  overlay.innerHTML = `
    <div class="sa-call-panel">
      <div class="sa-call-header">
        <div class="sa-call-company" id="sa-call-company">Support</div>
        <div class="sa-call-status" id="sa-call-status">Calling...</div>
      </div>
      <div class="sa-call-body">
        <div class="sa-call-transcript" id="sa-call-transcript"></div>
      </div>
      <div class="sa-call-controls">
        <button class="sa-ctrl-btn" id="sa-btn-menu" title="More options">
          <svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
        </button>
        <button class="sa-ctrl-btn" id="sa-btn-speaker" title="Toggle speaker">
          <svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>
        </button>
        <button class="sa-ctrl-btn" id="sa-btn-mic" title="Toggle microphone">
          <svg viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5.91-3c-.49 0-.9.36-.98.85C16.52 14.2 14.47 16 12 16s-4.52-1.8-4.93-4.15c-.08-.49-.49-.85-.98-.85-.61 0-1.09.54-1 1.14.49 3 2.89 5.35 5.91 5.78V20c0 .55.45 1 1 1s1-.45 1-1v-2.08c3.02-.43 5.42-2.78 5.91-5.78.1-.6-.39-1.14-1-1.14z"/></svg>
        </button>
        <button class="sa-hangup" id="sa-btn-hangup" title="End call">
          <svg viewBox="0 0 24 24"><path d="M12 9c-1.6 0-3.15.25-4.6.72v3.1c0 .39-.23.74-.56.9-.98.49-1.87 1.12-2.66 1.85-.18.18-.43.28-.7.28-.28 0-.53-.11-.71-.29L.29 13.08c-.18-.17-.29-.42-.29-.7 0-.28.11-.53.29-.71C3.34 8.78 7.46 7 12 7s8.66 1.78 11.71 4.67c.18.18.29.43.29.71 0 .28-.11.53-.29.71l-2.48 2.48c-.18.18-.43.29-.71.29-.27 0-.52-.11-.7-.28-.79-.74-1.69-1.36-2.67-1.85-.33-.16-.56-.5-.56-.9v-3.1C15.15 9.25 13.6 9 12 9z"/></svg>
        </button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  // floating phone FAB
  const fab = document.createElement("button");
  fab.className = "sa-fab";
  fab.id = "sa-fab";
  fab.setAttribute("aria-label", "Start a call");
  fab.innerHTML = `<svg viewBox="0 0 24 24"><path d="M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z"/></svg>`;
  document.body.appendChild(fab);

  // ── Elements ───────────────────────────────────────────────────
  const bannerCta = document.getElementById("sa-banner-cta");
  const bannerOngoing = document.getElementById("sa-banner-ongoing");
  const timerBadge = document.getElementById("sa-timer-badge");
  const callOverlay = document.getElementById("sa-overlay");
  const callCompany = document.getElementById("sa-call-company");
  const callStatus = document.getElementById("sa-call-status");
  const callTranscript = document.getElementById("sa-call-transcript");
  const btnSpeaker = document.getElementById("sa-btn-speaker");
  const btnMic = document.getElementById("sa-btn-mic");
  const btnHangup = document.getElementById("sa-btn-hangup");

  // ── State ──────────────────────────────────────────────────────
  let isCallActive = false;
  let ws = null;
  let mediaStream = null;
  let mediaRecorder = null;
  let timerInterval = null;
  let callSeconds = 0;
  let isMuted = false;
  let isSpeakerOff = false;
  let audioContext = null;

  // ── Timer ──────────────────────────────────────────────────────
  function startTimer() {
    callSeconds = 0;
    updateTimerDisplay();
    timerInterval = setInterval(() => {
      callSeconds++;
      updateTimerDisplay();
    }, 1000);
  }

  function stopTimer() {
    if (timerInterval) clearInterval(timerInterval);
    timerInterval = null;
    callSeconds = 0;
  }

  function updateTimerDisplay() {
    const m = String(Math.floor(callSeconds / 60)).padStart(2, "0");
    const s = String(callSeconds % 60).padStart(2, "0");
    timerBadge.textContent = `${m}:${s}`;
  }

  // ── WebSocket ──────────────────────────────────────────────────
  function getWsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const base = API_BASE.replace(/^https?:/, proto);
    return `${base}/api/v1/voice/${COMPANY_ID}/call`;
  }

  function connectWebSocket() {
    return new Promise((resolve, reject) => {
      const url = getWsUrl();
      ws = new WebSocket(url);

      ws.onopen = () => {
        ws.send(JSON.stringify({
          type: "start",
          session_id: getSessionId(),
        }));
        resolve();
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          handleServerMessage(msg);
        } catch (e) {
          console.error("[SwiftAgent] Bad WS message", e);
        }
      };

      ws.onerror = (err) => {
        console.error("[SwiftAgent] WebSocket error", err);
        reject(err);
      };

      ws.onclose = () => {
        if (isCallActive) endCall();
      };
    });
  }

  function handleServerMessage(msg) {
    switch (msg.type) {
      case "status":
        updateCallStatus(msg.status);
        break;
      case "transcript":
        appendTranscript("You", msg.text);
        break;
      case "reply_text":
        appendTranscript("Agent", msg.text);
        break;
      case "audio":
        playAudio(msg.data);
        break;
      case "error":
        console.error("[SwiftAgent] Server error:", msg.message);
        callStatus.textContent = "Error — please try again";
        break;
    }
  }

  function updateCallStatus(status) {
    const labels = {
      ready: "Connected",
      transcribing: "Listening...",
      thinking: "Thinking...",
      ended: "Call ended",
    };
    callStatus.textContent = labels[status] || status;
  }

  function appendTranscript(speaker, text) {
    const line = document.createElement("p");
    line.style.marginBottom = "8px";
    line.style.textAlign = "left";
    line.innerHTML = `<strong style="color:#333">${speaker}:</strong> ${text}`;
    callTranscript.appendChild(line);
    callTranscript.scrollTop = callTranscript.scrollHeight;
  }

  // ── Audio Playback ─────────────────────────────────────────────
  function playAudio(base64Data) {
    if (isSpeakerOff) return;
    const bytes = Uint8Array.from(atob(base64Data), c => c.charCodeAt(0));
    const blob = new Blob([bytes], { type: "audio/mp3" });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.play().catch(e => console.warn("[SwiftAgent] Audio play failed", e));
    audio.onended = () => URL.revokeObjectURL(url);
  }

  // ── Mic Recording ──────────────────────────────────────────────
  async function startRecording() {
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      console.error("[SwiftAgent] Mic access denied", e);
      callStatus.textContent = "Microphone access required";
      return false;
    }

    audioContext = new AudioContext();
    mediaRecorder = new MediaRecorder(mediaStream, {
      mimeType: MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm",
    });

    let chunks = [];
    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
      if (chunks.length === 0 || !ws || ws.readyState !== WebSocket.OPEN) return;

      const blob = new Blob(chunks, { type: "audio/webm" });
      chunks = [];

      const buffer = await blob.arrayBuffer();
      const base64 = btoa(
        new Uint8Array(buffer).reduce((data, byte) => data + String.fromCharCode(byte), "")
      );

      ws.send(JSON.stringify({ type: "audio", data: base64 }));
      ws.send(JSON.stringify({ type: "stop_audio" }));
    };

    // record in 4-second intervals for natural pauses
    function recordCycle() {
      if (!isCallActive || isMuted) return;
      chunks = [];
      mediaRecorder.start();
      setTimeout(() => {
        if (mediaRecorder && mediaRecorder.state === "recording") {
          mediaRecorder.stop();
          // start next cycle after a brief gap
          setTimeout(recordCycle, 200);
        }
      }, 4000);
    }

    recordCycle();
    return true;
  }

  function stopRecording() {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      mediaRecorder.stop();
    }
    mediaRecorder = null;
    if (mediaStream) {
      mediaStream.getTracks().forEach(t => t.stop());
      mediaStream = null;
    }
    if (audioContext) {
      audioContext.close();
      audioContext = null;
    }
  }

  // ── Call Lifecycle ──────────────────────────────────────────────
  async function startCall() {
    if (isCallActive) return;

    callTranscript.innerHTML = "";
    callStatus.textContent = "Calling...";
    callOverlay.classList.add("sa-active");
    bannerCta.style.display = "none";
    bannerOngoing.classList.add("sa-active");

    try {
      await connectWebSocket();
      const micOk = await startRecording();
      if (!micOk) {
        endCall();
        return;
      }
      isCallActive = true;
      startTimer();
      callStatus.textContent = "Connected";
    } catch (e) {
      console.error("[SwiftAgent] Failed to start call", e);
      callStatus.textContent = "Connection failed";
      setTimeout(endCall, 2000);
    }
  }

  function endCall() {
    isCallActive = false;
    stopTimer();
    stopRecording();

    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "end" }));
      ws.close();
    }
    ws = null;

    callOverlay.classList.remove("sa-active");
    bannerOngoing.classList.remove("sa-active");
    bannerCta.style.display = "flex";

    isMuted = false;
    isSpeakerOff = false;
    btnMic.classList.remove("sa-muted");
    btnSpeaker.classList.remove("sa-muted");
  }

  // ── Control Buttons ────────────────────────────────────────────
  bannerCta.addEventListener("click", startCall);
  fab.addEventListener("click", startCall);
  btnHangup.addEventListener("click", endCall);

  btnMic.addEventListener("click", () => {
    isMuted = !isMuted;
    btnMic.classList.toggle("sa-muted", isMuted);
    if (mediaStream) {
      mediaStream.getAudioTracks().forEach(t => (t.enabled = !isMuted));
    }
  });

  btnSpeaker.addEventListener("click", () => {
    isSpeakerOff = !isSpeakerOff;
    btnSpeaker.classList.toggle("sa-muted", isSpeakerOff);
  });

  // ── Load Company Config ────────────────────────────────────────
  async function loadConfig() {
    try {
      const resp = await fetch(`${API_BASE}/api/v1/widget/${COMPANY_ID}/config`);
      if (!resp.ok) return;
      const config = await resp.json();
      if (config.name) {
        callCompany.textContent = config.name;
      }
    } catch (e) {
      console.warn("[SwiftAgent] Could not load config", e);
    }
  }

  // ── Initialize ─────────────────────────────────────────────────
  loadConfig();
})();
