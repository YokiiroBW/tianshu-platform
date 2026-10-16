import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "infrastructure.spec.ts",
  outputDir: "../../.runtime/infrastructure-browser-results",
  reporter: [
    ["list"],
    [
      "html",
      { outputFolder: ".runtime/infrastructure-browser-report", open: "never" },
    ],
  ],
  fullyParallel: false,
  workers: 1,
  use: { ...base.use, baseURL: "http://127.0.0.1:4827" },
  webServer: {
    command:
      '".runtime/venv/Scripts/python.exe" -B tests/backend/infrastructure_fixture.py',
    url: "http://127.0.0.1:4827",
    cwd: "../..",
    reuseExistingServer: false,
  },
});
