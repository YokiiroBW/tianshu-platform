import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "onboarding-real.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 45000,
  use: { ...base.use, baseURL: "http://127.0.0.1:5192" },
  webServer: undefined,
});
