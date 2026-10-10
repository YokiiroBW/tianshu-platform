import { defineConfig } from "@playwright/test";

/** Uses a separately started actual platform fixture; does not intercept media requests. */
export default defineConfig({
  testDir: "./tests",
  testMatch: "media-joint.spec.ts",
  outputDir: "../../.runtime/media-ui/joint-test-results",
  reporter: [["list"]],
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 90_000,
  use: {
    baseURL: process.env.MEDIA_JOINT_URL ?? "http://127.0.0.1:18898",
    browserName: "chromium",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    reducedMotion: "reduce",
    actionTimeout: 10_000,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
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
