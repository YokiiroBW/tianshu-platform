import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "provider-real.spec.ts",
  outputDir: "./test-results/providers-real",
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:4814",
    browserName: "chromium",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    viewport: { width: 1280, height: 900 },
  },
});
