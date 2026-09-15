# 天枢平台

React/TypeScript 网页：统一应用壳、八个目的型工作区入口、浅深主题、可访问抽屉和按需模块加载。Python 平台提供持久来源解析、版本化模型配置，以及 TS-014 的真实本地登录与同源对话入口；未配置的模块明确显示不可用。仅供本地开发和审查。

后端安装、已发布 HTTP 入口、受信内部接点与验证命令见 [平台后端运行](docs/platform/runtime.md)。后端使用独立 Python 依赖，下面的前端命令和能力保持原有约定。

TS-013 后端新增精确来源输入、逐角色 `input/current` 授权、持久撤销水位和 Core 批量回执事务回填，支持显式 HTTPS 服务模式。完整配置、TLS测试、响应丢失重取及迁移恢复见 [来源授权运行](docs/platform/source-sync.md)。TS-014网页通过平台适配器消费这些端口，实际验证范围见任务交接；不据此宣称完整L0通过。

TS-015 后端在唯一 Models owner 内新增原生 `model-protocol/v1` 配置生产者：独立表/版本序列/撤销键空间、显式 `native_config_versions` 授权、默认关闭的快照端口和合同自有错误信封。设置、CLI 与错误语义见 [原生模型配置](docs/platform/native-model-config.md)；网关消费与真实模型仍未验收。

## 本地运行

TS-064 后端新增 AssetLink HTTPS 五只读应用/CLI入口、独立主体与连接授权、有界响应与取消。配置和真实双产品隔离联验命令见 [资产只读接入](docs/platform/assetlink.md)；资产网页仍未接入。

需要 Node.js 22.12+（本次使用 24.19.0）与 npm 11.6.2。根 `package-lock.json` 固定依赖，只有一个包清单和锁文件。

```sh
npm ci
npm run dev
```

开发地址 `http://127.0.0.1:5173`，端口占用会报错。无需环境变量、密钥或生产账号。路由使用 `#/workbench` 等 hash 地址，静态服务器无需业务路径重写。

```sh
npm run format:check
npm run typecheck
npm run build
npm exec playwright install chromium
npm run test:e2e
npm run preview
```

`build` 先检查类型，再输出 `apps/web/dist/`。`test:e2e` 需要先构建，自动在 `127.0.0.1:4173` 启动并关闭构建预览，已有同端口服务会拒绝运行。手动 `preview` 使用相同地址。`npm run format` 格式化源码与文档。没有单独配置 lint，不把类型检查称为 lint。

浏览器测试覆盖 1440×1000 桌面和 390×844 手机模拟视口、键盘、主题、减少动态、200% 文字、空搜索结果、无效地址、离线、存储拒绝、模块加载中与实际加载失败。截图与失败轨迹在忽略的 `apps/web/test-results/`，报告在 `apps/web/playwright-report/`。这是本地组件与浏览器模拟验证，不是真机性能或真实服务集成验证。

若本机只有 Codex 捆绑的 Node 与 pnpm，没有 npm，可在本任务目录运行同一 npm（不生成 pnpm 锁）：

```powershell
& C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/pnpm.cmd --package=npm@11.6.2 dlx npm ci
```

后续将末尾 `ci` 换为 `run build` 等 npm 参数。该绝对路径仅为本次 Windows 主机工具位置，普通开发环境直接使用 npm。

## 开发边界

- 应用壳和挂载入口：`apps/web/src/app/`。
- 唯一令牌：`apps/web/src/design/tokens.css`；公共状态、抽屉与细轨：`apps/web/src/components/`。
- 本地工作台与接入准备：`apps/web/src/features/workbench/`、`settings/`。
- 小屋懒加载边界：`apps/web/src/features/room/RoomPage.tsx`，保留已有环境渲染预览，未接权威生活状态。
- 后续接入：[模块与视觉约定](docs/module-integration.md)。

未配置页不请求猜测的 API，不回退示例成功。没有实时语音或观影入口。localStorage仅保存外观偏好；登录使用HttpOnly会话Cookie，服务Bearer不进入浏览器。角色、记忆、设备与任务以各后台为权威。

参考主工作区 V2 第 2、3.1、10、11 节、`docs/development/workstreams/platform-ui.md` 及认可的 `output/ui/management-v2`、`memory-diary-v2`、`docker-status-v3`。模块加载遵循 [React lazy](https://react.dev/reference/react/lazy) 与 [Suspense](https://react.dev/reference/react/Suspense)，构建使用 [Vite](https://vite.dev/guide/)。

## 网页对话入口

现有应用的 `/#/companion` 提供本地管理员登录、会话/角色选择和同源聊天控制台。配置、启动、模型未配置状态及实际接入边界见 [网页控制台运行说明](docs/platform/web-console.md)。默认关闭真实对话，没有内置账号或密码。

## 家庭设备入口

`/#/home` 的家庭设备段显示已登记 Home Assistant 实体的读数、可用性与采样时间，并在显式解锁后按服务器登记的动作模板执行 light/switch 开关。只读传感器不显示控制；受理回执与后续观测分开显示，结果未知不自动重发。设置、错误语义与验证入口见 [家庭设备状态与受限控制](docs/platform/home-devices.md)。本轮没有真实 HA 实例或设备。
