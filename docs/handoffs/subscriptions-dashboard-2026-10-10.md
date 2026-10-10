# 订阅概览与竖向导航交接

本轮 2026-10-10 开始、10-11 完成本地交付。分支 `codex/subscriptions-dashboard-20261010`，基线 `123acfa302cfe27969746584c406b98ecda8d200`；独立检出 `worktrees/subscriptions-dashboard-20261010/frontend`。父级负责串行集成；未推送或部署。

根 `#/subscriptions` 默认概览（同 `/4`）；`/0` 管理、`/1` 链接、`/2` 下载中心、`/3` 账号保留。旧 `#/resources/2` 跳到 `/0`，登录回跳、刷新与浏览器返回已覆盖。原主导航“订阅”激活时展开五项带图标竖向子导航，手机抽屉可达；本模块移除占空的大标题、说明和横向标签，保留面包屑与可访问标题。

概览复用媒体控制器、现有业务页面、状态和设计令牌，展示全量任务 KPI、订阅环图与卡片、暂存盘、环境、最近五项任务及快捷入口。缺少 overview 不以近期列表推全量；磁盘未知不冒称零容量，目标配置不冒称在线。参考 Easy-vdl 本地 Dashboard JS/CSS 与编译导航的布局；仅读，未执行或复制整套产物。完整行为说明见 [media-web.md](../platform/media-web.md)。

实际验证：

- 类型检查、Vite 生产构建及修改文件格式检查通过；构建保留既有 renderer 大包提示。
- 生产浏览器行为 90/90 通过（媒体 48、导航 21、概览边界 21），33.0 秒。
- 概览三尺寸浅/暗色、减少动态、Axe 与无横向溢出专项 3/3 通过，6.1 秒；已实际查看桌面、手机及手机导航截图。父级视觉核对通过。
- StrictMode 开发浏览器 1/1 通过，2.7 秒。
- 必需应用壳检查 15 passed / 4 failed / 1 conditional skip，19.9 秒。四个失败均为既已确认的 `shell.spec.ts:174/187` 桌面及手机用例等待“小屋环境预览”标题；未改变或修复旧故障。
- 原真实 HTTP joint 套件适配竖向导航，追加概览服务端统计及暂存盘断言（桌面/手机共两项）。本检出未运行该 HTTP 套件；父级合入后运行，共六项。前端替身 UI 通过与真实平台 HTTP 联验分别记录。

本地截图（不入库）：`.runtime/subscriptions-dashboard/screenshots/overview-light-desktop.png`、`overview-dark-desktop.png`、`overview-light-mobile.png`、`overview-dark-mobile.png`、`overview-mobile-navigation.png`。其他结果位于同一 `.runtime/subscriptions-dashboard` 下的 `behavior-test-results`、`visual-test-results`、`dev-test-results`、`shell-test-results`。

检查命令沿用 [media-web.md](../platform/media-web.md)；真实 HTTP 联调设置 `MEDIA_JOINT_URL` 和隔离控制 URL `MEDIA_FIXTURE_CONTROL` 后执行 `node node_modules/@playwright/test/cli.js test --config apps/web/playwright.media-joint.config.ts`。后端 overview 由父级另行集成，本分支不改后端、依赖清单或锁。
