import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "qq-admin.spec.ts",
  outputDir: "../../.runtime/qq-browser-results",
  reporter: [["list"]],
  use: {
    baseURL: "https://127.0.0.1:4850",
    browserName: "chromium",
    ignoreHTTPSErrors: true,
    locale: "zh-CN",
    reducedMotion: "reduce",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
    {
      name: "mobile",
      use: {
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
      },
    },
  ],
});
