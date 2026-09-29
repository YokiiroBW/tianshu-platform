# QQ-IDENTITY-20260929 · Platform

- 目标：用已登录后台维护 QQ 管理身份；从已验收机器人事件保存可追溯称呼；把只读身份核验交给 Companion。初始授权为空。
- 基线：`c897e8b2351694808a1231f2ce1245425235c8e1`；协调者后续多角色候选 `4026e8f698cfa57bbc18ee0a5f74469cf1aa95f7` 需先完成本任务提交后按冲突对齐。交付提交为包含本文件的本地 `codex/qq-identity-20260929` HEAD。
- 变更：新增独立 SQLite 管理授权/CAS/去敏审计，后台 `qq-admin` 页面与服务查验 `/internal/v1/qq-admin/check`；来源断言决定 QQ 号及会话，QQ ID 严格校验；事件 v2 只增加昵称和群名片，接受后的别名待办可重试；观察入口使用同一无猜测长度的 QQ 号格式。
- 合同：`docs/contracts-candidates/qq-identity/v1` 是候选，不是根目录已冻结合同。v1 事件继续兼容；v2 事件需有 `nickname`、`group_card` 两字段。
- 实际验证：`test_bots.py` 管理页面 HTTP 登录/CSRF、授权/CAS/撤销、服务身份查验、来源不匹配、别名待办；`test_web_console.py`；`test_bot_observation.py`；三产品隔离 HTTPS 观察联测 `test_observation_http_joint.py` 2/2，新增 v3 用例在 Memory 离线后恢复，核对同号跨 BOT/群私、同名异号、来源别名、重启重读和零回复；NoneBot 宿主到 Platform/Core 的 HTTPS 联测；TypeScript typecheck、Vite build；HTTPS Chromium 桌面/手机 `qq-admin.spec.ts` 2/2，三状态截图在忽略目录 `.runtime/qq-browser-results/`。末次浏览器测试由 `run_qq_identity_browser_real_memory_fixture.py` 起真实 Memory HTTPS 服务并通过 Platform 档案代理读回，种子账号为合成注册。
- 部署前配置：后台 operator 需 `qq.admin.view`、`qq.admin.manage`；Companion 专用 service principal 需 `qq.admin.check` 和其原 dialogue 来源路由。档案页面可配置 `web_qq_profiles={base_url,token_env,ca_file}`；称呼写入可配置 `qq_alias_memory`，两者与其它凭据名称和值均须独立。Memory 先执行显式 QQ 别名迁移并配置两个 Platform 服务凭据及操作。备份平台数据库时同时备份 `.qq-admin.sqlite` sidecar。
- 增量：观察 v3 由 Platform 以完整事件摘要绑定来源；称呼待办只存身份/显示字段、不保留正文，持久游标轮转待办避免前 32 项失败时后续饥饿。浏览器真实 Memory fixture 启动：先设 `TS_QQ_MEMORY_ROOT=<本任务 Memory 检出>`、`TS012_CONTRACT_DIR=<根合同目录>`、`TIANSHU_TEST_CERT_PYTHON=<可用证书 Python>`，执行 `python tests/backend/run_qq_identity_browser_real_memory_fixture.py`；另终端执行 `node node_modules/@playwright/test/cli.js test --config apps/web/playwright.qq.config.ts`。
- 未完成/风险：尚未对最终多角色固定候选重基、未发布根 QQ 合同；未部署/迁移生产数据，也未连接真实 QQ、模型或 NAS。完整 AstrBot 4.27.3 宿主没有在本环境执行。下一步由协调者串行审查三提交、联合样例与配置后决定部署。
