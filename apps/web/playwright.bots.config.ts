import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "bot-connections.spec.ts",
  outputDir: "./test-results/bots",
  reporter: "list",
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:4819",
    browserName: "chromium",
    locale: "zh-CN",
    viewport: { width: 1280, height: 900 },
  },
  webServer: {
    command:
      '".runtime/venv/Scripts/python.exe" tests/backend/run_bot_web_fixture.py',
    cwd: "../..",
    url: "http://127.0.0.1:4819",
    reuseExistingServer: false,
  },
});
