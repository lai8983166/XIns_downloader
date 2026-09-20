import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8001";

export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/preview": apiTarget,
      "/download": apiTarget,
      "/media-proxy": apiTarget,
      "/profile": apiTarget,
      "/threads": apiTarget
    }
  }
});
