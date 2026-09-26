# PROVIDER-U1 前端阶段交接（HTTP 契约待定）

## 目标与基线

基线 `bc41c05808271dc03d643d567e43788978e1e587`，分支 `codex/provider-web-20260926`。目标是将普通模型设置页改成供应商自助管理，并引导首次对话配置。本次先完成独立组件；后端跨产品 HTTP 契约尚未发布，**网页主入口仍是旧模板页，不能称自助配置已接通**。本提交只供后续适配与审查，不应单独集成或部署。

## 已完成

- `ProviderManager.tsx`：去敏供应商卡片、空态、添加/编辑、OpenAI/DeepSeek 官方地址预设和自定义 HTTPS Chat Completions 服务；允许先保存空模型 ID，随后读取模型或手填。保存不会触发上游测试。页面通过局域网 HTTP 打开时提示网页到平台的传输未加密，不强制页面 HTTPS；首批上游供应商地址按 P1 当前能力要求 HTTPS。
- 区分“获取模型”和“测试回复”。短回复测试前明确展示可能费用与不发送聊天历史；结果必须绑定当前 revision，旧结果不使默认按钮可用。默认、停用、清密钥、删除为独立操作；当前默认供应商的停用/清密钥/删除被禁用并提示先切换。编辑默认供应商提示测试及默认失效。
- 编辑密钥空白保留；独立清除密钥按钮，不回显。组件未使用 localStorage、sessionStorage、日志或直连上游请求。操作等待可取消；未知执行不自动重试。
- 首次对话 `model=not_configured` 时增加进入模型设置的链接。旧页尚未替换，故此引导需随 HTTP 接线一同验收。
- 补充固定错误码的可理解提示。最终码集合应与正式合同核对。

## 验证与证据

- `node node_modules/typescript/bin/tsc -p apps/web/tsconfig.json --noEmit`：通过。
- `node node_modules/@playwright/test/cli.js test --config apps/web/playwright.providers.config.ts`：合成组件 2/2 通过。覆盖保存/枚举/测试/默认的分离、修订失效、密钥留空、HTTPS 上游与 HTTP 页面区分；含 axe 和移动宽度检查。**夹具不代表平台接口、网关或真实模型通过。**
- 合成 UI 截图：`apps/web/test-results/providers/provider-manager-synthetic-136c8-n-test-and-default-distinct/provider-manager-synthetic.png`（忽略的本地测试产物）。截图展示默认卡片与操作状态，不含真实密钥。
- `git diff --check`：通过。仅改 `apps/web/**` 和本交接。

## 后端合同依赖与下一步

需协调者发布并由平台生产者、网页消费者共同确认：`/api/web/providers/*` 的实际方法/路径、去敏列表与默认指针字段、save/clear-key/delete/default 的 CAS/幂等字段、模型枚举与短回复测试的鉴权/CSRF/超时及取消语义、固定错误码与测试状态。P1 工作区在途 `provider_management.py` 仅供观察，**不视为冻结合同**。

合同到位后：按真实 schema 写同源 API 适配器；替换 `ModelsPanel` 的普通模板发布视图并复用已有登录/管理解锁；所有变更后重新读取权威列表，特别是测试失败或结果未知；用隔离平台+网关录制上游做 HTTP 联验，再做浏览器设置页/首次对话引导证据。不可将当前合成结果称真实接入。自动租期、动态授权与陪伴实际回复由后端/协调联合验收。

## 参考

- 根协调：`docs/development/provider-self-service-scope-2026-09-26.md`、`docs/development/provider-wave1-dispatch-2026-09-26.md`。
- OpenAI 官方 [Models API](https://platform.openai.com/docs/api-reference/models/object) 与 [API reference](https://platform.openai.com/docs/api-reference/introduction)；DeepSeek 官方 [多轮 Chat API 指南](https://api-docs.deepseek.com/guides/multi_round_chat/)。地址预设不代表账号、模型 ID 或生成能力已验证。
