# ADAPTER-WEB-JOINT 浏览器与宿主联合验收

## 目标与基线

在集成候选上，从真实构建的网页走管理员登录、Cookie/CSRF、二次解锁、NoneBot 检测、保存为停用、启用和停用；后台使用实际 Platform/Core TLS 服务及正式加载的 NoneBot 插件 HTTP RPC。Platform 起始基线 `ebac8e5ce586b3eb05a2142836f5aac65a690e53`，Companion 起始基线 `e85987147b1109c8a41b3782e6fc3ccc33e0e249`。本任务只新增浏览器用例、Playwright 配置、隔离夹具和本交接，没有修改产品代码、锁文件或原 P 联合测试。

## 实际边界与证据

`scripts/dev/bot_adapters_live_fixture.py` 复用 `tests/backend/test_bot_adapters_host_joint.py` 的配置、临时自签 CA 和构建器，断言 Platform、Core 与 NoneBot 插件均从当前集成候选导入。每个浏览器用例启动全新临时数据目录和三个 loopback 服务：Platform HTTPS、Core HTTPS、NoneBot 插件 HTTP。宿主通过正式 NoneBot 插件注册和合成 OneBot SDK 返回在线 QQ 账号 `42`；合成发送函数若被调用即失败。角色、联系人、登录身份、令牌、证书与数据库均为临时合成数据。没有真实 QQ、模型、NAS、真实聊天或生产数据。

浏览器用例没有 `page.route` 或适配器 API 替身。它检查每次 `probe/create/enable/disable` 的实际 POST 均带会话 Cookie 和 CSRF，断言探测协议及宿主返回账号；检查创建回执的停用状态、私聊 `7`、允许作者 `7` 与角色 `actor:a`；检查启用和停用回执的状态与递增修订号，并等待页面读回状态和确认文案。`ready` 启用通过 Platform 对插件和 Core 的实际调用完成。桌面 `1440×1000` 和移动 `390×844` 各跑一次，分别保存 probe、创建后停用、启用、最终停用四张截图。

截图位于忽略提交的 `apps/web/test-results/bot-adapters-live-real-bro-6f3f6-probe-create-enable-disable-{desktop,mobile}/`，文件名为 `probe.png`、`created-disabled.png`、`enabled.png`、`disabled.png`。已人工查看两端 `enabled.png`：连接条目、账号/私聊/作者和启用状态可见，移动版没有横向溢出。全页移动截图中页面的固定跳转链接位于标题附近；这是截图拼接时的固定元素呈现，未观察到接入表单或连接条目被遮挡。

## 验证与复现

从本仓库根目录执行：

```powershell
& 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/pnpm.cmd' install --no-lockfile --ignore-scripts
& 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/pnpm.cmd' exec tsc -p apps/web/tsconfig.json --noEmit
& 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/pnpm.cmd' exec vite build --config apps/web/vite.config.ts
$env:TS_BOT_ADAPTER_LIVE_PYTHON='C:/YOKI/Codex/tianshu-peiban-bot/worktrees/ADAPTER-H/tianshu-companion/.runtime/adapter-venv/Scripts/python.exe'
& 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/pnpm.cmd' exec playwright test --config apps/web/playwright.bot-adapters-live.config.ts
```

结果：前端类型检查和构建通过；最终浏览器联合验收桌面、移动端 `2 passed (8.8s)`。Python 夹具 `py_compile` 和新 TypeScript 文件的 Prettier 检查通过。首轮启动时补齐契约目录环境变量，并将本地浏览器网页模式设为 `service_https`；最终一轮双端通过。浏览器仅测试 NoneBot 宿主，AstrBot 的实际宿主联合链路由 `ADAPTER-JOINT.md` 的 P 双宿主测试覆盖。

## 未覆盖与交接

没有验证真实 QQ 登录或发送、模型生成、真实局域网/NAS 部署、长时间运行及生产凭据。桌面和移动端都在同一台机器上的 loopback 上验收。下一步由协调者审查本任务四文件和截图，按集成流程合入；真实账号与设备的接入需独立安排。
