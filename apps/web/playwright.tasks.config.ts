import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

/**
 * TS-018 任务中心：单个项目，桌面视口起步；移动端在同一会话里改视口验证。
 *
 * 任务中心读的是真实操作台账，一个后台进程里的记录只增不减，因此这里刻意不用两个项目分别
 * 写同一个合成后台：桌面与移动的检查由用例自己切换视口完成，记录顺序保持可预期。
 * 后台沿用网页控制台的合成装置（`tests/backend/run_web_fixture.py`：网页 4814、合成 HA 4817），
 * 它登记的正是本平台已持久化的两个来源：模型配置发布与家庭设备控制。
 *
 * 开发模式（`npm run dev` 的 Vite 输出、`main.tsx` 里的 React `StrictMode`）另有
 * `playwright.tasks-dev.config.ts`：那条路径要多起一个开发服务器，因此单独一个配置，
 * 这里的 `webServer` 保持单个对象（协调的复现配置会展开它）。
 */
export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "task-center.spec.ts",
  fullyParallel: false,
  workers: 1,
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
  ],
  use: { ...base.use, baseURL: "http://127.0.0.1:4814" },
  webServer: {
    command:
      '".runtime/venv/Scripts/python.exe" tests/backend/run_web_fixture.py',
    url: "http://127.0.0.1:4814",
    cwd: "../..",
    reuseExistingServer: false,
  },
});
