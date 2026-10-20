import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "plugins.spec.ts",
  fullyParallel: false,
  workers: 1,
  projects: [
    { name: "plugins", use: { viewport: { width: 1440, height: 1000 } } },
  ],
  use: { ...base.use, baseURL: "http://127.0.0.1:4874" },
  webServer: {
    command: `"${process.env.TS237_PYTHON ?? ".runtime/venv/Scripts/python.exe"}" tests/backend/run_plugins_fixture.py`,
    url: "http://127.0.0.1:4874",
    cwd: "../..",
    reuseExistingServer: false,
  },
});
