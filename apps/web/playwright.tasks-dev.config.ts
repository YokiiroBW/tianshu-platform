import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

/**
 * TS-018 任务中心在**开发模式**（`npm run dev`）下的回归：`main.tsx` 用 React `StrictMode`，
 * 开发模式的 effect 会按 setup → cleanup → setup 复演一遍，生产构建不经过这一遍。
 * 因此这条路径必须真的跑在 Vite 开发服务器上，而不是 `apps/web/dist`。
 *
 * 两个服务器：
 * 1. 合成后台（`tests/backend/run_web_fixture.py`：网页 4814、合成 HA 4817）——用例仍然用真实
 *    后台登录、真实操作一次设备、并取回真实的记录回答；
 * 2. 开发服务器（127.0.0.1:5173）——真实开发模块与真实 StrictMode。它只提供页面模块，没有同源
 *    后台，所以用例把会话与记录两个回答换成上面取回的那份真实内容。
 * 开发服务器显式绑 `127.0.0.1`：Vite 默认只监听 localhost（本机解析为 ::1），而这里按
 * `127.0.0.1` 探测就绪。用当前这个 Node 解释器，绝不复用未知的开发服务器进程。
 */
export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "task-center-dev.spec.ts",
  fullyParallel: false,
  workers: 1,
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
  ],
  use: { ...base.use, baseURL: "http://127.0.0.1:4814" },
  webServer: [
    {
      command:
        '".runtime/venv/Scripts/python.exe" tests/backend/run_web_fixture.py',
      url: "http://127.0.0.1:4814",
      cwd: "../..",
      reuseExistingServer: false,
    },
    {
      command: `"${process.execPath}" node_modules/vite/bin/vite.js --config apps/web/vite.config.ts --strictPort --host 127.0.0.1`,
      url: "http://127.0.0.1:5173",
      cwd: "../..",
      reuseExistingServer: false,
    },
  ],
});
