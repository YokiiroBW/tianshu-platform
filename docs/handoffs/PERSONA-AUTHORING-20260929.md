# 人格档案创建与编辑：Platform 交接

2026-09-29。任务分支 `codex/persona-authoring-20260929`；基线
`e7b9ce063eb7731bba673da303d3835dfc84c96d`；功能提交
`15af0e9957a403005d1cc96b02be6a35e8d332c4`。本交接文件随后的提交号由交付消息给出。
未推送、未合并、未部署。

## 目标与变更

- 网页「陪伴 → 人格与世界 → 创建与编辑」支持新建、查看、编辑、复制、保存草稿、
  明确应用到已授权角色，也支持直接编辑和应用已有角色。显示名称/简介、状态、更新时间、
  四字段提示和字数限制。打开编辑器后旧历史/差异区折叠但可随时展开。
- 前端离开未保存输入时提示；并发版本冲突保留输入。请求使用稳定 `client_id` 供同一正文重试。
  正文不含浏览器可见的管理令牌、服务地址或数据库路径。
- `WebPersonaAuthor` 在现有同源 Cookie、Origin、CSRF 与后端专用 HTTPS 连接上新增五条固定路由。
  仅把四个表单字段送给 Companion；只列出配置中的可应用角色。每次请求在拿连接名额前、
  拿到名额后和回答前复核会话、读取权限、写动作及目标范围；应用还要求编辑权限。
  旧四条只读路由继续可用。
- 新增 `persona.create`、`persona.edit`、`persona.apply` 操作权限；
  `web_personas.authoring_enabled` 默认关闭，`apply_subjects` 必须是旧 `allowed_subjects` 子集。
  细节见 `docs/platform/persona-authoring.md`。

Companion 功能提交 `9a7d924300033329990565903a3164889b8595ec`；根合同候选
`contracts/persona-authoring/candidate-v1` 提交
`540c5c906ed8a70e7da268824a3e261fdff6a945`。已发布
`persona-management/v1` 只读合同未改。

## 实际验证

- `test_persona_author.py`：1 通过，验证排队等待期间撤销编辑权限时应用被拒绝。
- `apps/web` TypeScript 与 Vite build 通过；本次改动 Python Ruff check/format check 通过。
- 真实隔离 HTTPS Playwright：2 通过。一次性目录启动真实 Companion Core、两个 Platform
  控制台与合成账号，覆盖创建→读回→改草稿→应用→角色读回、已有角色直接应用、复制、
  过期版本 409、过长正文 400、越界角色 403、登出 401、只读操作员 403，以及浏览器冲突后
  保留输入。Core 旧新 pin、重启/再导入和操作幂等由 Companion 同名交接中的测试覆盖。
- 旧只读后端套件曾运行 45 项，其中 44 项通过，1 项在 Windows 临时 SQLite 文件清理时
  `PermissionError`，不是读接口断言失败；本任务未宣称该套件全绿。
- 候选合同 JSON Schema 与实例校验通过，manifest 中四个文件的 SHA256 逐项复核通过。

截图是实际 HTTPS 浏览器测试产物，已提交到仓库：

- 草稿桌面：`C:/YOKI/Codex/persona-authoring-worktrees/platform/docs/handoffs/persona-authoring-2026-09-29/desktop.png`
- 应用后手机：`C:/YOKI/Codex/persona-authoring-worktrees/platform/docs/handoffs/persona-authoring-2026-09-29/mobile.png`
- 并发冲突与输入保留：`C:/YOKI/Codex/persona-authoring-worktrees/platform/docs/handoffs/persona-authoring-2026-09-29/conflict.png`

## 配置、迁移和后续

按 `docs/platform/persona-authoring.md`，在现有 `web_personas` 中审查后增加
`authoring_enabled: true` 和具体 `apply_subjects`，给 Web operator 增加所需三项动作；
沿用原 `persona_connections` 的专用 HTTPS 凭据、显式 CA 和只读名单。
Companion 需启用 Personas 并保留独立 `admin_token_env`；Platform 令牌环境变量与其对应。
没有新数据库迁移；集成前停写并对 Companion v9 SQLite 做包含 WAL 的 SQLite backup。
本任务没有更改 NAS 配置或生产数据，协调者需审查合同、合并次序和真实 NAS 联调。
