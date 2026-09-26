# ONBOARDING-ACCOUNT · 单管理员首次使用后端

基线：平台 `c1c7547`。隔离分支 `codex/onboarding-account-20260926`。本交付只改平台后端、后端测试和文档；前端、根部署工具、NAS 均未修改。

实现：显式 `web_account` create/claim 配置、一次性安装凭据、真实单管理员 SQLite/封条持久化、统一会话状态、已配置管理员认证接管。创建/接管均复用同源 Cookie/CSRF/Origin、限流与服务器 operator 权限。新账户提交后登录和既有 models/home/access 解锁均读新 hash；其他实例及重启不回退旧配置密码。移除 opt-in 但留有持久状态时保守拒绝；没有账号文件且未 opt-in 的原部署行为保持。

完整配置、API、状态/错误闭集、备份/恢复约束见 [web-account.md](../platform/web-account.md)。首次 GET/preflight 不写账号文件；正常待初始化状态不阻塞读取初始化页面。文件为 `<database_path>.web-account.sqlite` 与同名追加 `.sealed` 的封条，必须一起持久化、备份恢复。

实际验证（隔离合成数据、本机 Python 3.12）：

- `test_web_account` 最终 13 项通过：真实 HTTP 初次创建/接管/重新登录、重启、实际模型解锁、两独立进程竞争、只读 preflight、账号与封条损坏/缺失、空表拒绝、移除配置禁止旧密码回退、事务前失败、事务后成功响应丢失、Cookie/CSRF/Origin/未知字段/限流、内部授权撤销、安装凭据独立性及去敏投影。
- `test_web_console/test_web_models/test_web_access/test_home` 87 项通过；与当时账户 11 项合跑为 98 项通过。
- `test_runtime_health` 87 项在配置真实 TLS 解释器后全部通过；与当时账户 12 项合跑为 99 项通过。初跑 TLS 环境未设置时 81 项跳过不计通过，补环境后已实际执行。
- `test_audit_runtime` 6 项通过。修改文件的 Ruff 格式/静态检查通过。

环境：`TS012_CONTRACT_DIR=<根>/contracts/text-dialogue/v1`，`TS013_TLS_PYTHON=<根>/.runtime/nas-a1-r1-venv/Scripts/python.exe`。以上均不是 NAS 实机、容器或完整产品联合验收。

限制：磁盘故障/初始化中断可留下不可用未完成文件并保守 503，需离线检查/一致恢复；跨进程竞争败方可能 409 或短暂 503。整卷连同封条一同删除无法与新卷区分，不提供匿名恢复。不能仅回滚到不理解账号库的旧二进制或删除账号配置/数据；必须维持一致的代码、配置和数据恢复集合。未实现多用户、忘记密码、邀请或在线重置。

下一步：协调者集成统一前端入口与部署适配，做真实本地浏览器初始化/接管流程；受控 NAS 版本更新和实机验收另行执行。
