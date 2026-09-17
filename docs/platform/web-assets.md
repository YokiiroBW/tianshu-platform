# 资产库只读网页

网页“资料与资源 → 资产库”（`/#/resources/1`）是本平台唯一能看到远端资产库索引的地方。它是
**只读窗口**：只显示上游授权返回的索引元数据，不写入、不下载、不预览原件，也不缓存任何一页
内容。没有媒体端口的协议不会在这里长出缩略图或下载按钮。

## 数据从哪里来

页面自己不认识资产库。它只经同源端口把“哪一个操作、哪个库、哪个条目、什么查询”交给
**TS-064 的 `platform.assets.read`**，由那一个应用端口按现行 AssetLink 只读协议去读：

| 页面动作       | 上游操作         | 说明                       |
| -------------- | ---------------- | -------------------------- |
| 连接后的库列表 | `libraries.list` | 上游授权的库               |
| 打开一个库     | `entries.browse` | 目录分页，游标绑定本次范围 |
| 搜索           | `assets.search`  | 显式提交，不自动跨连接搜索 |
| 查看条目详情   | `entries.get`    | 按需重读一个条目           |
| （库详情）     | `libraries.get`  | 单库读取，网页按需使用     |

页面不新建第二套资产客户端，不复制对端 ACL，不做持久缓存，也不读 AssetLibrary 的可变数据库。
预算、超时、取消传播、错误清正文都仍是 TS-064 已有的行为，这里只是把它们接到浏览器上。

## 同源与授权边界

| 检查           | 在哪里     | 规则                                                                    |
| -------------- | ---------- | ----------------------------------------------------------------------- |
| 会话 Cookie    | 每个请求   | `tianshu_session`，HttpOnly，`SameSite=Strict`                          |
| CSRF           | 每个 POST  | `X-CSRF-Token` 必须与该会话当前值逐字节相同                             |
| Host 与 Origin | 每个请求   | 必须等于部署时配置的 `web.origin`（`local_rehearsal` 只允许 127.0.0.1） |
| 读后复核       | 每个读请求 | 上游返回后再核对会话对象与策略指纹，中途撤权/轮换即 401                 |
| 上游授权       | 每次读     | 对端用自己的凭据独立授权；本平台不代替它判断                            |

浏览器只提交**操作白名单加标识/查询**：不传 Bearer、凭据引用、端点 URL 或物理路径。连接由
服务器从 `asset_connections` 登记里解析，凭据只从登记的凭据环境变量取。把浏览器的管理员声明
当成上游授权是禁止的：`web_assets.principal` 是一个独立的服务身份，通常不是 operator。

## 页面状态与会话范围

会话里的“当前连接”是页面自己的状态，存在服务端会话对象里，默认 900 秒：

- `POST /api/web/assets/state` —— **只读**：有哪些允许的连接、当前选的是哪一个。请求体必须是
  `{}`。它永不改变范围，所以页面在每次读取前问它在哪都不会把自己问没。
- `POST /api/web/assets/connection` —— **改变**：`{"connection_id": "library-a"}` 选定一个
  `web_assets.allowed_connections` 里登记过的连接；`{}` 显式丢弃当前选择。

两者分开是有意的。选定连接与“我现在在哪”是两件事：如果读取状态本身会清掉范围，一个在每次
读取前自检的页面就会永远停在“请选择资产连接”，而对端其实一直有授权。

没有选定连接时的读取得 403 `connection_required`；这不是授权失败，是页面还没有范围。

## 端口

全部是 `POST`，全部同源，全部 `Cache-Control: no-store`：

| 端口                | 请求体                                                                                                              | 回答                          |
| ------------------- | ------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| `assets/state`      | `{}`                                                                                                                | 页面状态（见上）              |
| `assets/connection` | `{}` 或 `{"connection_id"}`                                                                                         | 改变后的页面状态              |
| `assets/libraries`  | `{}`、`{"page_size"}`、`{"cursor"}`                                                                                 | 库列表 + `page` + `preview`   |
| `assets/browse`     | `{"library_id", "parent_relative_path", "page_size", "cursor", "sort_by", "sort_direction", "kind", "name_filter"}` | 目录一页 + `library` + `page` |
| `assets/search`     | `{"query", "scope", "library_id"?, "parent_relative_path"?, "page_size", "cursor"}`                                 | 命中一页 + `page`             |
| `assets/entry`      | `{"library_id", "entry_id"}`                                                                                        | 一个条目的只读详情            |

每一页都是**整页替换**：范围、查询、库或连接一变，页面立刻清掉旧正文并中止在飞的读取，迟到
的回包不会回填。`page_size` 默认 50，上限 100；游标绑定产生它的连接、库、范围、查询与排序，
换一个范围再带旧游标会得到 409 `cursor_conflict`，而不是把上一份列表接在后面。

