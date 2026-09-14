# 网页模型配置管理（TS-016）

TS-015 的原生模型配置只有本地 CLI 与内部快照端口。本任务在既有网页控制台的**设置 → 模型与用量**
页接入可操作的模型配置管理：显示 Chat（`text-dialogue/v1`）与原生（`model-protocol/v1`）各自
的版本、绑定与失效/撤销状态，并让显式授权的本机网页操作者预览、发布和撤销。配置权威仍然只有
一个：写入全部经 `Platform.Models`，本任务不新建配置表、不新增跨产品合同端口。

## 边界

| 项           | 说明                                                                                    |
| ------------ | --------------------------------------------------------------------------------------- |
| 新增网页端口 | 仅同源 `/api/web/models/*`，属本产品网页控制台；不新增 `/internal/v1/*`，不冒用其它合同 |
| 浏览器输入   | 只有模板标识、上次读到的版本号、幂等 client id；没有 URL、凭据、namespace 或密钥字段    |
| 浏览器输出   | 脱敏投影：版本、有效期、availability、provider/model/协议/能力与绑定；无端点与凭据引用  |
| 写权限       | 默认关闭；开启后仍需普通登录之外的显式解锁；普通聊天登录不获得任何管理权限              |
| 未配置       | 明确返回 `management_disabled` / `web_not_configured`，绝不显示成成功                   |

## 设置

`web_models` 是可选顶层设置；缺省即关闭，网页只读版本、不能发布或撤销。模板是服务器登记并
经审查的配置形状，浏览器只引用其 `template_id`。

```json
{
  "web_models": {
    "enabled": true,
    "unlock_ttl_seconds": 900,
    "templates": [
      {
        "template_id": "chat-local-text",
        "label": "本地演练 · Chat 文本",
        "target": "chat",
        "provider_id": "provider-fixture",
        "model_id": "fixture-text-model",
        "verified_capabilities": ["text"],
        "lifetime_seconds": 300,
        "timeout_ms": 30000
      },
      {
        "template_id": "native-local-text",
        "label": "本地演练 · 原生 Responses",
        "target": "native",
        "provider_id": "provider-native",
        "verified_capabilities": ["text", "stream"],
        "lifetime_seconds": 300
      }
    ]
  }
}
```

| 字段                    | 要求                                                                                    |
| ----------------------- | --------------------------------------------------------------------------------------- |
| `enabled`               | 布尔，缺省 `false`；false 时端口存在但所有写操作 403 `management_disabled`              |
| `unlock_ttl_seconds`    | 整数 60–3600，缺省 900；解锁只作用于当前会话                                            |
| `templates`             | 1–32 项，`template_id` 唯一且满足 `common#id`                                           |
| `target`                | `chat` 或 `native`；决定协议钉、workload、版本键与持久表                                |
| `provider_id`           | 必须存在于 `providers` 登记，且协议钉与 target 一致（native 需显式 `openai-responses`） |
| `model_id`              | 可选，缺省取登记的第一个模型；必须在该登记的 `model_ids` 内                             |
| `verified_capabilities` | 登记能力的子集，不重复                                                                  |
| `lifetime_seconds`      | 整数 1–`config_max_lifetime_seconds`（缺省 3600），缺省 300                             |
| `timeout_ms`            | 整数 1–120000，缺省 30000                                                               |

启动时逐项校验；未知字段、未知提供方、协议不匹配、越权能力、重复模板 id 或超出最大发布时长都会
让服务明确失败，不会退化运行。`web_models` 只随网页控制台存在：没有 `web` 配置时整个网页入口
仍按原语义 503 `web_not_configured`，也不会生成任何管理运行库。

网页操作者身份即 `web.principal`（必须是 platform/operator 且 `account.namespace=web`）。该
principal 还必须拥有 `config.publish`、`config.revoke`、`config.view`，否则状态为
`operator_not_authorized`。

## 端口

