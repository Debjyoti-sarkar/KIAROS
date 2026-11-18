/**
 * KIAROS — main entry point.
 *
 * Wires the orb, WebSocket, speech input/output and phone-side actions into one
 * experience that runs in a laptop browser, a phone browser (PWA), or the Android app.
 */

import { createOrb, type OrbState } from "./orb";
import { createVoiceInput, createAudioPlayer, speakText, stopSpeaking } from "./voice";
import { createSocket } from "./ws";
import { openSettings, checkFirstTimeSetup, openPairing } from "./settings";
import {
  apiFetch, authedUrl, device, getServer, getToken, isConfigured, isLocalLaptop, isNative,
  platformName, setConnection, wsUrl,
} from "./config";
import "./style.css";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

type State = "idle" | "listening" | "thinking" | "speaking";
let currentState: State = "idle";
let isMuted = false;
const DEVICE = device();
const MODE = DEVICE === "laptop" ? "continuous" : "push";

const $ = (id: string) => document.getElementById(id)!;
const statusEl = $("status-text");
const errorEl = $("error-text");
const captionsEl = $("captions");
const talkBtn = $("btn-talk") as HTMLButtonElement;
const connEl = $("conn-dot");

document.body.classList.add(`device-${DEVICE}`, `mode-${MODE}`);
if (isNative) document.body.classList.add("native");

function showError(msg: string) {
  errorEl.textContent = msg;
  errorEl.style.opacity = "1";
  setTimeout(() => (errorEl.style.opacity = "0"), 6000);
}

function caption(who: "you" | "kiaros", text: string, fromDevice = "") {
  const line = document.createElement("div");
  line.className = `caption caption-${who}`;
  line.textContent = text;
  if (fromDevice && fromDevice !== DEVICE) {
    // Said on another device — show where it came from.
    const tag = document.createElement("span");
    tag.className = "caption-device";
    tag.textContent = fromDevice === "phone" ? "on phone" : "on laptop";
    line.prepend(tag);
  }
  captionsEl.appendChild(line);
  while (captionsEl.children.length > 4) captionsEl.firstChild?.remove();
}

function updateStatus(state: State) {
  const labels: Record<State, string> = {
    idle: MODE === "push" ? "tap to talk" : "",
    listening: "listening…",
    thinking: "thinking…",
    speaking: "",
  };
  statusEl.textContent = labels[state];
}

// ---------------------------------------------------------------------------
// Connect screen (Android app first run, or when the token is rejected)
// ---------------------------------------------------------------------------

function showConnectScreen(message = "") {
  const screen = $("connect-screen");
  (document.getElementById("connect-server") as HTMLInputElement).value = getServer() || "";
  (document.getElementById("connect-token") as HTMLInputElement).value = getToken();
  $("connect-message").textContent = message;
  screen.style.display = "flex";
}

$("connect-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const server = (document.getElementById("connect-server") as HTMLInputElement).value;
  const token = (document.getElementById("connect-token") as HTMLInputElement).value;
  try {
    setConnection(server, token);
  } catch {
    $("connect-message").textContent = "That server address doesn't look right.";
    return;
  }
  $("connect-message").textContent = "Connecting…";
  try {
    const res = await apiFetch("/api/settings/preferences");
    if (res.status === 401) {
      $("connect-message").textContent = "The server rejected that token.";
      return;
    }
    if (!res.ok) throw new Error(String(res.status));
  } catch {
    $("connect-message").textContent = "Couldn't reach KIAROS. Is the laptop on the same Wi-Fi and the server running?";
    return;
  }
  $("connect-screen").style.display = "none";
  socket.reconnect();
});

// ---------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------

const orb = createOrb($("orb-canvas") as HTMLCanvasElement);
const audioPlayer = createAudioPlayer();
orb.setAnalyser(audioPlayer.getAnalyser());

function transition(newState: State) {
  if (newState === currentState) return;
  currentState = newState;
  orb.setState(newState as OrbState);
  updateStatus(newState);
  talkBtn.classList.toggle("active", newState === "listening");
  if (MODE === "continuous") {
    if (newState === "idle" || newState === "listening") {
      if (!isMuted) voiceInput.resume();
    } else {
      voiceInput.pause();
    }
  }
}

