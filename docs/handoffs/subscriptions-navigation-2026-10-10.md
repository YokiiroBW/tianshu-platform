# 订阅独立导航本地交付

基线 `44de324`；分支 `codex/subscriptions-navigation-20261010`，候选为本文件所在提交。仅本地开发和提交，未部署、推送或修改后端/依赖。

- 个人空间中“用户”后新增“订阅”。`#/subscriptions` 与 `/0` 默认订阅管理，`/1` 链接下载、`/2` 下载任务、`/3` 账号与媒体库。复用原媒体页面和应用壳导航，移除重复标题/标签栏及资料与资源的旧入口。
- 旧 `#/resources/2` 替换为新默认入口，保留书签与登录回跳；任务中心 `resources.download` 责任链接直接进入 `/2`，来源合同不变。
- 查找覆盖已读取的全部订阅，可按名称、来源 ID/类型、账号或媒体库搜索；状态筛选、读取范围汇总、显示更多、无订阅与无匹配分别呈现。保留原订阅编辑/规则/历史扫描/暂停/恢复等功能，补充扫描安排和登录/规则恢复提示。

实际验证：

- TypeScript 类型检查、Vite 生产构建、格式检查与 `git diff --check` 通过；构建保留既有 renderer 大包警告。
- `playwright.media.config.ts`：原媒体 48 项通过；新增导航/查找等 21 项通过，含桌面 1440、平板 1024、手机 390。首轮 63 通过、6 个新测试定位器失败，修正定位器后仅补跑相关 6 项，6/6 通过；产品代码未因此修改，不重复未变成功项。
- `playwright.media-dev.config.ts`：开发 StrictMode 1/1 通过。
- 完整 `shell.spec.ts`：15 通过、4 失败、1 条件跳过。4 失败为基线已知的小屋 `小屋环境预览` 标题断言（174/187 行 × desktop/mobile），非本次导航回归；未改旧测试、未重复导出旧基线。
- 桌面/手机截图实际检查，无横向溢出。存于本检出 `.runtime/subscriptions-navigation/screenshots/subscriptions-desktop.png` 和 `subscriptions-mobile.png`；合成 API 数据，仅用于前端验证。

等价执行命令：

```powershell
node node_modules/typescript/bin/tsc -p apps/web/tsconfig.json --noEmit
node node_modules/vite/bin/vite.js build --config apps/web/vite.config.ts
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.media.config.ts
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.media-dev.config.ts
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.config.ts shell.spec.ts --workers 2 --reporter list --output .runtime/subscriptions-navigation/shell-test-results
```

`media-joint.spec.ts` 仅适配新路由/原生链接，本轮未重跑后端 HTTP 联验。下一步由父级串行合入候选；复用未变的有效验证结果。
