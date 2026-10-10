# 订阅与多模型发布候选前端验证

2026-10-11，在父级隔离合并候选 `7ef05e3` 上验证；仅修正 App 合并排版、模型浏览器用例排版及壳边界测试的旧假设。产品路由保持线上 `settings/7 → memory/0`、设置空项和新增订阅导航，不改业务行为、后端、合同或依赖。

- TypeScript 类型检查、Vite 生产构建、受影响前端 Prettier 和 `git diff --check` 通过。保留既有 renderer 大包提示。
- 订阅 dashboard/navigation 三尺寸 45/45 通过，23.4 秒，含浅/暗色、Axe、抽屉、旧书签、登录回跳和刷新返回；使用明确合成媒体 API。
- 多模型桌面/手机真实平台 HTTP 浏览器 2/2 通过，9.6 秒。隔离供应商目录与登录身份，验证保存、刷新、继承、主模型同步与独立配置；无真实供应商调用或真实账号。
- 壳首轮 13 passed / 6 failed / 1 conditional skip，20.3 秒。两项边界失败是旧测试仍把已存在的 `settings/9` 天气页当作非法；改为当前数组上界并补合法 9 及旧 7 跳转断言，窄重跑 2/2 通过，1.7 秒。四项小屋标题旧失败保留；其他已通过输入未重跑，未另建基线。

浏览器命令为 `playwright.media.config.ts` 的 `subscriptions-dashboard.spec.ts subscriptions-navigation.spec.ts`、`playwright.model-functions.config.ts`、`playwright.config.ts shell.spec.ts`，均通过 `node node_modules/@playwright/test/cli.js test --config ...` 执行。模型夹具显式使用 `TS012_CONTRACT_DIR=C:/YOKI/Codex/tianshu-peiban-bot/.runtime/features-release-20261010/contracts-input/contracts/text-dialogue/v1` 及此候选 `.runtime/venv/Scripts/python.exe`，未使用根 CRLF 合同。

结果与截图位于本检出 `.runtime/release-validation/{subscriptions,model-functions,shell,shell-boundaries}`；已查看模型桌面/手机截图。节点依赖和 Python venv 通过已授权 junction 只读复用。父级负责镜像构建、推送与部署，本执行者未推送或部署。
