import { defineConfig } from "@playwright/test";
import { fixtureEnv } from "./playwright.fixture-env";

export default defineConfig({
  testDir: "./tests",
  testMatch: "comics-joint.spec.ts",
  outputDir: "../../.runtime/comics-ui/test-results",
  reporter: [["list"]],
  workers: 1,
  retries: 0,
  timeout: 120000,
  use: {
    baseURL: "http://127.0.0.1:18917",
    browserName: "chromium",
    viewport: { width: 1440, height: 1000 },
    locale: "zh-CN",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
    actionTimeout: 15000,
  },
  webServer: {
    command: `"${process.env.COMIC_TEST_PYTHON ?? (process.platform === "win32" ? ".runtime/venv/Scripts/python.exe" : ".runtime/venv/bin/python")}" -B tests/backend/run_comic_fixture.py`,
    url: "http://127.0.0.1:18917/health/live",
    cwd: "../..",
    reuseExistingServer: false,
    env: { ...fixtureEnv(), PYTHONDONTWRITEBYTECODE: "1", PYTHONUTF8: "1" },
  },
});
