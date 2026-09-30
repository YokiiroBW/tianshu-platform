# MEMORY-ROLE-20260930 · Platform 交接

基线：已部署 Platform `17af1b43b432f4bd6e250192fcfad35f8b04a8c1`。候选实现提交：`c369861e072bbdce6d88bbadc7bd0b48483f52ff`；禁用原因修复和真实浏览器增量提交：`5ff7b11105f109940d4cf014d2a280202273910f`；本交接文件为后续文档提交。仅在本产品独立 worktree 开发，未写根仓库、`projects/`、NAS 或真实账号。

## 变更

- 记忆页顶部增加角色选择，沿用现有 panel/button/select 风格。默认旧静态角色，刷新与跨记忆子页在同一登录账号的标签页内保留；退出或失效清除。切换清空概览、人物列表、详情、记录、游标和错误并中止旧请求。慢旧响应受 AbortController 与 generation 双重保护。页面可见时每 15 秒及重新可见时复核当前角色读取；任何失败隐藏旧结果。
- `web_memory` 只从当前 operator 的 `role_runtime` 目录给出友好角色名、版本和不可读原因。动态角色必须 active、enabled 且具 `memory.read`；被接管停用的静态默认角色也不允许走旧入口。前端只传 `role_id` 与 `role_version`，服务端从原 `entry_id` 的账号、人物、会话、audience 与 route 派生独立的临时来源；返回前重新核验角色、会话与来源。每个并发请求有独立临时 entry，断连后延迟任务完成时清理。
- 同源请求候选见 [memory-role-browser/v1](../contracts-candidates/memory-role-browser/v1/README.md)。旧请求完全省略角色字段时保持静态单角色语义；显式 null/未知角色不回退。上游 400 `invalid_input` 明确传回，跨角色游标不会被读作同角色分页。

## 配置差异（由总控审查部署）

Platform `web_memory` 在旧字段基础上增设 `"runtime_roles": true`。Memory 同时需 `callers.platform.allow_runtime_roles: true`、`browser_readers.platform.allow_runtime_roles: true`、既有持久 `role_grants_database_path`；保留 reader 的原 `account`、`actor_id`、单一精确 `scopes` 模板，以及 caller 的静态 `allowed_actors` 和 `operations: ["browse"]`。各门控缺省关闭，不用通配 actor。当前 `self_private` 模板不扩大到群专属 `group_only` 范围。

## 已完成验证

- 真实 HTTPS Platform→Memory 进程联验：两个角色同 person/conversation，概览和不同语义组分页、双方人物投影、伪造角色、跨角色游标、grant 撤销、静态默认角色接管后停用、Memory 进程重启恢复。命令：`TS012_CONTRACT_DIR=<根 contracts/text-dialogue/v1> TS013_TLS_PYTHON=<证书工具解释器> TS_CONNECT_M_PATH=<本任务 Memory worktree> python -m unittest discover -s tests/backend -p test_memory_joint.py -v`。
- 同一联验加 `TS_MEMORY_ROLE_BROWSER_NODE=<Node 路径>` 后，实际 Chromium 桌面 1440×1000 与手机 390×844 经网页登录真实 Platform/Memory；A→B 切换、概览、人物投影、本人记录和刷新保留均通过，未拦截请求。截图已目视检查：`.runtime/memory-role-browser/memory-role-live-desktop.png`、`memory-role-live-mobile.png`（忽略的本地测试产物）。

实际联验命令（PowerShell，在 Platform worktree）：

```powershell
$env:TS012_CONTRACT_DIR='C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
$env:TS013_TLS_PYTHON='C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:TS_CONNECT_M_PATH='C:/YOKI/Codex/memory-role-worktrees/memory'
$env:TS_MEMORY_ROLE_BROWSER_NODE='C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
& 'C:/YOKI/Codex/persona-authoring-worktrees/platform/.runtime/venv/Scripts/python.exe' -m unittest discover -s tests/backend -p test_memory_joint.py -v
```
- Platform HTTPS synthetic peer 最窄测试 5 项：角色目录/operator 过滤、临时来源、禁用/版本变化、同角色并发读加断连清理。命令：同环境下 `python -m unittest discover -s tests/backend -p test_web_memory.py -v`。
- `tsc -p apps/web/tsconfig.json --noEmit`、Vite 生产 build；Chromium 桌面与手机 `memory-role.spec.ts` 与原记忆页用例共 4 项通过，含刷新保留、人物与本人页、慢响应、角色撤销后旧内容清除、退出清选择。命令：`node node_modules/@playwright/test/cli.js test --config apps/web/playwright.config.ts --grep 'memory role selection|memory separates overview' --workers=1`。
- 截图（忽略的测试产物）：`apps/web/test-results/memory-role-memory-role-se-9b4b3-cannot-replace-the-new-role-desktop/memory-role-active-desktop.png` 与对应 `...-mobile/memory-role-active-mobile.png`；同目录还有角色移出后的界面截图。
- `ruff check services/platform/web_memory.py tests/backend/test_memory_joint.py tests/backend/test_web_memory.py` 与 `node --check tests/backend/memory_role_joint_browser.mjs` 通过。补齐 worktree 忽略目录 `.runtime/workspace-context.json` 后全量 `python -m unittest discover -s tests/backend -q` 共运行 915 项：14 fail、39 error、62 skip。47 个失败/错误在未改的媒体 regex worker 用例，报 `regex_worker_failed`；其余是现有 `runtime.lock` 与开发依赖闭包不一致，以及未配置 `TS025_CANDIDATE_DIR`、`TS100_DIAGNOSTICS_CONTRACT_DIR` 和独立联验支持模块。记忆相关的最窄用例及真实浏览器联验均通过；全量结果不记作通过。

## 待总控处理与边界

请串行合入 Platform/Memory 候选，核对 NAS 当前配置后再更新和实机验收；本地真实 HTTPS 联验不代表 NAS 已部署。现有角色目录在启动恢复时遵守既有 pending/verify 语义，pending 不可读取，本任务未改角色恢复流程。Memory 最终 grant 可独立撤销，网页在下一次读取/可见性复核时隐藏旧结果；没有服务端推送。
