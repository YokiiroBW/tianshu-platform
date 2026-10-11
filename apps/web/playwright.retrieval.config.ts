import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  testMatch: "retrieval.spec.ts",
  workers: 1,
  reporter: "list",
  outputDir: "./test-results/retrieval",
  use: {
    baseURL: "http://127.0.0.1:4828",
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
    command: `"${process.env.TIANSHU_TEST_PYTHON ?? "python"}" apps/web/tests/retrieval-fixture.py`,
    cwd: "../..",
    url: "http://127.0.0.1:4828/api/web/session",
    reuseExistingServer: false,
  },
});
