import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

const output =
  process.env.TS116_BROWSER_OUTPUT ?? "./test-results/relationships";
if (process.env.TS116_BROWSER_EXTERNAL !== "1") {
  throw new Error(
    "Run tests/backend/test_relationship_browser.py to own the local three-product fixture.",
  );
}

export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "relationships.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  outputDir: `${output}/results`,
  reporter: [
    ["list"],
    ["html", { outputFolder: `${output}/report`, open: "never" }],
    ["junit", { outputFile: `${output}/junit.xml` }],
  ],
  use: {
    ...base.use,
    baseURL: "http://127.0.0.1:4844",
  },
  webServer: undefined,
});
