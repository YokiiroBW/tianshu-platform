# 多角色运行时交接：Platform

## 目标、基线与提交

- 任务：`C:/YOKI/Codex/tianshu-peiban-bot/docs/development/role-runtime-plan-2026-09-29.md`。
- 基线：`9bed67e2de8a101f55f9a95e9a8b1288719ce8c2`。
- 固定提交：本文件所在提交（SHA 随总控交接消息提供）；未合并、未推送、未部署。

## 变更

- 增加持久角色意图和阶段账本（`<platform database_path>.roles.sqlite`）。Platform 先核验提供商，再让 Companion 暂停角色、Memory 应用精确 actor grant，最后启用 Companion；任一阶段未完成时只显示配置中/失败并拒绝新对话。重启后重新核验 Peer 版本与提供商，不凭本地旧 active 标记开放角色。相同 `client_id` 重放只返回原意图；不同内容冲突。
- 按 actor 固定提供商修订版，显式提供商不可用时失败关闭；无显式选择才继承当前全局默认。已有网页 origin/source 正式登记链继续生效，新增角色只在 active 后进入网页入口。原有静态角色可由操作者明确接管：保留 actor ID、原人格与原静态网页/BOT 连接，单独选择模型；不自动接管生产角色。
- 陪伴页面新增角色管理：列表、搜索、新建、编辑、停用、人格版本、模型、记忆读写开关；仅显示真正接通的能力。失败原因用可读文案，原始 ID/修订哈希收在诊断信息。BOT 连接和观察选角仅接受 active 角色；创建角色不自动发言或改观察策略。
- 候选跨产品合同：`docs/contracts-candidates/role-runtime/v1/`。未发布到根 `contracts/`。
- 返修：Core pause/enable 共享不可变 `application_id`、操作主体和配置签名；新增 pending 取消/停用入口，active 角色停用也走相同的精确 owner 版本 CAS 拒绝链。停用不依赖失效的 provider 或已升版档案，不重新应用 Persona，Core 与 Memory 均确认后才显示 disabled，旧应用回放不能重新启用。网页可见“取消配置并停用”与“继续停用”，已绑定但内容未变的档案沿用固定版本；retry 完成后清理旧请求 ID，继续编辑产生新意图。

## 实际验证

- `tests/backend/test_role_runtime.py`：10 通过，含新建/重放/重启/停用、预应用失败重配、原有角色接管且保留静态来源、Peer 成功后丢回执再重试无重复作用、首次静态接管取消的两种 Core 回执情况、新动态角色未创建时取消不伪造 Core 角色，以及启用角色不改变 observe-only 账号且不建立隐式回复连接。
- 受影响 Platform 回归：基础范围 77 通过、1 跳过、3 个子测试通过（Web console/dialogue/sender、provider、BOT、观察、角色）；追加配置门控及失败场景后，角色/Web dialogue/观察定向回归 18 通过；Ruff 通过；`pnpm exec tsc --noEmit` 和 Vite build 通过。
- `tests/backend/test_role_joint.py::RoleJoint::test_real_role_apply_and_model_routing`：1 通过。隔离 HTTPS Platform/Companion/Memory/Gateway、Chromium 桌面与手机、新建/编辑/停用/接管、录制模型上游请求、两条合成 NoneBot 连接及对应回复。模型请求共 5 次：新 A、B、原有 actor:a 和 A/B BOT；不同模型、人格均按 actor 录制。伪造网页 actor/conversation 返回 403；新角色和接管的原有角色停用后消息均拒绝；Core/Memory 同时为 disabled。证据：`.runtime/role-joint/results/joint-summary.json`、`roles-desktop.png`、`roles-mobile.png`、`roles-existing.png`（运行目录，不入库）。测试 Gateway 仅在隔离进程对回环地址放行录制模型；产品 Gateway 未修改。
- 12 项失败场景与未覆盖点：`docs/handoffs/ROLE-RUNTIME-FAILURE-MATRIX.md`。该矩阵中的“部分/缺口”必须保留，不能宣称全量通过。
- 返修定向：Platform 角色及 Web/Provider 45 通过；Companion 受影响 Core/model/Persona/Role 41 通过；真实 HTTPS 四项联合 4 通过。新增 lost Memory 回执后源档案编辑的真实 owner 回归、active 停用双 owner 确认、取消 Core/Memory 停用写入都成功但回执先后丢失及两次重启的测试；Chromium 覆盖失败→retry 成功→继续编辑。TypeScript、Vite build 与受影响 Python Ruff 均通过。
- 固定合成截图：`docs/handoffs/evidence/role-runtime/roles-desktop.png`、`roles-mobile.png`、`roles-existing.png`；隔离模型回执仍在 `.runtime/role-joint/results/joint-summary.json`，不含生产数据。
- 静态首接管取消返修：若 Core 首次 pause 未提交，Companion 仍仅在 `legacy_roles` 暴露该 actor，取消必须从 version 0 写入 Core 显式 disabled，再写 Memory deny。若 Core 首次 pause 已提交但回执丢失，则按 catalog 中当前 runtime 版本停用。Core 不可确认时保持 pending；尚未创建的新动态角色不伪造 Core 角色。真实 HTTPS 用 `actor:a/b` 两支核对原 Persona/binding 保留、Memory 拒绝、Core 全量重启后仍拒绝；Platform 定向回归 45 通过，联合四项 4 通过。

