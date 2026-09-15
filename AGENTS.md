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

## TS-013 来源同步增量

- 唯一新运行包 `contracts/source-sync/v1` 1.0.0，固定 manifest SHA256 `178d0ce66210bdfad4cfb85d8b5f0905b0b67f834e2a530efe5636ff0373633d`；绑定旧 text/profile，不读 candidate。
- [完整运行说明](docs/platform/source-sync.md) 与 [合成配置](docs/platform/source-sync.settings.example.json)。新增固定 `POST /internal/v1/source-access/read` 的 input/current；认证分别是 companion/memory。纯规则不是认证器，源输入/批量回填仍是受信应用端口，不新增映射 RPC。
- 新最窄套件：`.venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_sources.py -v`；TLS套件将 pattern 改为 `test_source_https.py`。完整后端仍用原 discover 命令。
- TLS测试设置 `TS013_TLS_PYTHON` 为含 cryptography 50.0.1 的工具解释器，本机为 `C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。证书仅临时生成，不装系统信任；缺此变量明确 skip。设置 `PYTHONDONTWRITEBYTECODE=1` 后读取已集成网关，禁止写另一产品目录。
- `mode=service_https` 为显式认证 TLS 模式，serve 使用设置中的绝对证书/私钥路径；默认 local_rehearsal 和旧回归保留。SQLite迁移先备份旧库；所有真实数据、生产恢复与新generation批准未执行。当前后端45项，完整L0仍待产品集成。
- legacy 单actor回执和 partial source观察不得伪造新 input/admission 历史；未知历史 current 503。产品范围限定 services/tests/backend/docs/入口说明，不改网页及锁。

## TS-064 资产后台只读

- [配置、协议与真实联合验证](docs/platform/assetlink.md)。仅 `platform.assets.read` 与 CLI `asset-read`，无公共 HTTP/UI 路由；五读按现行 AssetLink，不能复制对端 ACL 或借 dialogue/admin 会话赋库权。
- 最窄：`.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_assets.py -v`，沿用 `TS012_CONTRACT_DIR/TS013_TLS_PYTHON`。旧后端回归仍用完整 discover；已通过最窄且输入未变时可仅运行其余四个实际 test 文件，避免重复。
- 联验：`.runtime/venv/Scripts/python.exe tests/backend/run_assetlink_integration.py --asset-repo <只读AssetLibrary-Git仓库> --dotnet <10.0.111工具> --postgres-bin <PG16.15-bin>`。只导出固定ff8e8a1到自己的.runtime并构建，自建临时PG；不得改对端快照源码、在projects写bin/obj或SQL灌权限。
- 请求64KiB、流式响应1MiB、单次5秒；401/404/错误清正文不循环重试，取消传播并关闭会话。成功只是索引元数据，原件可用性未验证；网页、50万、生产NAS仍未验。

## TS-015 平台原生模型配置

- 设置、错误语义、迁移与边界见 [原生模型配置](docs/platform/native-model-config.md)。合同为根 `contracts/model-protocol/v1`，manifest LF SHA256 `52711a71de56dbceebd1d5d96b2baf59a2d9551168029972d59111480f815141`；平台只做生产者，网关消费与真实模型另行验收。
- 唯一新端口 `POST /internal/v1/model-config/native/snapshot`，默认关闭，设置 `native_config_http: true` 才注册；CLI 为 `publish-native`、`revoke-native`、`view-native`，复用既有 `config.publish/revoke/view` operator 权限，不新增网页入口。原生使用独立 `native_configs` 表、`native_config_version` 序列、撤销键空间与显式 `caller.native_config_versions`；缺省为空，绝不继承 `config_versions`，旧 Chat 的 latest 与状态码不变。
- 最窄：设置 `TS012_CONTRACT_DIR` 后 `.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p 'test_native*.py' -v`。旧 Chat/Web/Asset 回归仍用完整 discover。本地 SQLite schema 为 2；版本 1 旧库迁移前生成 `.pre-native-<hex>.sqlite` 备份。
- 没有真实模型账号、供应商调用、浏览器入口或生产部署；根 `runtime_routes_enabled=false` 与 `runtime_disabled_until_joint_acceptance` 仍有效，本地开端口只是隔离演练。

## TS-016 网页模型配置管理

- 设置、授权、端口与错误语义见 [网页模型配置管理](docs/platform/web-models.md)。网页“设置 → 模型与用量”显示 Chat 与原生各自版本、绑定与失效/撤销状态，并可预览、发布、撤销；写入仍只经 `Platform.Models`，不新增配置表与 `/internal/v1/*` 端口。
- 可选顶层设置 `web_models`（默认关闭）登记已审查模板；浏览器只提交 `template_id`、`expected_version` 与幂等 `client_id`，URL/凭据/namespace 全由服务器从 `providers` 登记填写，投影不含端点与 secret-ref。
- 写权限要三步齐全：`web_models.enabled`、operator 的 `config.publish/revoke/view`、以及当前会话的显式 `models/unlock` 密码解锁（与登录共用限流）。普通聊天登录不获得管理权限；重启、退出、撤销与凭据轮换都会失效。
- 端口为同源 `/api/web/models/{view,unlock,lock,preview,publish,revoke}`，复用真实 Cookie/CSRF/Origin；旧版本 409 `version_conflict`，重放同 `client_id` 返回首次结果，撤销后 Chat 快照 410、原生 403。发布不假设回执必然写成：先落 `prepared` 意图（含该版本稳定摘要），再经 `Models` 写权威库，回答前一律与权威表对账，因此「已提交但回执丢失/进程中断」重试恢复为同一版本，回执缺失但权威行不存在时报 503 `publication_unverified`。运行产物 `<db>.web-models.sqlite` 只存意图与回执，不存端点、凭据引用或任何配置内容。
- 最窄：设置 `TS012_CONTRACT_DIR` 后 `.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p 'test_web_models.py' -v`；浏览器路径先 `npm run build`，再 `node node_modules/@playwright/test/cli.js test --config apps/web/playwright.web.config.ts`。完整后端仍用原 discover。

## TS-017 家庭设备状态与受限控制

- 设置、读数/控制语义、错误码与验证入口见 [家庭设备状态与受限控制](docs/platform/home-devices.md)。网页“家庭与服务 → 家庭设备”（`/#/home`）显示已登记 HA 实体的读数、可用性与采样时间；只读传感器不显示控制，light/switch 在显式解锁后按服务器登记模板执行 `turn_on`/`turn_off`。仅使用官方 REST 状态读取与服务调用，不用 `POST /api/states` 冒充控制；没有门锁、门、报警、场景或脚本动作。
- 可选顶层设置 `home`（缺省即没有入口）登记 HA 地址、凭据环境变量、实体与动作模板；浏览器只提交 `template_id`、`expected_revision` 与幂等 `client_id`，实体/域/服务/地址/凭据全由服务器解析。明文 http 沿用 `providers` 的已审查地址策略（`reviewed_addresses` + `allow_private_http`，本平台只批 loopback）。
- 控制权要三步齐全：`home.enabled`、operator 的 `device.control`、当前会话的 `home/unlock` 密码解锁（与登录共用限流，与 `models/unlock` 互相独立）。读数为同源 `POST /api/web/home/{view,refresh}`，控制为 `POST /api/web/home/control`；无 `/internal/v1/*` 新端口。
- 受理与观测分开：服务调用回执只表示 `accepted`（`target_reported` 只是回执中出现目标状态），只有受理之后的 `GET /api/states` 观测才是 `confirmed`/`contradicted`。未知结果（超时、连接中断、回执不可读、HA 5xx）保留且绝不自动重发；`prepared` 意图重试先按观测对账再决定是否发出一次目标状态动作；回执写失败不报假失败（`durable: false`）。`expected_revision` 过期 409 `state_conflict`，同键不同语义 409 `idempotency_conflict`。
- 运行产物 `<db>.home-controls.sqlite` 存读数（含采样时间与修订号）与意图表，不含地址或凭据；与权威库一起备份。取消只停止页面等待，不声称未发出或已回滚。
- 最窄：设置 `TS012_CONTRACT_DIR` 后 `.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p 'test_home.py' -v`（同进程起真实 loopback 合成 HA）；浏览器路径先 `npm run build`，再 `node node_modules/@playwright/test/cli.js test --config apps/web/playwright.web.config.ts`（合成 HA 在 4817）。完整后端仍用原 discover。
- 协调授权：本任务独占本产品 HA 相关路由，并已获准改 `apps/web/src/app/modules.ts` 的 `home` lazy 注册与 `App.tsx` 的一行页面挂载（`#/home` 段 0）；未改壳的其余部分、`tokens.css`、其他模块与依赖锁。没有真实 HA 实例、设备或生产部署。

## 共享边界

- 根清单/锁、`src/app/modules.ts`、应用壳、`src/design/tokens.css` 为集成人单写。模块作者仅改分配的 `src/features/<module>/`。
- 遵循 [模块接入规则](docs/module-integration.md)，不另建壳、主题或入口。重模块懒加载；离开和隐藏后清理渲染与订阅。
- 唯一 CSS 令牌源；业务不复制颜色/渐变。Docker 用蓝黄红灰，运行与健康分开，未知不等于停止，选中不覆盖细轨。
- 无发布 schema 和双边验收不猜 API。前端不保存权威业务状态，不把浏览器声称的权限当授权。
- TS-010 小屋仅入口。日记未授权不取全文。实时语音和观影不进网页。

本地隔离开发和提交用于审查；不自动推送、部署或操作真实设备。交付短记录 docs/handoffs/<任务编号>.md，含实际变更、验证、风险及下一步。

## TS-014 网页登录与对话

- 运行与身份边界见 [网页控制台](docs/platform/web-console.md)。同源静态入口 `/#/companion`；无web配置503。`web.dialogue_enabled`默认false，模型未发布不可发送。Cookie/CSRF与内部Bearer严格隔离。
- 正式web-conversation/v1 manifest LF SHA256 `e493a1b5d0f4cec8d55995553faf84042f4c33a59365d15423e57f4dc70a6c09`，来源/人格/发送仍复用旧包。候选目录仅留审查记录，不作运行入口。
- 最窄后端：设置`TS012_CONTRACT_DIR`后 `.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p 'test_web*.py' -v`。
- 真实网页登录浏览器：先构建，再 `node node_modules/@playwright/test/cli.js test --config apps/web/playwright.web.config.ts`；该配置用自己的临时合成后台4814。synthetic UI用例仅状态替身，不能替代真实四产品联合。
- 受影响壳回归：`npm run test:e2e -- shell.spec.ts`；不重复小屋渲染输入未变的WebGL套件。
- 当前SQLite权威库之外新增同路径`.web-inputs.sqlite`与`.web-replies.sqlite`，分别保存去重结果和持久sender；不通过sender数据库向网页绕过Core来源展示检查。
