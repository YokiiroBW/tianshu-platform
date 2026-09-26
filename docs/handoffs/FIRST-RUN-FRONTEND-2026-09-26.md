# FIRST-RUN-FRONTEND — 2026-09-26

基线 c1c7547，分支 codex/first-run-onboarding-20260926。本次仅前端与浏览器用例；后端、锁、小屋渲染资源未修改。

## 结果

- 独立 #/login 与 #/setup，匿名会话 CSRF；只有服务器明确 create_admin 才提供首次创建。支持确认密码、一次提交、安装验证码说明、部署账号认证后的 claim，成功重新读取服务器会话。
- 旧部署说明已有管理员账号，旧服务器缺少 onboarding 时仅登录。未知状态先等待，网络/503/异常响应不开放创建。setup_closed/setup_required/account_unavailable 提供刷新恢复。
- 壳统一会话与退出，受保护页面保留深链返回；移除聊天、人格、任务、设备、模型五套重复登录表单及局部退出。原模型/设备/访问设置二次密码验证保留。人格/任务原有 scope 和代次保护保留。
- 401/会话异常先后台重读权威 session，只有确认匿名才跳登录；重新读取失败显示故障。退出立即卸载私人页面与请求，服务未确认退出时准确说明，可重新连接核对。跨标签确认退出广播不携带凭据。
- 工作台提供账号 → 模型 → 角色/对话引导。模型 not_configured 与连接缺失分开；unverified 只说明配置登记、允许尝试第一条对话，不宣称实机验证。未知模型状态不显示可对话。
- 保持玻璃材质/配色/导航结构，小屋仍是公开本地预览。模型管理目前只能发布安装者预登记模板，已明示不能直接填写服务提供商地址/密钥。

## 实际验证

- TypeScript 全前端类型检查通过；生产构建通过（既有小屋大块提示仍在）；改动文件 Prettier 检查与 git diff --check 通过。
- `node node_modules/@playwright/test/cli.js test --config apps/web/playwright.onboarding.config.ts`：53 passed，1 skipped（既有桌面跳过移动导航专用用例）。包括新引导 18 项（桌面/移动），以及全部 shell/room/status-rail/access-settings 静态 UI 套件；含 WCAG、深浅主题、200%文字、WebGL像素、卸载清理、网络故障、401仍登录、setup关闭恢复。
- 静态 UI 用例明确模拟 session；生产没有登录绕过、没有 localStorage 初始化标记。小屋原“无业务请求”断言仅允许壳 GET session，其余业务/网络请求仍禁止。
- UI预览独占5190，本次未使用其他服务端口、未操作NAS。
- 截图运行产物：`.runtime/onboarding-screenshots/`，桌面/移动的 first-run-setup、first-run-next-step、unified-login 共六张，已目视检查桌面设置和移动登录。

## 集成与未验

后端合同由父任务提供并随后集成。此次浏览器结果是明确的合成状态验证，不是新后端真实HTTP或NAS实机验收。父任务将用5192/5193做真实创建、退出、重登和claim闭环。旧专用真实服务套件（web-console/personas/assets/tasks）各有自己的后台装置，未在本轮占用4814等端口；web-console网络失败断言已适配统一入口，旧persona退出故障用例中局部 `.persona-state` 断言应在真实套件迁移时按壳级退出提示调整。

网页模型配置仍依赖服务器已有模板；添加提供商编辑器不在本次范围，不能把引导入口当作该能力已交付。
