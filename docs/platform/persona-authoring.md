# 人格档案创建与应用

网页「陪伴 → 人格与世界 → 创建与编辑」可创建可复用档案、复制、查看、编辑已有角色、
保存草稿和明确保存并应用。档案的名称/简介只用于目录，四个文本字段进入 Companion
Personas 内容。网页不要求 JSON、角色 ID 或部署文件；应用时只从登记的目标列表选择。
复制已有档案或角色时，服务端按被复制版本读取完整内容并保留未展示的扩展字段。
保存草稿不改变运行人格；应用只影响之后新准备的轮次。历史与差异区继续使用原
`persona-management/v1` 只读合同。

## 配置增量

沿用 `persona_connections` 的专用 HTTPS 凭据与已发布只读合同。`web_personas` 可追加：

```json
{
  "authoring_enabled": true,
  "apply_subjects": ["actor:companion"]
}
```

缺 `authoring_enabled` 时默认关闭；`apply_subjects` 必须是旧 `allowed_subjects`
的子集。这个列表控制网页可编辑的既有角色和可应用目标，不因创建新档案扩大。
原 `allowed_subjects` 及已发布只读合同配置保留。Web operator 除 `persona.read`
外，按职责增加 `persona.create`、`persona.edit`、`persona.apply`；保存并应用需要
edit 与 apply。普通网页登录本身及 `config.publish` 不授予这些权限。Companion
仍须启用 `personas` 并登记独立 `admin_token_env`；Platform 的
`persona_connections` 令牌环境变量必须对应它，令牌只留后端。服务地址只准 HTTPS，
并需显式 CA 文件。平台浏览器会话、Origin、CSRF、操作权限和目标范围在每次写入
前后复核。

网页固定请求为同源 `/api/web/personas/{profiles,view,create,save,apply}`。
Platform 把按钮点击翻译为一个 Companion 管理操作；`apply` 不会从浏览器连续
发送 `draft`、`approve`、`publish` 三个请求。Companion 在同一事务中完成它们并
保留 request_id 幂等账本。失去回答时，用相同 `client_id` 重试相同正文可读回
首次结果。过期版本是 409，正文与表单仍留在页面供用户检查。

此增量不新增平台或 Companion 数据库表、列、索引和 schema 版本。上线前对现有
Companion v9 SQLite 数据库执行停写、SQLite backup（包含 WAL）并保存恢复点；
Platform 配置变更需审查后重启，当前任务不对 NAS 或生产数据库操作。若部署尚未
执行原 v9 迁移，先按 Companion `docs/personas.md` 的原迁移备份流程处理，不在网页
首次 GET 时升级。

隔离验收：`tests/backend/run_persona_author_fixture.py` 在一次性目录启动真实
Companion Core 与两个 Platform 控制台，均走本机 HTTPS；浏览器用合成账号和
虚构人格检验创建、编辑、应用、读回、版本冲突、越界角色、登出和只读权限。
`apps/web/tests/persona-author.spec.ts` 保存桌面和手机截图至
`apps/web/test-results/persona-author-{desktop,mobile}.png`。模型调用为既有合成
替身；没有真实 QQ、付费模型、NAS 或生产身份。
