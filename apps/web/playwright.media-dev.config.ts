import { defineConfig } from "@playwright/test";
import base from "./playwright.config";
export default defineConfig({
  ...base,
  outputDir: "../../.runtime/media-ui/dev-test-results",
  reporter: [
    ["list"],
    ["html", { outputFolder: ".runtime/media-ui/dev-report", open: "never" }],
  ],
  testIgnore: [],
  testMatch: "media-dev.spec.ts",
  workers: 1,
  projects: [
    { name: "development", use: { viewport: { width: 390, height: 844 } } },
  ],
  use: { ...base.use, baseURL: "http://127.0.0.1:5195" },
  webServer: {
    command:
      "node node_modules/vite/bin/vite.js --config apps/web/vite.config.ts --host 127.0.0.1 --port 5195 --strictPort",
    url: "http://127.0.0.1:5195",
    cwd: "../..",
    reuseExistingServer: false,
  },
});