全部为同源 POST，复用真实 Cookie 会话、精确 Origin、CSRF 头、JSON 正文与 16 KiB 正文预算；
浏览器请求不得带 `Authorization`，内部 RPC 也不接受 Cookie/Origin。

| 路径                      | 输入                                             | 说明                                 |
| ------------------------- | ------------------------------------------------ | ------------------------------------ |
| `/api/web/models/view`    | `{}`                                             | 状态与脱敏投影（读操作，仅需已登录） |
| `/api/web/models/unlock`  | `{"password"}`                                   | 显式授权：再次校验管理员密码         |
| `/api/web/models/lock`    | `{}`                                             | 收起管理授权，聊天登录不受影响       |
| `/api/web/models/preview` | `{"template_id"}`                                | 预览将要发布的形状与版本号           |
| `/api/web/models/publish` | `{"template_id","expected_version","client_id"}` | 发布并落库（经 `Models` 权威逻辑）   |
| `/api/web/models/revoke`  | `{"target","version"}`                           | 撤销（经 `Models` 权威逻辑）         |

`view` 每次返回三态之一：`management_disabled`、`operator_not_authorized`、
`management_required`（已开启且账号有权限但当前会话未解锁）或 `ready`。写操作在非 `ready` 时
直接 403 并带同一状态码，不会先写后报。

### 解锁与失效

`unlock` 与登录共用同一个 scrypt 校验器、同一把锁和同一份 5 次/60 秒失败预算；成功后在**当前
会话**上打一个到 `unlock_ttl_seconds` 为止的管理标记。会话绝对期限、退出登录、principal 撤销、
服务凭据轮换与进程重启都会让管理授权一起失效，之后写操作返回 403 或 401，读取仍显示真实状态。
权限消息不会因为刷新而续期。

## 发布与撤销语义

- 发布文档完全由服务器组装：provider 的 `base_url`、`credential_ref`、`credential_namespace`、
  `capability_verification`、`model_id` 与 `verified_capabilities` 取自登记，协议/workload/
  `model_policy`/`reasoning_policy`（原生另有 `state_references=reject`）取自 target 规定，
  有效期窗口由服务器时钟决定。
- 版本号永远是「当前最高版本 + 1」；`expected_version` 必须是浏览器上次读到的当前版本，否则
  409 `version_conflict`。两个浏览器同读一版、其中一个先发布时，另一个必然冲突而不会覆盖，
  冲突后网页自动重新读取并清空过期预览。
- 有效期窗口锚定在服务请求所在的整秒：合同的时间戳编码会四舍五入到微秒，直接用原始读数可能
  比写事务自己的时钟检查晚约 0.5 微秒而被判非法；整秒可以精确往返，窗口长度恒等于
  `lifetime_seconds`。
- 重放：`client_id` 是一次发布意图的幂等键。同一 `client_id` 与同一语义重复提交返回首次结果
  （`state="replayed"`）且不新增版本；同一 `client_id` 换成别的模板/目标返回 409
  `idempotency_conflict`。
- 撤销调用既有 `models.revoke` / `models.native_revoke`：撤销后 Chat 快照返回 410、原生快照
  返回 403，撤销版本不能被重新发布，也不能被读取；未知版本 404。
- Chat 与原生各自独立的表、版本序列与撤销键空间在这里同样成立：网页发布 Chat 不会影响原生
  版本，反之亦然。

### 中断恢复协议

权威配置库与网页运行库是两个数据库，两次写入之间没有共同事务。因此发布不假设回执必然写成，
而是**先写持久意图、再写权威库、最后对账**：

1. `publish` 先在 `publication_intents` 里落一条 `prepared` 意图：`client_id`、语义摘要、目标、
   版本号、`expected_version`、该版本文档的**摘要**与整秒窗口。这一步在权威库被触碰之前完成。
2. 权威写入仍由 `Models` 完成（校验 + 单一事务），本模块不复制该逻辑。
3. 每个回答——首次、重试、重启后重试——都先读权威表：当意图的版本存在且**存的摘要等于意图摘要**
   时，就认定这次意图所指的文档确实已在该版本发布，返回同一结果（重试显示 `replayed`）并把意图
   落为 `committed`；没有任何一行时报错只反映真实状态。

