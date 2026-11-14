import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

const HUB = "http://127.0.0.1:8787";

export default defineConfig(({ mode }) => {
  // Extra host names the dev server may be reached under: PINEO_ALLOWED_HOSTS, comma-separated
  // (from the environment or a .env file). Plain IP addresses are always allowed by Vite.
  const extraHosts = (loadEnv(mode, ".", "PINEO_").PINEO_ALLOWED_HOSTS ?? "")
    .split(",")
    .map((host) => host.trim())
    .filter(Boolean);

  return {
    base: "./",
    plugins: [react()],
    server: {
      port: 4848,
      strictPort: true,
      host: true,
      // a half-finished live edit must never put Vite's red error overlay on the phone
      hmr: { overlay: false },
      // the phone loads this dev server over Tailscale; HMR uses the page's own host, so nothing is pinned to localhost
      allowedHosts: [".ts.net", "localhost", "127.0.0.1", ...extraHosts],
      proxy: {
        "/ws": { target: HUB, ws: true, changeOrigin: true },
        "/api": { target: HUB, changeOrigin: true },
        "/dashboard": { target: HUB, changeOrigin: true },
      },
    },
    preview: { port: 4848, strictPort: true, host: true },
  };
});
