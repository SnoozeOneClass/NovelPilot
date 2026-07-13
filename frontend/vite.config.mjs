import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiTarget = process.env.NOVELPILOT_API_TARGET ?? "http://127.0.0.1:8010";

export default defineConfig({
  cacheDir: "../.tmp/vite-cache",
  plugins: [react()],
  build: {
    assetsDir: ".",
    emptyOutDir: true,
    outDir: "../.tmp/frontend-dist"
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    headers: {
      "Cache-Control": "no-store"
    },
    proxy: {
      "/api": apiTarget
    }
  }
});
