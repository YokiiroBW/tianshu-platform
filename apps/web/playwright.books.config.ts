import { defineConfig } from "@playwright/test";
import { fixtureEnv } from "./playwright.fixture-env";

export default defineConfig({
  testDir: "./tests",
  testMatch: "books-joint.spec.ts",
  outputDir: "../../.runtime/books-ui/test-results",
  reporter: [["list"]],
  workers: 1,
  retries: 0,
  timeout: 120000,
  use: {
    baseURL: "http://127.0.0.1:18913",
    browserName: "chromium",
    viewport: { width: 1440, height: 1000 },
    locale: "zh-CN",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
    actionTimeout: 15000,
  },
  webServer: {
    command: `"${process.env.BOOK_TEST_PYTHON ?? (process.platform === "win32" ? ".runtime/venv/Scripts/python.exe" : ".runtime/venv/bin/python")}" tests/backend/run_book_fixture.py`,
    url: "http://127.0.0.1:18913/health/live",
    cwd: "../..",
    reuseExistingServer: false,
    env: fixtureEnv(),
  },
});
