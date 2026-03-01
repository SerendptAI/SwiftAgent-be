/**
 * SwiftAgent Chat Widget
 * Self-contained, embeddable chat widget for customer support.
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
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');

    .sa-widget * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }

    .sa-widget {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 99999;
    }

    /* ── Floating Button ── */
    .sa-fab {
      width: 60px;
      height: 60px;
      border-radius: 50%;
      background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
      border: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 24px rgba(99, 102, 241, 0.4);
      transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .sa-fab:hover {
      transform: scale(1.08);
      box-shadow: 0 6px 32px rgba(99, 102, 241, 0.5);
    }
    .sa-fab svg {
      width: 28px;
      height: 28px;
      fill: white;
      transition: transform 0.3s ease;
    }
    .sa-fab.sa-open svg {
      transform: rotate(90deg);
    }

    /* ── Chat Window ── */
    .sa-window {
      position: absolute;
      bottom: 76px;
      right: 0;
      width: 400px;
      max-width: calc(100vw - 32px);
      height: 560px;
      max-height: calc(100vh - 120px);
      background: #0f1117;
      border-radius: 16px;
      overflow: hidden;
      display: none;
      flex-direction: column;
      box-shadow: 0 8px 48px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(255, 255, 255, 0.06);
      animation: sa-slide-up 0.25s ease-out;
    }
    .sa-window.sa-visible {
      display: flex;
    }

    @keyframes sa-slide-up {
      from { opacity: 0; transform: translateY(16px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    /* ── Header ── */
    .sa-header {
      padding: 16px 20px;
      background: linear-gradient(135deg, #6366f1 0%, #7c3aed 100%);
      display: flex;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
    }
    .sa-header-logo {
      width: 36px;
      height: 36px;
      border-radius: 50%;
      background: rgba(255,255,255,0.2);
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }
    .sa-header-logo img {
      width: 100%;
      height: 100%;
      object-fit: cover;
    }
    .sa-header-logo svg {
      width: 20px;
      height: 20px;
      fill: white;
    }
    .sa-header-info h3 {
      font-size: 15px;
      font-weight: 600;
      color: white;
    }
    .sa-header-info p {
      font-size: 12px;
      color: rgba(255,255,255,0.78);
      margin-top: 1px;
    }

    /* ── Messages ── */
    .sa-messages {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      scrollbar-width: thin;
      scrollbar-color: rgba(255,255,255,0.1) transparent;
    }
    .sa-messages::-webkit-scrollbar {
      width: 4px;
    }
    .sa-messages::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.1);
      border-radius: 2px;
    }

    .sa-msg {
      max-width: 85%;
      padding: 10px 14px;
      border-radius: 12px;
      font-size: 14px;
      line-height: 1.5;
      word-wrap: break-word;
      animation: sa-fade-in 0.2s ease;
    }
    @keyframes sa-fade-in {
      from { opacity: 0; transform: translateY(4px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    .sa-msg-user {
      align-self: flex-end;
      background: linear-gradient(135deg, #6366f1, #8b5cf6);
      color: white;
      border-bottom-right-radius: 4px;
    }

    .sa-msg-assistant {
      align-self: flex-start;
      background: #1a1d27;
      color: #e2e8f0;
      border: 1px solid rgba(255,255,255,0.06);
      border-bottom-left-radius: 4px;
    }
    .sa-msg-assistant strong { color: #a5b4fc; }
    .sa-msg-assistant code {
      background: rgba(99, 102, 241, 0.15);
      padding: 1px 5px;
      border-radius: 4px;
      font-size: 13px;
      font-family: 'SF Mono', 'Fira Code', monospace;
      color: #c7d2fe;
    }
    .sa-msg-assistant ul, .sa-msg-assistant ol {
      padding-left: 18px;
      margin: 6px 0;
    }
    .sa-msg-assistant li {
      margin-bottom: 3px;
    }
    .sa-msg-assistant p {
      margin-bottom: 8px;
    }
    .sa-msg-assistant p:last-child {
      margin-bottom: 0;
    }

    /* ── Typing Indicator ── */
    .sa-typing {
      display: none;
      align-self: flex-start;
      padding: 12px 18px;
      background: #1a1d27;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.06);
    }
    .sa-typing.sa-visible { display: flex; gap: 5px; }
    .sa-typing span {
      width: 7px;
      height: 7px;
      background: #6366f1;
      border-radius: 50%;
      animation: sa-bounce 1.2s infinite;
    }
    .sa-typing span:nth-child(2) { animation-delay: 0.15s; }
    .sa-typing span:nth-child(3) { animation-delay: 0.3s; }
    @keyframes sa-bounce {
      0%, 80%, 100% { transform: translateY(0); opacity: 0.4; }
      40% { transform: translateY(-6px); opacity: 1; }
    }

    /* ── Input ── */
    .sa-input-wrap {
      padding: 12px 16px;
      border-top: 1px solid rgba(255,255,255,0.06);
      display: flex;
      gap: 8px;
      background: #13151c;
      flex-shrink: 0;
    }
    .sa-input {
      flex: 1;
      padding: 10px 14px;
      border-radius: 10px;
      border: 1px solid rgba(255,255,255,0.1);
      background: #1a1d27;
      color: #e2e8f0;
      font-size: 14px;
      outline: none;
      transition: border-color 0.2s;
      resize: none;
      min-height: 40px;
      max-height: 100px;
      font-family: inherit;
    }
    .sa-input::placeholder { color: rgba(255,255,255,0.3); }
    .sa-input:focus { border-color: #6366f1; }

    .sa-send {
      width: 40px;
      height: 40px;
      border-radius: 10px;
      border: none;
      background: linear-gradient(135deg, #6366f1, #8b5cf6);
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: opacity 0.2s;
      flex-shrink: 0;
      align-self: flex-end;
    }
    .sa-send:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    .sa-send svg {
      width: 18px;
      height: 18px;
      fill: white;
    }

    /* ── Welcome Message ── */
    .sa-welcome {
      text-align: center;
      padding: 32px 24px;
      color: rgba(255,255,255,0.5);
      font-size: 13px;
      line-height: 1.6;
    }
    .sa-welcome h4 {
      font-size: 16px;
      color: #e2e8f0;
      margin-bottom: 6px;
      font-weight: 600;
    }

    /* ── Powered By ── */
    .sa-powered {
      text-align: center;
      padding: 6px;
      font-size: 10px;
      color: rgba(255,255,255,0.2);
      background: #13151c;
    }
    .sa-powered a {
      color: rgba(255,255,255,0.3);
      text-decoration: none;
    }
  `;

    const styleEl = document.createElement("style");
    styleEl.textContent = STYLES;
    document.head.appendChild(styleEl);

    // ── Build DOM ──────────────────────────────────────────────────
    const widget = document.createElement("div");
    widget.className = "sa-widget";
    widget.innerHTML = `
    <div class="sa-window" id="sa-window">
      <div class="sa-header">
        <div class="sa-header-logo" id="sa-logo">
          <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>
        </div>
        <div class="sa-header-info">
          <h3 id="sa-company-name">Support</h3>
          <p>We typically reply instantly</p>
        </div>
      </div>

      <div class="sa-messages" id="sa-messages">
        <div class="sa-welcome" id="sa-welcome">
          <h4>👋 Hi there!</h4>
          How can we help you today? Ask about transactions, deposits, withdrawals, or anything else.
        </div>
      </div>

      <div class="sa-typing" id="sa-typing">
        <span></span><span></span><span></span>
      </div>

      <div class="sa-input-wrap">
        <textarea class="sa-input" id="sa-input"
          placeholder="Type your message..." rows="1"></textarea>
        <button class="sa-send" id="sa-send" disabled>
          <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
        </button>
      </div>

      <div class="sa-powered">Powered by <a href="https://swiftagents.org" target="_blank">SwiftAgents</a></div>
    </div>

    <button class="sa-fab" id="sa-fab" aria-label="Open chat">
      <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.17L4 17.17V4h16v12z"/></svg>
    </button>
  `;
    document.body.appendChild(widget);

    // ── Elements ───────────────────────────────────────────────────
    const fab = document.getElementById("sa-fab");
    const win = document.getElementById("sa-window");
    const msgs = document.getElementById("sa-messages");
    const input = document.getElementById("sa-input");
    const sendBtn = document.getElementById("sa-send");
    const typing = document.getElementById("sa-typing");
    const welcome = document.getElementById("sa-welcome");
    const nameEl = document.getElementById("sa-company-name");
    const logoEl = document.getElementById("sa-logo");

    let isOpen = false;
    let isLoading = false;

    // ── Toggle ─────────────────────────────────────────────────────
    fab.addEventListener("click", () => {
        isOpen = !isOpen;
        win.classList.toggle("sa-visible", isOpen);
        fab.classList.toggle("sa-open", isOpen);
        if (isOpen) input.focus();
    });

    // ── Input Handling ─────────────────────────────────────────────
    input.addEventListener("input", () => {
        sendBtn.disabled = !input.value.trim() || isLoading;
        // Auto-resize textarea
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 100) + "px";
    });

    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            if (input.value.trim() && !isLoading) sendMessage();
        }
    });

    sendBtn.addEventListener("click", () => {
        if (input.value.trim() && !isLoading) sendMessage();
    });

    // ── Simple Markdown Renderer ───────────────────────────────────
    function renderMarkdown(text) {
        return text
            // Code blocks
            .replace(/```[\s\S]*?```/g, (m) => {
                const code = m.slice(3, -3).replace(/^\w+\n/, "");
                return `<pre><code>${escapeHtml(code.trim())}</code></pre>`;
            })
            // Inline code
            .replace(/`([^`]+)`/g, "<code>$1</code>")
            // Bold
            .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
            // Italic
            .replace(/\*(.+?)\*/g, "<em>$1</em>")
            // Bullet lists
            .replace(/^[\s]*[-•]\s+(.+)/gm, "<li>$1</li>")
            .replace(/(<li>.*<\/li>\n?)+/gs, "<ul>$&</ul>")
            // Numbered lists
            .replace(/^\d+\.\s+(.+)/gm, "<li>$1</li>")
            // Headers
            .replace(/^###\s+(.+)/gm, "<strong>$1</strong>")
            .replace(/^##\s+(.+)/gm, "<strong>$1</strong>")
            // Paragraphs
            .replace(/\n{2,}/g, "</p><p>")
            .replace(/\n/g, "<br>")
            .replace(/^(.+)$/s, "<p>$1</p>");
    }

    function escapeHtml(str) {
        return str.replace(/[&<>"']/g, (m) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m])
        );
    }

    // ── Add Message ────────────────────────────────────────────────
    function addMessage(role, content) {
        if (welcome) welcome.style.display = "none";
        const div = document.createElement("div");
        div.className = `sa-msg sa-msg-${role}`;
        div.innerHTML = role === "user" ? escapeHtml(content) : renderMarkdown(content);
        msgs.appendChild(div);
        msgs.scrollTop = msgs.scrollHeight;
    }

    // ── Send Message ───────────────────────────────────────────────
    async function sendMessage() {
        const text = input.value.trim();
        if (!text) return;

        input.value = "";
        input.style.height = "auto";
        sendBtn.disabled = true;
        isLoading = true;

        addMessage("user", text);
        typing.classList.add("sa-visible");
        msgs.scrollTop = msgs.scrollHeight;

        try {
            const resp = await fetch(`${API_BASE}/api/v1/widget/${COMPANY_ID}/chat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    session_id: getSessionId(),
                    message: text,
                }),
            });

            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || "Request failed");
            }

            const data = await resp.json();
            addMessage("assistant", data.reply || "Sorry, I couldn't generate a response.");
        } catch (err) {
            console.error("[SwiftAgent]", err);
            addMessage(
                "assistant",
                "I'm sorry, I'm having trouble connecting. Please try again in a moment."
            );
        } finally {
            typing.classList.remove("sa-visible");
            isLoading = false;
            sendBtn.disabled = !input.value.trim();
            input.focus();
        }
    }

    // ── Load Company Config ────────────────────────────────────────
    async function loadConfig() {
        try {
            const resp = await fetch(`${API_BASE}/api/v1/widget/${COMPANY_ID}/config`);
            if (!resp.ok) return;
            const config = await resp.json();
            if (config.name) nameEl.textContent = config.name;
            if (config.logo_url) {
                logoEl.innerHTML = `<img src="${config.logo_url}" alt="${config.name}" />`;
            }
        } catch (e) {
            console.warn("[SwiftAgent] Could not load config", e);
        }
    }

    // ── Load Previous Session ──────────────────────────────────────
    async function loadHistory() {
        try {
            const sessionId = getSessionId();
            const resp = await fetch(
                `${API_BASE}/api/v1/widget/${COMPANY_ID}/history/${sessionId}`
            );
            if (!resp.ok) return;
            const data = await resp.json();
            if (data.messages && data.messages.length > 0) {
                data.messages.forEach((m) => addMessage(m.role, m.content));
            }
        } catch (e) {
            console.warn("[SwiftAgent] Could not load history", e);
        }
    }

    // ── Initialize ─────────────────────────────────────────────────
    loadConfig();
    loadHistory();
})();
