import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

/**
 * TS-025 人格目录与版本的浏览器验收：一个项目，桌面视口起步，移动端在同一会话里改视口。
 *
 * 后台是 `tests/backend/personas_fixture.py`：一个进程里三个真实控制台加一个真实 TLS 合成角色
 * 服务，每个控制台用自己的隔离 SQLite 与自己的部署设置（4820 可读、4821 未配置、4822 未授权）。
 * 「未配置」「未授权」「读取失败」「空目录」因此都是真实部署差异，不是页面里被替换掉的回答。
 *
 * 合成角色服务需要临时证书：`TS013_TLS_PYTHON` 指向带 cryptography 的工具解释器，缺它时装置
 * 会明确退出，不会静默跳过一个状态。真实 Core 的联合验收在 `test_personas_joint.py`，与此分开记录。
 */
export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "personas.spec.ts",
  fullyParallel: false,
  workers: 1,
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
  ],
  use: { ...base.use, baseURL: "http://127.0.0.1:4820" },
  webServer: {
    command:
      '".runtime/venv/Scripts/python.exe" tests/backend/personas_fixture.py',
    url: "http://127.0.0.1:4820",
    cwd: "../..",
    reuseExistingServer: false,
    env: {
      ...(process.env as Record<string, string>),
      TS013_TLS_PYTHON: process.env.TS013_TLS_PYTHON ?? "",
    },
  },
});
