import { defineConfig } from "@playwright/test";
import base from "./playwright.config";
import { fixtureEnv } from "./playwright.fixture-env";

/**
 * TS-019 只读资产页：单个项目，桌面视口起步；移动端在同一会话里改视口验证。
 *
 * 后台是这一个产品自己的合成装置（`tests/backend/run_asset_web_fixture.py`）：网页 4814、
 * 合成资产对端 4819（真实 TLS，走已发布资产只读协议）、场景开关 4818。它不是 AssetLibrary，
 * 真实联合验证仍由 TS-064 的独立驱动完成。
 *
 * 资产读取是有状态的一串记录（选定连接、进入目录、搜索），因此这里刻意不用两个项目分别写
 * 同一个合成后台：桌面、窄屏与移动的检查由用例自己切换视口完成，记录顺序保持可预期。
 */
export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: "asset-library.spec.ts",
  fullyParallel: false,
  workers: 1,
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
  ],
  use: { ...base.use, baseURL: "http://127.0.0.1:4814" },
  webServer: {
    command:
      '".runtime/venv/Scripts/python.exe" tests/backend/run_asset_web_fixture.py',
    url: "http://127.0.0.1:4814",
    cwd: "../..",
    reuseExistingServer: false,
    env: fixtureEnv(),
  },
});
