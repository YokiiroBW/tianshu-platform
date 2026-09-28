# ADAPTER-U · 网页机器人适配器向导交接

## 目标与基线

- 基线：`88fb778974f7050f3b4aa3c84a47b847d997e349`；实现提交：`2924ec358b3c99959bd8ab21fa4454062850aa0c`；审查返修：`d916ee4e475d8b8348c859a69b6f37b75d211b4f`；显式恢复提交：`4a55d0d548c17904283f3eb548b1f61ae657877f`，分支 `codex/bot-adapter-ui-20260928`。
- 按协调合同 `docs/development/bot-adapter-self-service-2026-09-28.md` 的 `/api/web/bot-adapters/{view,probe,create,enable,disable}` 字段实现；管理员解锁复用 `/api/web/bots/unlock`。本任务只改机器人设置页及浏览器测试。

## 变更

- 设置第 3 页以“添加适配器”为主入口。选 AstrBot/NoneBot，输入插件监听地址和一次性连接密钥，真实 `probe` 成功后仅显示返回的 QQ 账号；选择角色、群/私聊 ID 与明确作者白名单，`create` 保存为停用，再通过独立按钮 `enable`。普通流程不要求输入内部协议、槽位或服务地址；高级项只提供可信 CA。局域网 HTTP 需显式勾选，HTTPS 证书验证由后台负责。
- 写入后重新读取 `view` 核对连接 ID、修订与启停状态；启用只有 `ready` 才显示已确认。结果不明只提示刷新核对，不自动重试。草稿到期、切页与取消会清理表单；插件密钥与 CA 提交后立即从表单清除，不回显。
- 原有预登记槽位连接管理收在“旧连接管理”折叠区，原创建、启停与轮换功能保留；新适配器不在前端伪装为旧连接。新后台未启用时显示明确提示，不出现可提交的新表单。
- 新增同源 API 类型与错误提示；浏览器测试覆盖完整创建/启停、错误密钥、不可达地址、版本不兼容、空 SDK 账号、局域网 HTTP 确认、过期草稿、取消、切页、未知写入与旧管理回归。
- 审查返修：创建、启用、停用的成功提示同时要求操作回执与重新读取状态为明确的 `disabled`/`ready`；不一致的连接在本页标为“结果待核对”并禁止继续写入，直到显式刷新。管理解锁过期会隐藏向导、清除地址/密钥/草稿与范围，并重新显示解锁入口，未知写入仍提示先核对。私聊联系人 ID 自动填入允许作者，可编辑；页面不再长期显示开发验收措辞。
- 联合审查补充：服务端 `state=unknown` 的已有连接才显示“核对并恢复”；该按钮只提交 `{id,expected_revision,client_id}`，不提交新的启停目标，也不由刷新自动触发。页面说明恢复原启用操作可能恢复正常消息处理；结果需回执与 `view` 对同一 ID、原修订、启停及 `ready`/`disabled` 状态一致才确认。409 `result_unknown` 先提示刷新，只有重新读到服务端 `unknown` 才开放恢复；409 `version_conflict` 与未决结果要求显式刷新后再操作。`draft` 不开放恢复。

## 实际验证

- `pnpm exec tsc -p apps/web/tsconfig.json --noEmit`：通过。
- `pnpm exec vite build --config apps/web/vite.config.ts`：通过；既有 RoomPage 大包提示仍在。
- `pnpm exec playwright test --config apps/web/playwright.config.ts bot-adapters.spec.ts --workers=1`：显式恢复补充后桌面/手机 38 项通过；同源 API 使用明确合成响应。`settings-subpages.spec.ts` 桌面/手机 6 项通过。
- `TS012_CONTRACT_DIR=.../contracts/text-dialogue/v1 pnpm exec playwright test --config apps/web/playwright.bots.config.ts`：原旧连接管理 1 项通过，使用隔离的真实本地 Platform/Cookie/CSRF 夹具；不是新适配器后台联验。
- `git diff --cached --check`：通过。目视检查桌面/手机向导与保存后状态，手机无横向溢出。截图是忽略的本地运行产物：`apps/web/test-results/bot-adapters-adapter-wizar-8e634-icitly-enables-and-disables-{desktop,mobile}/bot-adapter-{wizard,saved}.png`。
- 显式恢复的桌面/手机截图亦已目视检查：`apps/web/test-results/bot-adapters-unknown-backe-f59fe-firms-the-original-revision-{desktop,mobile}/bot-adapter-recovery.png`；手机按钮完整显示，无横向溢出。
- 本机无 `npm` 命令；用已配置的 pnpm 11.19.0 从根 `package.json` 固定版本安装依赖，没有改锁文件。

## 未完成与集成注意

- ADAPTER-P 后台与 ADAPTER-H 插件尚未合到本检出；本向导的新 API 通过浏览器合成响应验证。总控需按上述固定草案核对 P/U 请求与回执，跑真实 Platform/Core/插件联测和重启恢复；本交接不声称真实 SDK、消息收发、NAS 或生产部署完成。
- `probe` 必须只在真实插件响应且账号来自实际 SDK 时返回草稿；CA/HTTPS、私网 HTTP、凭据持久化、权限和启停一致性均为后台权威边界。新 API 缺失时页面明确不可用。
- 不操作真实机器人账号、联系人或群，未发送测试消息。
