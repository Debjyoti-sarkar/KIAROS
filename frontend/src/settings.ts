/**
 * KIAROS — Settings and phone pairing panels.
 *
 * Slide-in overlay for API keys, integrations (calendar, mail), preferences,
 * status, and the "Pair phone" QR screen shown on the laptop.
 */

import qrcode from "qrcode-generator";
import { apiFetch } from "./config";

interface StatusResponse {
  claude_code_installed: boolean;
  calendar_configured: boolean;
  mail_configured: boolean;
  notes_dir: string;
  screenshot_available: boolean;
  voice: "fish" | "device";
  ai_provider: string;
  memory_count: number;
  task_count: number;
  devices: { device: string; platform: string }[];
  server_port: number;
  uptime_seconds: number;
  env_keys_set: {
    anthropic: boolean;
    gemini: boolean;
    llm: boolean;
    fish_audio: boolean;
    fish_voice_id: boolean;
    calendar: boolean;
    mail_address: string;
    mail_password: boolean;
    user_name: string;
  };
}

interface PreferencesResponse {
  user_name: string;
  honorific: string;
  weather_location: string;
}

interface PairingResponse {
  token: string;
  ips: string[];
  app_url: string;
  browser_url: string;
  https_enabled: boolean;
}

let panelEl: HTMLElement | null = null;
let isOpen = false;

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function apiGet<T>(url: string): Promise<T> {
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return res.json();
}

