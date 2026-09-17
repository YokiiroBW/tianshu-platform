import { defineConfig } from "@playwright/test";
import base from "./playwright.config";
import { fixtureEnv } from "./playwright.fixture-env";

const VITE = "http://127.0.0.1:5173";

/**
 * TS-019 只读资产页的 StrictMode 路径：网页来自 `npm run dev` 的 Vite 输出（5173），
 * `main.tsx` 在那里用 React StrictMode，因此首挂载的 effect 会复演一次。
 *
 * 开发服务器只有页面，没有同源后台，所以用 `vite.assets-dev.config.ts` 把 `/api/web/*` 转发到同一
 * 装置里的第二个后台实例（4815，被配置的 origin 就是 5173）：主机与 Origin 复核照旧生效，不是被
 * 绕过。资产对端仍是真实 TLS 的合成对端（4819），场景开关在 4818。
 */
export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "asset-library-dev.spec.ts",
  // 自己的产物目录（在被忽略的 `apps/web/test-results/` 之下）：与构建产物套件分开，跑完这条
  // 不会把上一套已经收好的截图清掉。
  outputDir: "./test-results/dev-strictmode",
  fullyParallel: false,
  workers: 1,
  projects: [{ name: "dev", use: { viewport: { width: 1440, height: 1000 } } }],
  use: { ...base.use, baseURL: VITE },
  webServer: [
    {
      command:
        '".runtime/venv/Scripts/python.exe" tests/backend/run_asset_web_fixture.py',
      url: "http://127.0.0.1:4815",
      cwd: "../..",
      reuseExistingServer: false,
      env: fixtureEnv(),
    },
    {
      command: `"${process.execPath}" node_modules/vite/bin/vite.js --config apps/web/vite.assets-dev.config.ts --strictPort --host 127.0.0.1`,
      url: VITE,
      cwd: "../..",
      reuseExistingServer: false,
    },
  ],
});
