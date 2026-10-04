import { readFileSync } from "node:fs";
import { defineConfig } from "@playwright/test";

// The backend writer starts an isolated fixture with real authenticated TLS owners.
// Its synthetic login stays in the ignored runtime file, never in test source.
const path = process.env.TS_C4_JOINT_FIXTURE;
const fixture = path ? JSON.parse(readFileSync(path, "utf8")) : null;
export default defineConfig({
  testDir: "./tests",
  testMatch: "runtime-joint.spec.ts",
  outputDir: "../../.runtime/ui-browser-joint",
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: fixture?.url,
    ignoreHTTPSErrors: true,
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
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
