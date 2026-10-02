import { defineConfig } from "@playwright/test";

// The Python joint fixture owns the actual Companion TLS and Platform HTTP
// servers and the temporary synthetic databases. No route mocks are installed.
export default defineConfig({
  testDir: "./tests",
  testMatch: "life-joint.spec.ts",
  outputDir: "./test-results/life-joint",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.TS_LIFE_JOINT_URL,
    browserName: "chromium",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 } } },
  ],
});
