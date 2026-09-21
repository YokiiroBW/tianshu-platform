# 人格目录、历史与版本比较（TS-025）

角色与对话（`/#/companion`）的第三段「人格与世界」之前没有真实数据源。本任务在网页控制台接入
Core 已发布的**人格读取**能力：显示本部署显式授权的角色目录、四类历史（修订/发布/批准/回退）、
单个版本内容与两版字段比较。整页只读——不修改、不批准、不发布、不回退、不导入，也不新增任何
跨产品写入端口。

## 边界

| 项            | 说明                                                                                                  |
| ------------- | ----------------------------------------------------------------------------------------------------- |
| 新增网页端口  | 仅同源 `/api/web/personas/{catalog,history,revision,compare}`，属本产品网页控制台                     |
| 上游端口      | 沿用 Core 既有 `POST /internal/v1/persona/manage`，只用 `get`/`history_page`/`revision`/`compare`     |
| 浏览器输入    | 只有角色 id、历史类别、本页签发的不透明续读位置、两个版本 id；没有 operation、limit、reader、scope    |
| 浏览器输出    | 脱敏投影：状态、版本号、两类指针、四类条目字段、字段值；没有凭据、地址、CA 路径、本地路径或操作者身份 |
| 权限          | 控制台登录 + 显式 `persona.read` 动作 + 部署角色名单 + 连接自己的凭据，四层各自独立                   |
| 未配置/未启用 | 明确返回 `personas_not_configured`/`personas_disabled`，未授权返回 `persona_read_required`            |
| 读不到        | 上游超时、断连、回答不可用一律 5xx 明说；**读不到永远不等于空目录、空历史或空内容**                   |

## 合同与候选包

被消费的读取合同来自 `contracts/persona-management/candidate-v1`，它仍是**候选包**：

| 项              | 值                                                                 |
| --------------- | ------------------------------------------------------------------ |
| manifest SHA256 | `06a4e2c2be73952f9c369f4c1a540f7a6394a4f54b267e5487d23d64cd97e8fb` |
| 状态            | `candidate_not_published`；`production_publish_authorized: false`  |
| 允许的消费任务  | `TS-025`                                                           |
| 生产者提交      | `0a775631366f1b7c4bab94ac5b16e67aa15ee1ab`（Core `main` 上）       |

启动时逐项校验 manifest 哈希、状态、消费任务、生产者提交与操作清单，任一项不符即整段不可用；
另外只有 `mode = local_rehearsal` 且显式 `allow_candidate: true` 才会加载候选包，生产 TLS 部署
无论怎么配置都不会读它。候选包未发布意味着：**本页读的是候选合同，发布前不得作为生产入口**。

## 设置

`persona_connections` 与 `web_personas` 都是可选顶层设置，缺省即没有这一页。

```json
{
  "persona_connections": {
    "characters-local": {
      "base_url": "https://127.0.0.1:4823",
      "token_env": "TS025_PERSONA_ADMIN",
      "ca_file": "C:/local-rehearsal/companion-localhost.pem",
      "timeout_seconds": 10
    }
  },
  "web_personas": {
    "enabled": true,
    "connection_id": "characters-local",
    "allowed_subjects": ["actor:alpha", "actor:beta"],
    "candidate_directory": "C:/.../contracts/persona-management/candidate-v1",
    "allow_candidate": true
  }
}
```

| 字段                  | 要求                                                                          |
| --------------------- | ----------------------------------------------------------------------------- |
| `base_url`            | 必填，`https` 且不含账号密码或查询串；本机演练用 `https://127.0.0.1:<端口>`   |
| `token_env`           | 必填，`[A-Z][A-Z0-9_]*` 形式的环境变量名；不能与其它 principal 的凭据变量重名 |
| `ca_file`             | 可选，必须是绝对路径；只用于这一条连接                                        |
| `timeout_seconds`     | 可选，0 < 值 ≤ 10，缺省 10；这是单次读取的绝对时限，只收紧不放宽              |
| `enabled`             | 布尔，缺省 `false`；false 时状态为 `personas_disabled`                        |
| `connection_id`       | 必填，必须已登记在 `persona_connections`                                      |
| `allowed_subjects`    | 必填，1–64 个、不重复、满足 `common#id` 的**闭集**；页只读这份名单            |
| `candidate_directory` | 启用时必填，绝对路径                                                          |
| `allow_candidate`     | 启用时必须为 `true`，且 `mode = local_rehearsal`                              |

网页操作者即 `web.principal`，还必须拥有 `persona.read`；缺这一项是 403 `persona_read_required`，
不是「空目录」。凭据只在服务器侧解析：每次调用重新读一次环境变量，轮换后旧会话立即失效。

## 端口与固定形状

