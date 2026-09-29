import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "persona-author.spec.ts",
  fullyParallel: false,
  workers: 1,
  projects: [
    { name: "authoring", use: { viewport: { width: 1440, height: 1000 } } },
  ],
  use: {
    ...base.use,
    baseURL: "https://127.0.0.1:4840",
    ignoreHTTPSErrors: true,
  },
  webServer: {
    command:
      '".runtime/venv/Scripts/python.exe" tests/backend/run_persona_author_fixture.py',
    url: "https://127.0.0.1:4840",
    cwd: "../..",
    reuseExistingServer: false,
    ignoreHTTPSErrors: true,
    env: { ...(process.env as Record<string, string>) },
  },
});