由此得到的行为：

| 情况                               | 结果                                                          |
| ---------------------------------- | ------------------------------------------------------------- |
| 权威提交成功，回执写失败或进程中断 | 重试恢复为同一版本、同一结果；不会 409，也不会新增版本        |
| 权威提交**之前**中断               | 请求失败且权威库确实没有新版本；同 `client_id` 重试只发布一次 |
| 回执写失败（I/O）                  | 回执只是优化：本次回答依据权威库已确认的结果，不报假失败      |
| 权威行缺失，但回执存在             | 503 `publication_unverified`：既不报成功也不报普通冲突        |
| 同 `client_id`、不同内容           | 409 `idempotency_conflict`，权威库不变                        |
| 目标版本已被**别的**文档占用       | 409 `version_conflict`，不覆盖、不新增版本                    |
| 意图窗口已过期且尚未发布           | 409 `version_conflict`：窗口不可改写，需重新预览              |

意图行不含端点或凭据引用，也不能被任何消费者当作配置读取，因此不构成第二份配置权威。

## 本地存储

除权威库与 TS-014 的两个 sidecar 之外，新增忽略的运行产物
`<database_path>.web-models.sqlite`，只有一张意图/对账表：

```
publication_intents(client_id PRIMARY KEY, semantic, target, version, expected_version,
                    digest, published_at, usable_until, state, result,
                    prepared_at, settled_at)
```

`state` 只有 `prepared`（已认领、结果未知）与 `committed`（已持回执）两种；`digest` 是该
`client_id` 所指文档（不含关联用的 `request_id`）的稳定摘要，用于与权威表逐字节对账。表里
没有任何配置内容（没有端点、凭据引用或密钥）。删除它只会让重放与恢复不再被识别：权威库里
已提交的配置不受影响。权威库 schema 仍是 2，本任务不改迁移主线。

## 实际验证

```powershell
# 最窄（本任务新增）
$env:TS012_CONTRACT_DIR = "<已发布 contracts>/text-dialogue/v1"
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p "test_web_models.py" -v

# 受影响后端回归
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -v

# 真实合成后台的浏览器路径（先构建）
npm run build
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.web.config.ts
```

浏览器套件用 4814 端口的隔离合成 Platform：同一套用例里真实登录、真实解锁、真实发布/撤销、
两个浏览器上下文的旧版本冲突、同 client id 重放、清 Cookie 后的会话失效，以及只有状态替身的
禁用/无权/过期面板状态。合成后台没有任何真实模型账号或供应商调用。

中断恢复另有自检脚本（合成 fixture，全部断言失败即非零退出）：

```powershell
.runtime/venv/Scripts/python.exe .runtime/dsh-delivery/ts016-recovery-probe.py
```

它覆盖：两个目标各自的「已提交但回执丢失」恢复、提交后进程重启恢复、提交前中断只发布一次、
异内容冲突、版本被别的文档占用、以及回执存在但权威行缺失时的 `publication_unverified`。

## 未验证与下一步

- 没有任何真实模型账号、供应商调用、付费模型、真实设备或生产部署；全部证据为合成 fixture 与
  本机 loopback HTTP。
- 网关消费原生配置、`route_context`、原生 SSE/回执仍由其任务与协调联合验收，本任务不实现、
  不猜测其形状，也不改其工作树。
- 未做：50 万级版本列表分页、生产 PostgreSQL、真实多用户并发压测、管理操作的双人复核。
- 未做：用真实断电/进程终止以外的故障注入验证 SQLite 层面的写丢失（当前用回执写失败、回执写
  被跳过与重启三类合成注入覆盖）。
- 根 manifest 的 `runtime_routes_enabled=false` 与 `runtime_disabled_until_joint_acceptance`
  仍然有效：本地开启 `web_models` 只是隔离演练，不等于合同开放。