四个端点都只接受同源 Cookie `tianshu_session`、CSRF 头 `X-CSRF-Token` 与完全匹配的 `Origin`，
请求体 ≤ 16 KiB，每次 `await` 之后重新校验会话与读权限：

| 端点       | 请求体                                                    |
| ---------- | --------------------------------------------------------- |
| `catalog`  | `{"cursor": null}` 或上一页签发的 `next_cursor`           |
| `history`  | `{"subject": "...", "kind": "...", "cursor": null}`       |
| `revision` | `{"subject": "...", "revision_id": "<64 位摘要>"}`        |
| `compare`  | `{"subject": "...", "left": "<摘要>", "right": "<摘要>"}` |

- 目录每页固定 20 个角色，历史每页固定 20 条；两者都不接受浏览器指定的 `limit`。
- 续读位置是**本页签发**的不透明串（≤ 2048 字符），HMAC 绑定权限指纹、连接、角色集合摘要、
  页码与位置。伪造、截断或进程重启后的旧串一律 409 `cursor_conflict`，页面不会自动翻页。
- 上游 `next_cursor` 原样透传；上游因版本变化拒绝时是 409 `version_conflict`。
- 并发上限：浏览器侧同时最多 4 个业务请求（第 5 个 429 `too_many_requests`），对上游同时最多 4 个
  HTTP 请求；目录按每批最多 20 个角色、4 个 worker 读取。历史回答 ≤ 256 KiB，其余 ≤ 1 MiB。

错误码：`personas_not_configured` 503、`personas_disabled` 503、`persona_read_required` 403、
`dependency_unavailable` 503、`timeout` 503、`invalid_upstream` 502、`cursor_conflict` 409、
`version_conflict` 409、`not_found` 404、`invalid_input` 400、`too_many_requests` 429。

## 页面说出来的状态

页面不复用「空」表示异常，每一种情况都有自己的一句话：

| 状态                    | 触发                                                         |
| ----------------------- | ------------------------------------------------------------ |
| 未配置 / 未启用         | `personas_not_configured` / `personas_disabled`              |
| 未授权                  | `persona_read_required`                                      |
| 读取失败                | 超时、断连、上游回答不可用                                   |
| 空目录                  | 服务端**确实**返回 0 个角色（配置下限 1，属兜底）            |
| 无历史                  | 某一类历史确实 0 条                                          |
| 续读位置过期 / 版本过期 | `cursor_conflict` / `version_conflict`，附「重新打开第一页」 |
| 已选版本不可用          | `not_found`，不显示成空内容                                  |
| 登录已过期              | `session_expired`，清掉旧面板回到登录                        |

目录里读不到的角色仍占一行并写明原因；全部角色都读不到时整页报读取失败，而不是显示一个空目录。

## 真实链路与验证

真实联合验收用 Core 自己的固定提交（`0a775631366f1b7c4bab94ac5b16e67aa15ee1ab`）导出到本
worktree 的忽略运行时目录，经 uvicorn 在真实 loopback TLS 上服务，用真实 `persona_admin` Bearer
写入合成修订/批准/发布/回退，再由本页四个端点读取。`test_personas_joint.py` 里那份**部署角色未声明
版本**时 Core 存 `imported: null`、而候选 schema 要求整数——这是已记录的真实差异，作为一条明确的
`needs_coordinator` 事项保留，未在本地放宽。

```powershell
# 后端（最窄 + 真实联合）
$env:TS012_CONTRACT_DIR='C:\YOKI\Codex\tianshu-peiban-bot\contracts\text-dialogue\v1'
$env:TS025_CANDIDATE_DIR='C:\YOKI\Codex\tianshu-peiban-bot\contracts\persona-management\candidate-v1'
$env:TS013_TLS_PYTHON='C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:TS025_GIT='C:\YOKI\AzurLaneAutoScript\toolkit\Git\cmd\git.exe'
.\.runtime\venv\Scripts\python.exe -m unittest discover -s tests/backend -p "test_personas*.py" -v

# 网页（先构建）
npm run build
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.personas.config.ts
```

浏览器装置 `tests/backend/personas_fixture.py` 在一个进程里跑三个真实控制台（4820 可读、
4821 未配置、4822 未授权）与一个真实 TLS 合成角色服务（4823）；`TS013_TLS_PYTHON` 只用来生成
一次性证书，不装系统信任。合成装置不能替代真实联合验收，两者分别记录。

## 不在这里的东西

- 没有写入能力：不调用上游 `draft`/`approve`/`publish`/`rollback`/`retire`/`import`，也不新建
  `/internal/v1/*` 端口；`catalog`/`list` 上游操作不在本页消费范围。
- 不读 Core 的数据库、不读别的产品目录、不复制对端 ACL，也不把浏览器声称的身份当授权。
- 没有真实账号、付费模型、生产部署或系统信任证书；本地只有隔离合成数据与一次性证书。
