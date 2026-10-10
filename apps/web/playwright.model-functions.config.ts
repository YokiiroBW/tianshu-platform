import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "model-functions.spec.ts",
  outputDir: "./test-results/model-functions",
  reporter: "list",
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:4817",
    browserName: "chromium",
    locale: "zh-CN",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1280, height: 900 } } },
    {
      name: "mobile",
      use: { viewport: { width: 390, height: 844 }, isMobile: true },
    },
  ],
  webServer: {
    command: `"${process.env.TIANSHU_TEST_PYTHON ?? "python"}" apps/web/tests/model-functions-fixture.py`,
    cwd: "../..",
    url: "http://127.0.0.1:4817/api/web/session",
    reuseExistingServer: false,
  },
});