**三处列表都能续读**：搜索、目录、以及**授权库列表本身**。库列表是唯一能打开库的入口，所以
`assets/libraries` 的 `page.has_more` / `page.next_cursor` 必须能被页面接着读下去——否则超过一页的
授权库在网页上没有路径可达。`libraries.list` 的游标与目录列表的游标属于不同的列表，互相拿去续读
是 409 而不是“回答另一份列表”。平台自己也要求：**带续读标记的一页必须是完整一页**（`items` 数量
等于请求的 `page_size`），短页会被判为 `invalid_upstream`，避免把被截断的列表显示成完整的。

## 模块与依赖方向

| 模块                                     | 职责                                                                            |
| ---------------------------------------- | ------------------------------------------------------------------------------- |
| `services/platform/asset_page_config.py` | `web_assets` 段的部署期校验（谁能读、可暴露哪些已绑定连接）；中立，无传输无会话 |
| `services/platform/web_asset_queries.py` | 页面规则：白名单、边界、游标绑定、上游一致性、脱敏投影                          |
| `services/platform/web_assets.py`        | 同源适配层：会话范围、读后复核、取消、台账                                      |
| `services/platform/assets.py`            | 既有的五个只读操作（TS-064）；只导入 `asset_page_config`，**不导入网页模块**    |

## 错误与状态

| 情况                         | 页面看到的                                                   |
| ---------------------------- | ------------------------------------------------------------ |
| 未配置 `web_assets`          | 403 `assets_disabled`；旧部署保持原有行为与策略指纹          |
| 身份被撤权                   | 403 `operator_not_authorized`                                |
| 没有登记任何允许的连接       | 403 `no_asset_connections`                                   |
| 还没选连接                   | 403 `connection_required`（页面自己的提示，不是对端拒绝）    |
| 对端拒权 / 条目不可见        | 403 `forbidden` / 404 `not_found`，原样如实显示              |
| 对端连不上、超时、无正文 503 | 503 `dependency_unavailable`                                 |
| 对端给了不可用的 JSON        | 502 `invalid_upstream`                                       |
| 响应超过 1 MiB 预算          | 413 `budget_exceeded`                                        |
| 索引离线                     | **不是错误**：`availability: "offline"` 与真实索引行一起显示 |

“索引离线”“没有条目”“没有匹配”“读取失败”“还没有连接”“没有授权库”是六个不同的状态，各有各的
说法，不会折叠成一张空列表。原件可用性在协议里是 `not_verified`，页面就写“未验证”，不会显示
成可用。

现行只读协议没有预览或下载端口，所以页面只用类型图标并写明“暂不提供预览”，不生成假缩略图、
图库装饰或无效下载按钮。

## 设置

顶层可选设置 `web_assets`，缺省即没有这个页面（旧设置的策略指纹与行为不变）：

```json
{
  "web_assets": {
    "enabled": true,
    "principal": "assetreader",
    "allowed_connections": ["library-a"]
  }
}
```

- `principal` 必须是已登记的服务身份，且真的持有 `asset.read`；它读不到的身份在这里也不会
  被赋权。
- `allowed_connections` 只能**收窄**：列出的名字必须同时在该身份的 `asset_connections` 和顶层
  `asset_connections` 登记里存在。页面永远不会自动获得该身份的全部连接权限。
- 两个字段都经合同检查；写错在装配时就会失败，而不是等第一次读取。

## 验证

```powershell
# 后端：适配层、纯规则、取消、游标绑定、模块边界
$env:TS012_CONTRACT_DIR='C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
$env:TS013_TLS_PYTHON='C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:PYTHONDONTWRITEBYTECODE='1'
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p 'test_web_assets.py' -v

# 浏览器：真实登录 + 真实同源后台 + 隔离 TLS 合成对端（构建产物）
npm run build
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.assets.config.ts

# 浏览器：StrictMode 首挂载与再进入（开发服务器）
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.assets-dev.config.ts
```

两条浏览器套件都用 `tests/backend/run_asset_web_fixture.py` 起的隔离合成装置：合成对端说已发布
的 AssetLink 只读信封、走真实 TLS（4819），场景开关在 4818，可让它报告断线、拒权、索引离线或
超大响应。它**不是** AssetLibrary：真实资产服务的联合验证仍由 TS-064 的独立驱动完成。

StrictMode 套件走 Vite 的 5173，并在同一个装置里用第二个后台实例（4815，被配置的 origin 就是
5173）承接转发过来的 `/api/web/*`：主机与 Origin 复核照旧逐请求执行，只是这一实例被配置为相信
开发服务器的地址，规则没有被放宽。

没有真实 NAS、真实资产账号或生产部署参与这些验证。
