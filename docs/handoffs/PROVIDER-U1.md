# PROVIDER-U1：模型供应商网页交接

## 目标、基线与合同

产品基线 `bc41c05808271dc03d643d567e43788978e1e587`，分支 `codex/provider-web-20260926`。第一阶段组件提交 `342307e79bcdfa6227983b2cf8a5d21e6ed9df02`；本阶段继续接入真实同源 HTTP。接口按根协调 `contracts/provider-self-service/v1` 候选合同（根提交 `c644b7b`、`d4fdf5c`），平台 `5b16d0e`、网关 `294f117` 的已提交实现验收。未修改后端、共享合同、协调检出或 NAS。

## 实现

- 普通“设置 → 模型与用量”现使用 `ProviderModelsPanel`，只显示供应商管理，不展示模板发布版本、工作负载、凭据引用等内部字段。复用实际 `/api/web/session`、`models/view`、`models/unlock`、`models/lock` 的 Cookie/Origin/CSRF 与管理员二次验证；旧模板组件源码保留兼容但不再作为普通入口。
- `providerApi` 映射 v1 去敏 `Provider`/`Default`，只通过同源 `/api/web/providers/*` 操作。新建和编辑带 UUID 幂等键，编辑/清密钥/删除/测试/设默认带供应商修订 CAS，设默认再带默认指针修订 CAS。每次写入后重读权威列表；失败测试也重读以显示最新摘要。取消等待不自动重发。
- 供应商卡片、空态、添加/编辑、OpenAI/DeepSeek 预设与自定义 HTTPS Chat Completions 服务；先保存空模型 ID，再获取或手填模型。保存不发生成请求；模型列表不冒充短回复成功。测试按钮旁明确提示简短测试、可能费用、不发送聊天历史。密钥不回显、编辑留空保留、独立清除；测试和默认受当前修订约束。可停用、删除；当前默认先换选。
- 页面走局域网 HTTP 时提示网页与平台间未加密，不增加浏览器 HTTPS 门槛；首期模型供应商地址依后端合同要求 HTTPS。首次对话 `model=not_configured` 时有直达配置入口。错误密钥、枚举不支持、修订冲突与未知执行有固定用户提示。

## 实际验证

在 U1 工作区复用 `ROOM-INTEGRATION` 的本地 `node_modules` junction；未改依赖或锁。命令：

- `node node_modules/prettier/bin/prettier.cjs --check`（本次改动前端文件）：通过。
- `node node_modules/typescript/bin/tsc -p apps/web/tsconfig.json --noEmit`：通过。
- `node node_modules/vite/bin/vite.js build --config apps/web/vite.config.ts`：通过；现有 RoomPage 大包提示与本改动无关。
- `node node_modules/@playwright/test/cli.js test --config apps/web/playwright.providers.config.ts`：独立合成组件 **2/2**；包含保存/枚举/测试/默认分离、修订失效、密钥留空、axe 与移动宽度。此套不调用后端。
- 隔离真实 HTTP 夹具：使用产品已有 Python 3.12 测试环境运行 `apps/web/tests/provider-real-fixture.py`，提供 `TIANSHU_WORKSPACE=C:/Users/Administrator/.codex/worktrees/6bd8/tianshu-peiban-bot` 与 `TIANSHU_PRODUCT_WORKSPACE=C:/YOKI/Codex/tianshu-peiban-bot`，再运行 `node node_modules/@playwright/test/cli.js test --config apps/web/playwright.providers-real.config.ts --workers=1`：**4/4**。夹具用真正的平台与网关 HTTP、隔离录制 TLS 上游和生产构建的前端静态资源；关闭旧 `web_models` 模板功能。真实浏览器走登录→解锁→空配置→保存（零 completion）→枚举→留空密钥编辑→短回复测试（恰一 completion）→设默认→开始对话跳转。另验手填模型、切换默认、清密钥/停用/删除、枚举不支持与错误密钥的可理解错误、上游错误正文不泄漏、浏览器请求同源、密钥不在 localStorage。
- 真实浏览器截图：`apps/web/test-results/providers-real/provider-real-real-same-or-ef3fe-y-and-recorded-TLS-upstream/provider-real-same-origin.png`；组件截图：`apps/web/test-results/providers/provider-manager-synthetic-136c8-n-test-and-default-distinct/provider-manager-synthetic.png`。均为忽略的本地测试产物，无真实密钥。

## 限制与下一步

真实浏览器验证仅在隔离本地平台、网关与录制上游执行；无公网供应商、付费请求、NAS 或实际用户数据。浏览器“开始对话”已到陪伴页，网页夹具并未装配完整 Companion 服务，因此不声称浏览器收到真实聊天回复；后端根联合测试另有 Companion Core 新回合贯通证据。生产服务身份、证书、部署网络、快照续期与实机聊天仍由协调集成/部署验收。根 `v1` 为本地候选合同，需协调者按单一负责人审查合入后才算发布。

## 参考

- 根协调：`docs/development/provider-self-service-scope-2026-09-26.md`、`docs/handoffs/PROVIDER-BACKEND-2026-09-26.md`、`contracts/provider-self-service/v1/README.md`。
- OpenAI 官方 [Models API](https://platform.openai.com/docs/api-reference/models/object) 与 [API reference](https://platform.openai.com/docs/api-reference/introduction)；DeepSeek 官方 [多轮 Chat API 指南](https://api-docs.deepseek.com/guides/multi_round_chat/)。地址预设不代表账号、模型 ID 或生成能力已验证。