### 联合测试复现（隔离数据）

从本 Platform 检出目录运行；`uv` 使用 Python 3.12，浏览器脚本 `tests/backend/role_joint_browser.mjs` 与 `role_retry_browser.mjs` 由测试调用，使用安装在本检出 `node_modules` 中的 Playwright 和 Chromium。Node 可执行文件为 `C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe`。测试服务读取本检出的 `apps/web/dist`，所以先构建当前 UI。测试固定到合成回环录制端点，不产生真实模型/QQ流量。

```powershell
& 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/pnpm.cmd' exec vite build --config apps/web/vite.config.ts
$env:TS050_RUNTIME='C:/YOKI/Codex/role-runtime-worktrees/platform/.runtime/role-joint'
$env:TS050_CONTRACTS='C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
$env:TS_ROLE_GATEWAY_ROOT='C:/YOKI/Codex/tianshu-peiban-bot/projects/tianshu-model-gateway'
$env:PYTHONPATH='C:/YOKI/Codex/role-runtime-worktrees/platform;C:/YOKI/Codex/role-runtime-worktrees/platform/tests/backend;C:/YOKI/Codex/role-runtime-worktrees/companion/src;C:/YOKI/Codex/role-runtime-worktrees/memory/src;C:/YOKI/Codex/tianshu-peiban-bot/projects/tianshu-model-gateway/src;C:/YOKI/Codex/tianshu-peiban-bot/tests/integration'
uv run --python 3.12 --with pytest --with httpx --with uvicorn --with fastapi python -m pytest tests/backend/test_role_joint.py -q --tb=short --show-capture=no
$env:TS012_CONTRACT_DIR='C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
uv run --python 3.12 --with pytest python -m pytest tests/backend/test_role_runtime.py -q --tb=short
```

## 部署与回滚准备

1. 总控先发布双方验收后的根合同，固定三个产品提交；维护窗口内停止写入。备份 Platform 主 DB 及 `.roles.sqlite`、完整 provider catalog 目录与密钥、Companion 主 DB、Memory 主 DB 与 role grant sidecar，作为同一恢复集合。
2. Platform 配置 `role_runtime: {enabled: true, memory: {base_url, token_env, ca_file, timeout_seconds}}`；已有 `provider_self_service` 目录及 Gateway 管理凭据必须可用。管理主体显式加入 `role.manage`。Companion `core` 与 Memory `role_admin` 使用不同专用凭据和可信 CA；网页会话不直连 Peer。现有 web input entries 仍是归属入口。
3. 先启动 Memory/Companion，核对管理端点及旧角色，再启动 Platform；核对角色配置与回退。已有角色只在操作者明确点击应用后接管，首次选原人格时 `profile_id/profile_version=null`。停用旧角色会阻止新轮次，但不删除历史与原静态绑定声明。回滚须成组还原上述数据，不单独回退一个 sidecar。

## 未完成/风险与下一步

- 尚无真实 QQ、NAS、真实付费模型或生产迁移验证；BOT 联测为合成连接，截图来自本地浏览器。
- 失败矩阵的同会话跨角色 Memory/短上下文、全阶段停用故障注入、Gateway 每提供商容量等仍需总控验收。Persona 网页授权闭名单、生活日记不是角色管理自动开放的能力；未在本页宣称已接通。
- 总控审查三产品固定提交与合同，再决定发布顺序、部署演练和生产角色是否接管。
