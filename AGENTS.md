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

测试自动管理 4173 端口预览，不复用未知进程。截图和报告为忽略的运行产物。前端未配置独立 lint，前端当前无需环境变量。改动稳定后审查完整 diff，再按格式/类型、最窄回归、受影响套件验证；脚手架涉及全壳时运行全部浏览器套件。不要重复输入不变的成功检查。

## 后端实际命令与边界（TS-012）

Python 3.12+，独立 `pyproject.toml` 与固定依赖 `requirements-dev.txt`。安装、运行和完整接点见 [平台后端运行](docs/platform/runtime.md)。后端改动不重复运行输入不变的前端 WebGL 套件。

- 安装：`python -m venv .venv`；`.venv/Scripts/python.exe -m pip install -r requirements-dev.txt`；`.venv/Scripts/python.exe -m pip install --no-deps -e .`。
- 格式/静态检查：`.venv/Scripts/python.exe -m ruff format --check services/platform tests/backend`；`.venv/Scripts/python.exe -m ruff check services/platform tests/backend`。
- 受影响后端套件：设置 `TS012_CONTRACT_DIR` 后，`.venv/Scripts/python.exe -m unittest discover -s tests/backend -v`。真实平台→网关→录制上游子链另设置 `TS012_GATEWAY_SRC` 为已集成网关 `src`；缺失时明确跳过，不当作全链通过。
- 本地服务：`.venv/Scripts/python.exe -m services.platform --settings <隔离设置JSON> serve --port <本地端口>`。仅 loopback/local_rehearsal/sqlite_local；生产 PostgreSQL/登录/真实设备均未配置。
- 管理命令：同一入口 `local --credential-env <已登记环境变量> <action> --input <本地JSON>`；由服务器登记身份认证，不接受 payload 自称管理员。

服务代码位于 `services/platform/`，测试位于 `tests/backend/`。不修改已发布合同、不猜新增公共 HTTP 写入协议。来源签发与映射/来源核验为受信内部端口，真实适配器不得把未验证 payload 注入。平台唯一写模型配置；网关只读。前端只能接脱敏投影，不获得服务 token、origin_ref 或完整 secret-ref。环境凭据、数据库、真实数据与本机设置不入库；测试必须隔离，明确区分 CLI 认证、真实登录/渠道以及局部 HTTP 子链和 L0/L1。

## 共享边界

- 根清单/锁、`src/app/modules.ts`、应用壳、`src/design/tokens.css` 为集成人单写。模块作者仅改分配的 `src/features/<module>/`。
- 遵循 [模块接入规则](docs/module-integration.md)，不另建壳、主题或入口。重模块懒加载；离开和隐藏后清理渲染与订阅。
- 唯一 CSS 令牌源；业务不复制颜色/渐变。Docker 用蓝黄红灰，运行与健康分开，未知不等于停止，选中不覆盖细轨。
- 无发布 schema 和双边验收不猜 API。前端不保存权威业务状态，不把浏览器声称的权限当授权。
- TS-010 小屋仅入口。日记未授权不取全文。实时语音和观影不进网页。

本地隔离开发和提交用于审查；不自动推送、部署或操作真实设备。交付短记录 docs/handoffs/<任务编号>.md，含实际变更、验证、风险及下一步。
