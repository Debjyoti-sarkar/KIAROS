import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "com.kiaros.app",
  appName: "KIAROS",
  webDir: "dist",
  server: {
    // The app talks to your laptop over plain HTTP on the LAN / Tailscale,
    // so the page itself must be http:// too (no mixed-content blocking).
    androidScheme: "http",
    cleartext: true,
  },
  android: {
    backgroundColor: "#050508",
  },
};

export default config;