const voiceInput = createVoiceInput(
  MODE,
  (text) => {
    audioPlayer.stop();
    stopSpeaking();
    caption("you", text);
    socket.send({ type: "transcript", text, isFinal: true });
    transition("thinking");
  },
  showError,
  (listening) => {
    if (listening) transition("listening");
    else if (currentState === "listening") transition("idle");
  }
);

audioPlayer.onFinished(() => transition("idle"));

// ---------------------------------------------------------------------------
// Phone-side actions
// ---------------------------------------------------------------------------

function actionUrl(msg: Record<string, unknown>): { url: string; label: string } | null {
  const action = String(msg.action || "");
  const target = String(msg.target || "");
  const number = String(msg.number || "");
  const message = String(msg.message || "");
  switch (action) {
    case "call":
      return { url: `tel:${number}`, label: `Call ${target || number}` };
    case "sms":
      return { url: `sms:${number}${message ? `?body=${encodeURIComponent(message)}` : ""}`, label: `Text ${target || number}` };
    case "whatsapp":
      return { url: `https://wa.me/${number.replace(/\D/g, "")}${message ? `?text=${encodeURIComponent(message)}` : ""}`, label: `WhatsApp ${target || number}` };
    case "navigate":
      return { url: `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(target)}`, label: `Navigate to ${target}` };
    case "open_url": {
      const url = msg.server_relative ? authedUrl(target) : target;
      if (!/^https?:\/\//i.test(url)) return null;
      return { url, label: msg.server_relative ? "Open report" : `Open ${new URL(url).hostname}` };
    }
    default:
      return null;
  }
}

function launch(url: string) {
  if (/^https?:/i.test(url) && !isNative) window.open(url, "_blank", "noopener");
  else window.location.href = url; // Capacitor hands tel:/sms:/external links to Android
}

function handleDeviceAction(msg: Record<string, unknown>) {
  if (msg.action === "alert") {
    navigator.vibrate?.([300, 150, 300, 150, 300]);
    caption("kiaros", String(msg.message || "Time's up."));
    return;
  }
  const a = actionUrl(msg);
  if (!a) return;
  if (isNative) {
    launch(a.url);
    return;
  }
  // Browsers only allow opening apps from a tap, so offer a one-tap card.
  const card = $("action-card");
  const btn = $("action-card-btn") as HTMLButtonElement;
  btn.textContent = a.label;
  btn.onclick = () => {
    launch(a.url);
    card.style.display = "none";
  };
  card.style.display = "flex";
  setTimeout(() => (card.style.display = "none"), 30000);
}
$("action-card-close").addEventListener("click", () => ($("action-card").style.display = "none"));

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------

const socket = createSocket(wsUrl, (send) => {
  send({ type: "hello", device: DEVICE, platform: platformName(), native: isNative });
});

socket.onStatus((s) => {
  connEl.className = `conn-${s}`;
  if (s === "unauthorized") {
    if (isNative) showConnectScreen("The server rejected this device's token. Re-enter it from the laptop's Pair Phone screen.");
    else if (!isLocalLaptop()) showError("Not paired. Scan the pairing QR code on the laptop (menu → Pair phone).");
  }
});

socket.onMessage(async (msg) => {
  const type = msg.type as string;
  if (type === "audio") {
    const data = msg.data as string;
    if (msg.text) caption("kiaros", String(msg.text));
    if (data) {
      transition("speaking");
      audioPlayer.enqueue(data);
    } else {
      transition("idle");
    }
  } else if (type === "text") {
    const text = String(msg.text || "");
    caption("kiaros", text);
    if (msg.speak && text) {
      transition("speaking");
      await speakText(text);
      if (currentState === "speaking") transition("idle");
    } else {
      transition("idle");
    }
  } else if (type === "status") {
    const state = msg.state as string;
    if (state === "thinking") transition("thinking");
    else if (state === "working") {
      transition("thinking");
      statusEl.textContent = "working…";
    } else if (state === "idle" && currentState !== "speaking") transition("idle");
  } else if (type === "device_action") {
    handleDeviceAction(msg);
  } else if (type === "sync") {
    // A turn happening on another device: mirror it silently.
    caption(msg.role === "user" ? "you" : "kiaros", String(msg.text || ""), String(msg.device || ""));
  } else if (type === "history") {
    captionsEl.innerHTML = "";
    for (const m of (msg.messages as { role: string; text: string; device: string }[]) || []) {
      caption(m.role === "user" ? "you" : "kiaros", m.text, m.device);
    }
    captionsEl.classList.add("restored");
  } else if (type === "presence") {
    const devices = (msg.devices as { device: string }[]) || [];
    for (const kind of ["laptop", "phone"]) {
      const n = devices.filter((d) => d.device === kind).length;
      const el = $(`dev-${kind}`);
      el.classList.toggle("online", n > 0);
      el.classList.toggle("self", kind === DEVICE);
      el.title = `${kind[0].toUpperCase() + kind.slice(1)}: ${n > 0 ? "connected" : "offline"}`;
    }
  } else if (type === "task_complete" || type === "qa_result" || type === "suggestion") {
    console.log("[task]", msg);
  }
});

// ---------------------------------------------------------------------------
// Input: continuous on the laptop, tap-to-talk on the phone, keyboard anywhere
// ---------------------------------------------------------------------------

async function unlockAudio() {
  await audioPlayer.resume().catch(() => {});
}
document.addEventListener("click", unlockAudio);
document.addEventListener("touchstart", unlockAudio, { passive: true });

function pushToTalk() {
  void unlockAudio();
  if (currentState === "speaking") {
    audioPlayer.stop();
    stopSpeaking();
    transition("idle");
  }
  void voiceInput.listenOnce();
}

talkBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (MODE === "push") pushToTalk();
  else toggleMute();
});
$("orb-canvas").addEventListener("click", () => {
  if (MODE === "push") pushToTalk();
});

