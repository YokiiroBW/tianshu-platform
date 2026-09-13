# 项目开发约定

遵循天枢主工作区的 V2 总稿、当前任务卡及已发布的 contracts 版本。任务 worktree 的 .runtime/workspace-context.json 记录主工作区与明确基线。

只在分配的目录内实施，不改其他项目或共享合同；公共入口、依赖锁和迁移主线单人负责。禁止把未配置服务显示成成功。

## 实际验证命令

Node.js 22.12+，npm 11.6.2，根单一清单与锁。

| 目的             | 命令                                             |
| ---------------- | ------------------------------------------------ |
| 安装             | `npm ci`                                         |
| 开发             | `npm run dev`（127.0.0.1:5173）                  |
| 格式检查 / 修正  | `npm run format:check` / `npm run format`        |
| 类型检查         | `npm run typecheck`                              |
| 构建             | `npm run build`（含类型检查）                    |
| 浏览器准备       | `npm exec playwright install chromium`           |
| 最窄回归         | `npm run test:e2e -- --grep "connection search"` |
| 应用壳浏览器套件 | `npm run test:e2e`（先 build）                   |
| 手动构建预览     | `npm run preview`（127.0.0.1:4173）              |

测试自动管理 4173 端口预览，不复用未知进程。截图和报告为忽略的运行产物。未配置独立 lint、后端测试或业务单测，不虚报存在。当前无需环境变量。改动稳定后审查完整 diff，再按格式/类型、最窄回归、受影响套件验证；脚手架涉及全壳时运行全部浏览器套件。不要重复输入不变的成功检查。

## 共享边界

- 根清单/锁、`src/app/modules.ts`、应用壳、`src/design/tokens.css` 为集成人单写。模块作者仅改分配的 `src/features/<module>/`。
- 遵循 [模块接入规则](docs/module-integration.md)，不另建壳、主题或入口。重模块懒加载；离开和隐藏后清理渲染与订阅。
- 唯一 CSS 令牌源；业务不复制颜色/渐变。Docker 用蓝黄红灰，运行与健康分开，未知不等于停止，选中不覆盖细轨。
- 无发布 schema 和双边验收不猜 API。前端不保存权威业务状态，不把浏览器声称的权限当授权。
- TS-010 小屋仅入口。日记未授权不取全文。实时语音和观影不进网页。

本地隔离开发和提交用于审查；不自动推送、部署或操作真实设备。交付短记录 docs/handoffs/<任务编号>.md，含实际变更、验证、风险及下一步。
