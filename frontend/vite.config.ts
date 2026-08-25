import { defineConfig } from "vite";

const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": backendUrl,
      "/ws": {
        target: backendUrl.replace(/^http/, "ws"),
        ws: true,
      },
    },
  },
});