const typeForm = $("type-form") as HTMLFormElement;
const typeInput = $("type-input") as HTMLInputElement;
$("btn-keyboard").addEventListener("click", (e) => {
  e.stopPropagation();
  typeForm.classList.toggle("open");
  if (typeForm.classList.contains("open")) typeInput.focus();
});
typeForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = typeInput.value.trim();
  if (!text) return;
  typeInput.value = "";
  audioPlayer.stop();
  stopSpeaking();
  caption("you", text);
  socket.send({ type: "transcript", text, isFinal: true });
  transition("thinking");
});

function toggleMute() {
  isMuted = !isMuted;
  $("btn-talk").classList.toggle("muted", isMuted);
  if (isMuted) {
    voiceInput.pause();
    transition("idle");
    statusEl.textContent = "muted";
  } else {
    voiceInput.resume();
    transition("listening");
  }
}

// ---------------------------------------------------------------------------
// Menu
// ---------------------------------------------------------------------------

const menuDropdown = $("menu-dropdown");
$("btn-menu").addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = menuDropdown.style.display === "none" ? "block" : "none";
});
document.addEventListener("click", () => (menuDropdown.style.display = "none"));

const menu = (id: string, fn: () => void) =>
  $(id).addEventListener("click", (e) => {
    e.stopPropagation();
    menuDropdown.style.display = "none";
    fn();
  });

menu("btn-settings", () => openSettings());
menu("btn-pair", () => openPairing());
menu("btn-connection", () => showConnectScreen());
menu("btn-restart", async () => {
  statusEl.textContent = "restarting…";
  try {
    await apiFetch("/api/restart", { method: "POST" });
    setTimeout(() => socket.reconnect(), 4000);
  } catch {
    statusEl.textContent = "restart failed";
  }
});
menu("btn-fix-self", () => {
  socket.send({ type: "fix_self" });
  statusEl.textContent = "entering work mode…";
});

if (!isLocalLaptop()) $("btn-pair").style.display = "none";
if (!isNative) $("btn-connection").style.display = "none";

// ---------------------------------------------------------------------------
// Kick off
// ---------------------------------------------------------------------------

updateStatus(currentState); // show "tap to talk" on phones right away

if (!isConfigured()) {
  showConnectScreen("Enter the address and token shown on your laptop (KIAROS menu → Pair phone).");
}

setTimeout(() => {
  voiceInput.start();
  transition(MODE === "continuous" ? "listening" : "idle");
}, 800);

setTimeout(() => {
  if (isConfigured()) void checkFirstTimeSetup();
}, 2000);

// Installable PWA (phone browser over HTTPS, or laptop localhost)
if (!isNative && "serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("[sw]", e));
}
