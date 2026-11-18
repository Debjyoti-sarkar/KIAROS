/**
 * KIAROS client config — where the server is, how to authenticate, what device this is.
 *
 * - Laptop browser (http://localhost:8340): same origin, no token needed.
 * - Phone browser / installed PWA: same origin; the token arrives once as ?token=… and is kept.
 * - Android app (Capacitor): server URL + token entered on the connect screen and kept.
 */

import { Capacitor } from "@capacitor/core";

const SERVER_KEY = "kiaros.server";
const TOKEN_KEY = "kiaros.token";

export const isNative = Capacitor.isNativePlatform();

export type Device = "laptop" | "phone";

function store(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* storage unavailable (private mode) — values just won't persist */
  }
}

function load(key: string): string {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

// Capture a pairing token from the URL (QR code link), then remove it from the address bar.
(function captureTokenFromUrl() {
  const url = new URL(window.location.href);
  const t = url.searchParams.get("token");
  if (t) {
    store(TOKEN_KEY, t);
    url.searchParams.delete("token");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
})();

export function getServer(): string {
  if (!isNative) return window.location.origin;
  return load(SERVER_KEY).replace(/\/+$/, "");
}

export function getToken(): string {
  return load(TOKEN_KEY);
}

export function setConnection(server: string, token: string) {
  let s = server.trim();
  if (s && !/^https?:\/\//i.test(s)) s = "http://" + s;
  if (s && !/:\d+$/.test(new URL(s).host)) s = s.replace(/\/+$/, "") + ":8340";
  store(SERVER_KEY, s.replace(/\/+$/, ""));
  store(TOKEN_KEY, token.trim());
}

export function isConfigured(): boolean {
  return !isNative || !!load(SERVER_KEY);
}

export function isLocalLaptop(): boolean {
  return !isNative && ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
}

export function device(): Device {
  if (isNative) return "phone";
  if (isLocalLaptop()) return "laptop";
  return /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent) ? "phone" : "laptop";
}

export function platformName(): string {
  if (isNative) return Capacitor.getPlatform();
  return /Android/i.test(navigator.userAgent) ? "android-browser" : /iPhone|iPad/i.test(navigator.userAgent) ? "ios-browser" : "desktop-browser";
}

/** Absolute URL for a server path, e.g. "/api/health". */
export function apiUrl(path: string): string {
  return getServer() + path;
}

/** Server URL with the token attached (for links opened outside the app, e.g. reports). */
export function authedUrl(path: string): string {
  const u = new URL(apiUrl(path));
  const t = getToken();
  if (t) u.searchParams.set("token", t);
  return u.toString();
}

export function wsUrl(): string {
  const base = new URL(getServer());
  const proto = base.protocol === "https:" ? "wss:" : "ws:";
  const t = getToken();
  return `${proto}//${base.host}/ws/voice${t ? `?token=${encodeURIComponent(t)}` : ""}`;
}

/** fetch() against the server with the pairing token attached. */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers || {});
  const t = getToken();
  if (t) headers.set("Authorization", `Bearer ${t}`);
  return fetch(apiUrl(path), { ...init, headers });
}
