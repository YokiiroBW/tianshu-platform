import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "web-console.spec.ts",
  fullyParallel: false,
  workers: 1,
  use: { ...base.use, baseURL: "http://127.0.0.1:4814" },
  webServer: {
    command:
      '".runtime/venv/Scripts/python.exe" tests/backend/run_web_fixture.py',
    url: "http://127.0.0.1:4814",
    cwd: "../..",
    reuseExistingServer: false,
  },
});
