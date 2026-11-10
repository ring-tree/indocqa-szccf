import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API 代理目标与端口可经环境变量覆盖；默认指向本仓后端 8010 / 前端 5174
const apiTarget = process.env.VITE_API_TARGET ?? "http://localhost:8010";

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.VITE_PORT ?? 5174),
    proxy: {
      "/api": apiTarget,
    },
    host: true,
  },
});
