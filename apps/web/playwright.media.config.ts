import { defineConfig } from "@playwright/test";
import base from "./playwright.config";
/** UI lifecycle fixtures only. The real media owner HTTP joint suite is separate. */
export default defineConfig({
  ...base,
  outputDir: "../../.runtime/media-ui/test-results",
  reporter: [
    ["list"],
    ["html", { outputFolder: ".runtime/media-ui/report", open: "never" }],
  ],
  testIgnore: [],
  testMatch: "media.spec.ts",
  workers: 2,
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "tablet", use: { viewport: { width: 1024, height: 768 } } },
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
