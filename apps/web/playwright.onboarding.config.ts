import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

export default defineConfig({
  ...base,
  testMatch: [
    "onboarding.spec.ts",
    "shell.spec.ts",
    "room.spec.ts",
    "status-rail.spec.ts",
    "access-settings.spec.ts",
  ],
  workers: 2,
  use: { ...base.use, baseURL: "http://127.0.0.1:5190" },
  webServer: {
    command:
      "node node_modules/vite/bin/vite.js preview --config apps/web/vite.config.ts --host 127.0.0.1 --port 5190 --strictPort",
    url: "http://127.0.0.1:5190",
    cwd: "../..",
    reuseExistingServer: false,
  },
});
