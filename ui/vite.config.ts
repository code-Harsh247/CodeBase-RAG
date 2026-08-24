import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Proxy keeps the browser on one origin, so the fetch below needs no
    // absolute URL and no CORS round-trip in development.
    proxy: {
      "/api": {
        // 127.0.0.1 rather than localhost: Node may resolve localhost to ::1
        // while uvicorn binds IPv4 only, which surfaces as a proxy 500.
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
