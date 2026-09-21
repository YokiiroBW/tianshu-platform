import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

/**
 * TS-025 人格目录与版本的浏览器验收：一个项目，桌面视口起步，移动端在同一会话里改视口。
 *
 * 后台是 `tests/backend/personas_fixture.py`：一个进程里导出真实 Core 的固定提交、用真实 loopback
 * TLS 经 uvicorn 服务它，再起四个真实控制台——4820 主链（只经登记连接读那台真实 Core）、4821 未
 * 配置、4822 未授权、4824 故障控制台（指向 4823 的合成 TLS 替身）。主链因此是真实
 * Core → Platform → Chromium；替身只承担额外故障与三态正文，不替代主链。
 *
 * 「未配置」「未授权」「读取失败」「空目录」都是真实部署差异，不是页面里被替换掉的回答。真实 Core
 * 的联合验收另有 `test_personas_joint.py`，与此分开记录。
 *
 * 替身与 Core 都需要临时证书：`TS013_TLS_PYTHON` 指向带 cryptography 的工具解释器，缺它时装置
 * 会明确退出，不会静默跳过一个状态。
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
