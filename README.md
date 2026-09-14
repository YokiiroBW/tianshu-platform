# 天枢平台

React/TypeScript 网页基础：统一应用壳、八个目的型工作区入口、浅深主题、可访问抽屉、按需模块加载与未配置状态。另有独立的本地 Python 平台后端，提供持久来源解析与版本化模型配置；网页尚未接入该后端，没有真实登录或外部渠道连接。仅供本地开发和审查。

后端安装、已发布 HTTP 入口、受信内部接点与验证命令见 [平台后端运行](docs/platform/runtime.md)。后端使用独立 Python 依赖，下面的前端命令和能力保持原有约定。

TS-013 后端新增精确来源输入、逐角色 `input/current` 授权、持久撤销水位和 Core 批量回执事务回填，支持显式 HTTPS 服务模式。完整配置、TLS测试、响应丢失重取及迁移恢复见 [来源授权运行](docs/platform/source-sync.md)。真实 QQ/网页登录和 Core/Memory 完整联合链仍未验收，L0 未通过；前端没有接入这些新后端端口。

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
- 小屋懒加载边界：`apps/web/src/features/room/RoomPage.tsx`，没有渲染引擎或生活状态。
- 后续接入：[模块与视觉约定](docs/module-integration.md)。

未配置页不请求猜测的 API，不回退示例成功。没有实时语音或观影入口。浏览器仅保存外观偏好，不保存角色、记忆、设备、任务或凭据。正式接入前需完成同源聚合、会话授权和版本化合同验收。

参考主工作区 V2 第 2、3.1、10、11 节、`docs/development/workstreams/platform-ui.md` 及认可的 `output/ui/management-v2`、`memory-diary-v2`、`docker-status-v3`。模块加载遵循 [React lazy](https://react.dev/reference/react/lazy) 与 [Suspense](https://react.dev/reference/react/Suspense)，构建使用 [Vite](https://vite.dev/guide/)。
