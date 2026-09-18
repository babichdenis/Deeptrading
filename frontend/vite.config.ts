import { defineConfig } from "vite";

const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  server: {
    host: true,
    port: 5173,
    // Polling включён по умолчанию: на .3 repo на SMB-шаре, fs.watch не срабатывает.
    // Локально (нативная ФС) отключить: VITE_NO_POLLING=1 npm run dev
    watch: process.env.VITE_NO_POLLING === "1"
      ? undefined
      : { usePolling: true, interval: 500 },
    headers: {
      "Cache-Control": "no-store, no-cache, must-revalidate",
    },
    proxy: {
      "/api": backendUrl,
      "/ws": {
        target: backendUrl.replace(/^http/, "ws"),
        ws: true,
      },
    },
  },
});
