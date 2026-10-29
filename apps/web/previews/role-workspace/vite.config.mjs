import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { mockApi } from "./mock-api.mjs";

export default defineConfig({
  root: fileURLToPath(new URL("../../", import.meta.url)),
  plugins: [react(), mockApi()],
  server: { host: "127.0.0.1", port: 5175, strictPort: true, hmr: false },
});
