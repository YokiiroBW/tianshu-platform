import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "bot-adapters-live.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 90000,
  use: { ...base.use, ignoreHTTPSErrors: true },
  webServer: undefined,
});
