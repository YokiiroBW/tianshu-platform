import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "provider-manager.spec.ts",
  outputDir: "./test-results/providers",
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:5188",
    browserName: "chromium",
    locale: "zh-CN",
    viewport: { width: 1280, height: 900 },
  },
  webServer: {
    command:
      "node node_modules/vite/bin/vite.js --config apps/web/vite.config.ts --host 127.0.0.1 --port 5188",
    cwd: "../..",
    url: "http://127.0.0.1:5188/tests/provider-manager.fixture.html",
    reuseExistingServer: false,
  },
});