async function apiPost<T>(url: string, body: unknown = {}): Promise<T> {
  const res = await apiFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

const saveKey = (key_name: string, key_value: string) => apiPost("/api/settings/keys", { key_name, key_value });

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

// ---------------------------------------------------------------------------
// Panel HTML
// ---------------------------------------------------------------------------

function field(label: string, id: string, opts: { type?: string; placeholder?: string; test?: string; hint?: string } = {}): string {
  return `
    <div class="settings-field">
      <label for="${id}">${label}</label>
      <div class="settings-input-row">
        <input type="${opts.type || "text"}" id="${id}" placeholder="${esc(opts.placeholder || "")}" autocomplete="off" />
        ${opts.test ? `<button class="settings-btn" id="${opts.test}">Test</button><span class="status-dot" id="${opts.test}-dot"></span>` : ""}
      </div>
      ${opts.hint ? `<div class="status-detail">${opts.hint}</div>` : ""}
    </div>`;
}

function buildPanelHTML(): string {
  return `
    <div class="settings-backdrop" id="settings-backdrop"></div>
    <div class="settings-panel" id="settings-panel-inner">
      <div class="settings-header">
        <h2>Settings</h2>
        <button class="settings-close" id="settings-close" aria-label="Close">&times;</button>
      </div>

      <div class="settings-welcome" id="settings-welcome" style="display:none">
        <p>Welcome to KIAROS. Add a Google Gemini API key (or an Anthropic key) to get started — everything else is optional.</p>
      </div>

      <div class="settings-body">
        <section class="settings-section">
          <h3>AI &amp; Voice</h3>
          ${field("Google Gemini API key", "input-gemini-key", { type: "password", placeholder: "AIza…", test: "btn-test-gemini",
            hint: "From aistudio.google.com/apikey. KIAROS picks the newest Gemini Flash and Pro models your key can use." })}
          ${field("Anthropic API key (optional alternative)", "input-anthropic-key", { type: "password", placeholder: "sk-ant-…", test: "btn-test-anthropic",
            hint: "If both keys are set, Gemini is used." })}
          ${field("Fish Audio API key (optional)", "input-fish-key", { type: "password", placeholder: "leave empty to use the device's voice", test: "btn-test-fish" })}
          ${field("Fish voice model ID (optional)", "input-fish-voice-id", { placeholder: "voice model id from fish.audio" })}
          <div class="settings-actions"><button class="settings-btn primary" id="btn-save-keys">Save</button></div>
        </section>

        <section class="settings-section">
          <h3>Calendar</h3>
          ${field("ICS calendar link(s)", "input-calendar", {
            type: "password",
            placeholder: "https://calendar.google.com/…/basic.ics",
            test: "btn-test-calendar",
            hint: "Google Calendar → Settings → your calendar → Secret address in iCal format. Separate several with commas.",
          })}
          <div class="settings-actions"><button class="settings-btn primary" id="btn-save-calendar">Save</button></div>
        </section>

        <section class="settings-section">
          <h3>Email (read-only)</h3>
          ${field("Email address", "input-mail-address", { type: "email", placeholder: "you@gmail.com" })}
          ${field("App password", "input-mail-password", { type: "password", placeholder: "16-character app password", test: "btn-test-mail",
            hint: "Gmail: myaccount.google.com/apppasswords. KIAROS can only read — never send or delete." })}
          ${field("IMAP server", "input-mail-host", { placeholder: "imap.gmail.com" })}
          <div class="settings-actions"><button class="settings-btn primary" id="btn-save-mail">Save</button></div>
        </section>

        <section class="settings-section">
          <h3>Preferences</h3>
          <div class="settings-field"><label for="input-user-name">Your name</label><input type="text" id="input-user-name" placeholder="Your name" /></div>
          <div class="settings-field">
            <label for="input-honorific">How KIAROS addresses you</label>
            <select id="input-honorific">
              <option value="sir">Sir</option>
              <option value="ma'am">Ma'am</option>
              <option value="boss">Boss</option>
              <option value="">By name only</option>
            </select>
          </div>
          <div class="settings-field"><label for="input-weather">Weather location</label><input type="text" id="input-weather" placeholder="auto (from your IP), or e.g. Kolkata" /></div>
          <div class="settings-actions"><button class="settings-btn primary" id="btn-save-prefs">Save</button></div>
        </section>

        <section class="settings-section">
          <h3>Status</h3>
          <div class="status-grid">
            <div class="status-row"><span class="status-dot" id="status-server"></span><span>Server</span><span class="status-detail" id="status-server-detail"></span></div>
            <div class="status-row"><span class="status-dot" id="status-claude-cli"></span><span>Claude Code CLI (for builds)</span></div>
            <div class="status-row"><span class="status-dot" id="status-calendar"></span><span>Calendar</span></div>
            <div class="status-row"><span class="status-dot" id="status-mail"></span><span>Email</span></div>
            <div class="status-row"><span class="status-dot" id="status-screen"></span><span>Screenshots</span><span class="status-detail" id="status-screen-detail"></span></div>
            <div class="status-row"><span class="status-dot" id="status-ai"></span><span>AI</span><span class="status-detail" id="status-ai-detail"></span></div>
            <div class="status-row"><span class="status-dot status-green"></span><span>Voice</span><span class="status-detail" id="status-voice"></span></div>
            <div class="status-row"><span class="status-dot status-green"></span><span>Devices</span><span class="status-detail" id="status-devices"></span></div>
          </div>
        </section>

        <section class="settings-section">
          <h3>System</h3>
          <div class="sysinfo-grid">
            <div class="sysinfo-row"><span class="sysinfo-label">Memories</span><span id="sysinfo-memory">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">Open tasks</span><span id="sysinfo-tasks">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">Notes folder</span><span id="sysinfo-notes">--</span></div>
          </div>
        </section>
      </div>
    </div>
  `;
}

function buildPairingHTML(): string {
  return `
    <div class="settings-backdrop" id="pair-backdrop"></div>
    <div class="settings-panel">
      <div class="settings-header">
        <h2>Pair phone</h2>
        <button class="settings-close" id="pair-close" aria-label="Close">&times;</button>
      </div>
      <div class="settings-body" id="pair-body"><p class="status-detail">Loading…</p></div>
    </div>`;
}

// ---------------------------------------------------------------------------
// Panel helpers
// ---------------------------------------------------------------------------

function mount(html: string, id: string): HTMLElement {
  const container = document.createElement("div");
  container.id = id;
  container.className = "kiaros-overlay";
  container.innerHTML = html;
  // Reuse the settings-container styling for every overlay.
  container.setAttribute("data-overlay", "");
  document.body.appendChild(container);
  return container;
}

function show(el: HTMLElement) {
  el.style.display = "block";
  requestAnimationFrame(() => el.classList.add("open"));
}

function hide(el: HTMLElement) {
  el.classList.remove("open");
  setTimeout(() => (el.style.display = "none"), 300);
}

function setDot(id: string, status: "green" | "red" | "yellow" | "off") {
  const dot = document.getElementById(id);
  if (!dot) return;
  dot.className = "status-dot";
  if (status !== "off") dot.classList.add(`status-${status}`);
}

const val = (id: string) => (document.getElementById(id) as HTMLInputElement | HTMLSelectElement).value.trim();
const setVal = (id: string, v: string) => ((document.getElementById(id) as HTMLInputElement | HTMLSelectElement).value = v);

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

async function loadStatus(): Promise<StatusResponse | null> {
  try {
    const s = await apiGet<StatusResponse>("/api/settings/status");
    setDot("status-server", "green");
    document.getElementById("status-server-detail")!.textContent = `port ${s.server_port} · up ${formatUptime(s.uptime_seconds)}`;
    setDot("status-claude-cli", s.claude_code_installed ? "green" : "red");
    setDot("status-calendar", s.calendar_configured ? "green" : "off");
    setDot("status-mail", s.mail_configured ? "green" : "off");
    setDot("status-screen", s.screenshot_available ? "green" : "yellow");
    document.getElementById("status-screen-detail")!.textContent = s.screenshot_available ? "" : "install gnome-screenshot";
    document.getElementById("status-voice")!.textContent = s.voice === "fish" ? "Fish Audio" : "device voice";
    document.getElementById("status-devices")!.textContent =
      s.devices.map((d) => d.device).join(", ") || "none";
    setDot("btn-test-gemini-dot", s.env_keys_set.gemini ? "green" : "off");
    setDot("btn-test-anthropic-dot", s.env_keys_set.anthropic ? "green" : "off");
    setDot("status-ai", s.env_keys_set.llm ? "green" : "red");
    document.getElementById("status-ai-detail")!.textContent = s.ai_provider;
    setDot("btn-test-fish-dot", s.env_keys_set.fish_audio ? "green" : "off");
    setDot("btn-test-calendar-dot", s.env_keys_set.calendar ? "green" : "off");
    setDot("btn-test-mail-dot", s.env_keys_set.mail_password ? "green" : "off");
    if (s.env_keys_set.mail_address) setVal("input-mail-address", s.env_keys_set.mail_address);
    document.getElementById("sysinfo-memory")!.textContent = String(s.memory_count);
    document.getElementById("sysinfo-tasks")!.textContent = String(s.task_count);
    document.getElementById("sysinfo-notes")!.textContent = s.notes_dir;
    return s;
  } catch (e) {
    console.error("[settings] status failed:", e);
    setDot("status-server", "red");
    return null;
  }
}

async function loadPreferences() {
  try {
    const p = await apiGet<PreferencesResponse>("/api/settings/preferences");
    setVal("input-user-name", p.user_name || "");
    setVal("input-honorific", p.honorific ?? "sir");
    setVal("input-weather", p.weather_location || "");
  } catch (e) {
    console.error("[settings] preferences failed:", e);
  }
}

async function runTest(btnId: string, url: string, body: unknown = {}) {
  setDot(`${btnId}-dot`, "yellow");
  try {
    const r = await apiPost<{ valid: boolean; error?: string; model?: string; research_model?: string }>(url, body);
    setDot(`${btnId}-dot`, r.valid ? "green" : "red");
    const btn = document.getElementById(btnId)!;
    btn.title = r.valid ? (r.model ? `Working: ${r.model} (research: ${r.research_model})` : "Working") : r.error || "Failed";
    if (r.valid && r.model) alert(`Gemini key works.\nConversation: ${r.model}\nResearch: ${r.research_model}\n\nClick Save to use it.`);
    if (!r.valid && r.error) alert(r.error);
  } catch {
    setDot(`${btnId}-dot`, "red");
  }
}

function wireEvents() {
  const on = (id: string, fn: () => void) => document.getElementById(id)?.addEventListener("click", fn);
  on("settings-close", closeSettings);
  on("settings-backdrop", closeSettings);

  on("btn-save-keys", async () => {
    const g = val("input-gemini-key"), a = val("input-anthropic-key"), f = val("input-fish-key"), v = val("input-fish-voice-id");
    if (g) await saveKey("GEMINI_API_KEY", g);
    if (a) await saveKey("ANTHROPIC_API_KEY", a);
    if (f) await saveKey("FISH_API_KEY", f);
    if (v) await saveKey("FISH_VOICE_ID", v);
    setVal("input-gemini-key", "");
    setVal("input-anthropic-key", "");
    setVal("input-fish-key", "");
    await loadStatus();
  });
  on("btn-test-gemini", () => runTest("btn-test-gemini", "/api/settings/test-gemini", { key_value: val("input-gemini-key") || undefined }));
  on("btn-test-anthropic", () => runTest("btn-test-anthropic", "/api/settings/test-anthropic", { key_value: val("input-anthropic-key") || undefined }));
  on("btn-test-fish", () => runTest("btn-test-fish", "/api/settings/test-fish", { key_value: val("input-fish-key") || undefined }));

  on("btn-save-calendar", async () => {
    await saveKey("CALENDAR_ICS_URLS", val("input-calendar"));
    setVal("input-calendar", "");
    await loadStatus();
  });
  on("btn-test-calendar", () => runTest("btn-test-calendar", "/api/settings/test-calendar"));

  on("btn-save-mail", async () => {
    await saveKey("MAIL_ADDRESS", val("input-mail-address"));
    if (val("input-mail-password")) await saveKey("MAIL_APP_PASSWORD", val("input-mail-password"));
    if (val("input-mail-host")) await saveKey("MAIL_IMAP_HOST", val("input-mail-host"));
    setVal("input-mail-password", "");
    await loadStatus();
  });
  on("btn-test-mail", () => runTest("btn-test-mail", "/api/settings/test-mail"));

  on("btn-save-prefs", async () => {
    await apiPost("/api/settings/preferences", {
      user_name: val("input-user-name"),
      honorific: val("input-honorific"),
      weather_location: val("input-weather"),
    });
    await loadStatus();
  });
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function openSettings() {
  if (isOpen) return;
  isOpen = true;
  if (!panelEl) {
    panelEl = mount(buildPanelHTML(), "settings-container");
    wireEvents();
  }
  show(panelEl);
  const status = await loadStatus();
  await loadPreferences();
  const welcome = document.getElementById("settings-welcome")!;
  welcome.style.display = status && !status.env_keys_set.llm ? "block" : "none";
}

const SETUP_DISMISSED = "kiaros.setupDismissed";

export function closeSettings() {
  if (!panelEl || !isOpen) return;
  isOpen = false;
  hide(panelEl);
  // Don't keep popping the first-run panel open on every reload once dismissed.
  try { sessionStorage.setItem(SETUP_DISMISSED, "1"); } catch { /* ignore */ }
}

export function isSettingsOpen(): boolean {
  return isOpen;
}

let pairEl: HTMLElement | null = null;

function qrImg(text: string): string {
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  return qr.createImgTag(5, 0);
}

export async function openPairing() {
  if (!pairEl) {
    pairEl = mount(buildPairingHTML(), "pairing-container");
    document.getElementById("pair-close")!.addEventListener("click", () => hide(pairEl!));
    document.getElementById("pair-backdrop")!.addEventListener("click", () => hide(pairEl!));
  }
  show(pairEl);
  const body = document.getElementById("pair-body")!;
  try {
    const p = await apiGet<PairingResponse>("/api/pairing");
    if (!p.ips.length) {
      body.innerHTML = `<p class="status-detail">Couldn't find this laptop's network address. Is Wi-Fi connected?</p>`;
      return;
    }
    body.innerHTML = `
      <section class="settings-section">
        <h3>Android app</h3>
        <ol class="pair-steps">
          <li>Install <b>KIAROS.apk</b> on the phone.</li>
          <li>Open it and enter the server address and token below.</li>
        </ol>
        <div class="settings-field"><label>Server address</label><div class="pair-code">${esc(p.app_url)}</div></div>
        <div class="settings-field"><label>Token</label><div class="pair-code">${esc(p.token)}</div></div>
      </section>
      <section class="settings-section">
        <h3>Phone browser (no install)</h3>
        ${p.https_enabled ? `
          <p class="pair-steps">Scan with the phone camera, accept the certificate warning once, then use Chrome's menu → <b>Add to Home screen</b>.</p>
          <div class="pair-qr">${qrImg(p.browser_url)}</div>
          <div class="pair-code">${esc(p.browser_url)}</div>` : `
          <p class="pair-steps">Phone browsers need HTTPS for the microphone. Run <code>scripts/setup.sh</code> again to create a certificate, then restart KIAROS.</p>`}
      </section>
      <section class="settings-section">
        <p class="pair-steps">Both devices must be on the same Wi-Fi, or on the same Tailscale network to use KIAROS away from home. Keep the token private — it lets a device control this laptop.</p>
      </section>`;
  } catch (e) {
    body.innerHTML = `<p class="status-detail">Pairing details are only available on the laptop itself.</p>`;
    console.error(e);
  }
}

/** Auto-open settings on first run (no Anthropic key yet). */
export async function checkFirstTimeSetup(): Promise<boolean> {
  try {
    if (sessionStorage.getItem(SETUP_DISMISSED)) return false;
  } catch { /* ignore */ }
  try {
    const status = await apiGet<StatusResponse>("/api/settings/status");
    if (!status.env_keys_set.llm && !document.querySelector(".kiaros-overlay.open")) {
      openSettings();
      return true;
    }
  } catch {
    /* server not ready or not paired */
  }
  return false;
}
