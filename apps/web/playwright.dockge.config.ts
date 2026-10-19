import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

const uiOnly = process.env.TS207_UI_ONLY === "1";
const python = process.env.TS207_PYTHON ?? ".runtime/venv/Scripts/python.exe";
const fixture = process.env.TS207_FIXTURE ?? "tests/backend/dockge_fixture.py";

export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "dockge-control.spec.ts",
  outputDir: "../../.runtime/dockge-browser-results",
  reporter: [
    ["list"],
    ["html", { outputFolder: ".runtime/dockge-browser-report", open: "never" }],
  ],
  fullyParallel: false,
  workers: 1,
  use: { ...base.use, baseURL: "http://127.0.0.1:4837" },
  webServer: {
    command: uiOnly
      ? "node node_modules/vite/bin/vite.js preview --config apps/web/vite.config.ts --host 127.0.0.1 --port 4837 --strictPort"
      : `"${python}" -B "${fixture}" --web-dist "${process.cwd()}/apps/web/dist" --port 4837 --register-node`,
    url: "http://127.0.0.1:4837",
    cwd: "../..",
    reuseExistingServer: false,
    env: {
      PYTHONPATH: process.env.TS207_BACKEND_ROOT ?? process.cwd(),
      PYTHONDONTWRITEBYTECODE: "1",
    },
  },
});
