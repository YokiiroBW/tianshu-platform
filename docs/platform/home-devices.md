# 家庭设备状态与受限控制（TS-017）

平台通过 Home Assistant 的官方 REST 接口读取已登记实体并执行受限开关动作。仅使用
[HA REST API](https://developers.home-assistant.io/docs/api/rest/) 的状态读取与服务调用，
**不使用** `POST /api/states` 冒充控制：该接口只改状态表示，不与真实设备通信。

本轮没有连接任何真实 HA 实例、账号或设备。全部证据来自本机 loopback 上的隔离合成 HA
（`tests/backend/home_fixtures.py`），真实设备、局域网地址与生产部署另验。

## 归属与边界

| 负责方                                                       | 职责                                                             |
| ------------------------------------------------------------ | ---------------------------------------------------------------- |
| 平台连接器（`services/platform/home.py`）                    | 登记连接、按实体读取状态、校验目标、调用服务动作、保留不确定结果 |
| 网页“家庭与服务 → 家庭设备”（`apps/web/src/features/home/`） | 展示读数、可用性与采样时间；提交服务器登记的动作模板             |
| Home Assistant                                               | 品牌集成、实体状态、服务动作、场景与自动化                       |
| 设备/网关/厂商服务                                           | 实际硬件执行与回报                                               |

- 服务器登记 HA 地址、凭据环境变量、实体清单与动作模板；浏览器只送 `template_id`、
  已读到的读数修订号 `expected_revision` 和幂等 `client_id`。浏览器不能指定实体、服务、
  域名、地址或凭据。
- 只支持 `light`/`switch` 的显式目标状态动作 `turn_on`/`turn_off`，以及只读 `sensor`。
  没有门锁、门、报警、摄像头、场景、脚本或任意服务数据。
- 角色小屋的虚构设备与本连接器没有自动映射；本轮也没有建立任何映射。
- 只读网页端口为同源 `/api/web/home/*`，复用真实 Cookie/CSRF/Origin；不新增
  `/internal/v1/*` 合同端口。

## 设置

可选顶层设置 `home`（缺省即完全没有入口）。完整样例见
[web-console.settings.example.json](web-console.settings.example.json)。

```json
"home": {
  "enabled": false,
  "base_url": "http://127.0.0.1:8123",
  "reviewed_addresses": ["127.0.0.1"],
  "allow_private_http": true,
  "token_env": "TS017_HA_TOKEN",
  "unlock_ttl_seconds": 900,
  "timeout_seconds": 4,
  "status_max_age_seconds": 120,
  "entities": [
    { "entity_id": "light.study", "label": "书房灯", "kind": "light" }
  ],
  "templates": [
    {
      "template_id": "study-light-on",
      "label": "打开书房灯",
      "entity_id": "light.study",
      "service": "turn_on"
    }
  ]
}
```

| 键                       | 规则                                                                                                                                                                                  |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`                | 缺省 `false`：只读可用，控制端口对任何请求都不存在（`control_disabled`）                                                                                                              |
| `base_url`               | 只允许主机（无路径/查询/片段/内嵌凭据）；与 `providers` 相同的已审查地址策略：明文 http 要求 `allow_private_http: true` 且地址全为 loopback，字面 IP 必须与 `reviewed_addresses` 一致 |
| `token_env`              | 只写环境变量名；HA 长期令牌取自进程环境，从不写入设置、响应或日志                                                                                                                     |
| `unlock_ttl_seconds`     | 60–3600，控制解锁的有效期                                                                                                                                                             |
| `timeout_seconds`        | 1–15，单次 HA 请求总时限                                                                                                                                                              |
| `status_max_age_seconds` | 10–3600，读数到期阈值                                                                                                                                                                 |
| `entities`               | 1–32 项；`entity_id` 必须匹配 `(light\|switch\|sensor).<小写>` 且与 `kind` 同域                                                                                                       |
| `templates`              | 0–32 项；`service` 只能是 `turn_on`/`turn_off`，目标必须是可控制实体，不接受 `data` 等额外字段                                                                                        |

授权：控制端口要求操作者 principal 含 `device.control`，且当前网页会话显式解锁。普通登录
不获得控制权；解锁设备控制与解锁模型管理是两套独立状态，互不授予。

## 读数语义

`POST /api/web/home/view` 只返回本地投影（不联系 HA）；`POST /api/web/home/refresh` 才执行
真实观测：对每个登记实体做一次有界 `GET /api/states/<entity_id>`。

每个实体返回 `availability`、`code`、`state`、`value`、`unit`、`observed_at`（采样时间）、
`attempt_at`（最近一次尝试时间）与 `revision`（读数修订号）。

| `availability` | 含义                                                                                      |
| -------------- | ----------------------------------------------------------------------------------------- |
| `current`      | 最近一次成功观测在有效期内                                                                |
| `stale`        | 有成功观测但已超过 `status_max_age_seconds`；保留原值与采样时间                           |
| `offline`      | 本次读取失败（超时、拒连、重定向、载荷不可读等）；`code` 记录真实原因，旧值仍带原采样时间 |
| `unavailable`  | HA 报告该实体当前不可用                                                                   |
| `unknown`      | HA 状态字符串为 `unknown`，或状态无法解释（`device_malformed_state`）                     |
| `never_read`   | 还没有任何观测记录                                                                        |

HA 返回 `unavailable`/`unknown` 或无法解释的状态时 `state`/`value` 为 `null`，网页显示
“状态不可用”“尚未读取”或“上次读数：…”，**绝不**换算成关闭或零。每个实体的读数修订号只在
读数真的变化时前进；失败的尝试不前进。读数持久在 `<database_path>.home-controls.sqlite`，
重启后仍是上次读数（按时间标记为 `stale`），不会退回“未知”。

## 控制语义

`POST /api/web/home/control` 只接受 `{template_id, expected_revision, client_id}`。
实体、域与服务全部由服务器从登记表解析；`expected_revision` 是本浏览器实际读到的修订号，
不匹配即 409 `state_conflict`（两个浏览器不会静默覆盖）。

受理与观测是两件不同的事实：

- **受理**（`acceptance`）：`POST /api/services/<domain>/<service>` 的回执。
  `accepted` 只表示 HA 受理，`target_reported` 只表示回执里出现了目标状态。
- **观测**（`observation`）：受理之后一次新的 `GET /api/states`。`confirmed` 才表示读到了
  目标状态；`contradicted` 表示后续观测到别的状态（HA 说改了、设备没动）；`pending` 表示
  还没有后续观测；`unknown`/`unreadable`/`unavailable`/`not_sent` 各自如实表达。

持久幂等与恢复（`control_intents` 表，按 `client_id` 唯一）：

1. 先落 `prepared` 意图（含语义摘要、目标、服务、`expected_revision`），**然后**才联系 HA。
2. 已有结果的同一 `client_id` 直接重放该结果，不再发第二次服务调用；同键不同语义 409
   `idempotency_conflict`。
3. `unknown`（超时、连接中断、回执不可读、HA 5xx）会**保留**且**绝不自动重发**；重放同样返回
   该未知结果，操作者必须先重新读取设备状态，再用一次新的显式点击（新的 `client_id`）决定是否再发。
4. `prepared`（进程在回执前中断、回执写失败）重试时先按观测对账：目标状态已经读到就记为
   `observed`（`recovered_at_target`，无回执但目标已成立），否则才发出一次目标状态动作
   （`turn_on`/`turn_off` 是幂等的目标状态动作，重复请求收敛而不是取反）。
5. 回执写失败不会把已受理的指令报成失败：回答里 `durable` 为 `false`，意图仍是 `prepared`，
   下一次同键请求按第 4 条恢复，不会重复下发。

取消只停止当前页面等待（浏览器 abort），不会声称指令未发出或已回滚；服务端仍会完成该请求并
把结果写进意图表，操作者重新读取后即可看到真实结果。

## 错误语义

| code                        | HTTP | 含义                                      |
| --------------------------- | ---- | ----------------------------------------- |
| `home_disabled`             | 403  | 没有 `home` 设置段，没有任何入口          |
| `control_disabled`          | 403  | `enabled: false`，只读                    |
| `operator_not_authorized`   | 403  | 操作者没有 `device.control`               |
| `control_required`          | 403  | 会话未解锁设备控制                        |
| `state_conflict`            | 409  | 浏览器持有的读数修订号已过期              |
| `idempotency_conflict`      | 409  | 同一 `client_id` 被用于不同意图           |
| `device_credential_missing` | 503  | 环境变量里没有可用令牌                    |
| `device_timeout`            | 504  | HA 未在时限内响应（结果未知）             |
| `device_unavailable`        | 503  | 无法连接 HA（结果未知，方向保守）         |
| `device_unauthorized`       | 502  | HA 拒绝凭据（未送达）                     |
| `device_redirect`           | 502  | HA 返回重定向，连接器拒绝跟随（未送达）   |
| `device_rejected`           | 502  | HA 以 4xx 拒绝服务调用（未送达）          |
| `device_failed`             | 502  | HA 5xx（结果未知）                        |
| `device_invalid_response`   | 502  | 响应非 JSON、超限或实体不匹配（结果未知） |
| `receipt_unreadable`        | 502  | 200 回执正文无法读取（结果未知）          |
| `device_missing`            | 502  | 登记的实体在 HA 中不存在                  |
| `not_found`                 | 404  | 未登记的动作模板                          |

## 请求与凭据边界

- 浏览器请求体沿用控制台上限（JSON、≤16 KiB）、Origin/CSRF/Host/同源检查。
- 发往 HA 的每个请求都有总时限（`timeout_seconds`），响应上限 1 MiB；超过即
  `device_invalid_response`，不截断成假读数。
- 重定向一律拒绝（`allow_redirects=False`），不会跟随到另一个地址；目标只会是
  `/api/states/<已登记实体>` 与 `/api/services/<实体域>/<已登记服务>`。
- 令牌只从环境变量读取；响应与投影里没有 HA 地址、令牌、`Authorization` 头或环境变量名。
- 平台与日志都不写凭据；错误只返回稳定的 code，不返回对端原文。

## 实际验证入口

设置 `TS012_CONTRACT_DIR` 为正式 text-dialogue/v1 绝对路径：

```powershell
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p 'test_home.py' -v
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.web.config.ts
```

后端套件在同进程启动真实 loopback 合成 HA（读、服务调用、重定向、401、500、不可读/超限正文、
不生效的 200 回执、挂起与释放），并使用一个真实无人监听的 loopback 端口验证离线与恢复。
浏览器套件启动 4814 端口的隔离合成 Platform 与 4817 端口的合成 HA；`synthetic UI state fixture`
用例明确使用状态替身，只验证页面状态，不代表产品联合通过。

## 未完成与风险

- 没有真实 HA 实例、真实设备、局域网地址、生产凭据或部署；HA 的实体能力差异（亮度、色温、
  窗帘位置等参数动作）、WebSocket 状态订阅、场景调用都未在本轮实现。
- 控制权限是单机单管理员模型：没有逐设备审批、没有操作审计查看页、没有并发压测。
- 意图表 `<database_path>.home-controls.sqlite` 存储的是意图、结果码与读数，不含地址与凭据；
  它与权威库必须一起备份，否则会丢失幂等与恢复能力并出现重复下发风险。
- 本轮不处理 HA 侧的乐观状态与自动化竞争；同一设备被 HA 自动化同时改变时，只按观测如实显示。
