# TS-012 平台后端运行

本切片是可运行的本地 Python 平台服务，消费主协调仓库 `contracts/text-dialogue/v1` 的发布版 1.0.0（`102d347`）。启动逐一核对 manifest 和五份 schema 的 LF-normalized SHA-256；manifest 必须为 `81e6cc4ddef7c6f82e055d4cb04b090db036dd5c52763473ce697aa02db478a1`。不复制旧 candidate、不改公共 schema、不从网络加载 schema。

平台仅监听 `127.0.0.1`。设置必须显式为 `mode: local_rehearsal`、`storage: sqlite_local`；其他模式拒绝启动。SQLite/WAL 持久保存来源、撤销、关联回填、发布版本和投影，是隔离演练存储；没有生产 PostgreSQL 适配、迁移或生产启动默认值。认证过的本地 CLI 操作、真实网页登录、真实 QQ/TG 入站是三种不同证据，本次只有第一种。

## 安装与检查

Python 3.12+。后端独立使用 `pyproject.toml` 与固定解析结果 `requirements-dev.txt`，不修改 npm 清单和前端命令。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
.venv/Scripts/python.exe -m pip install --no-deps -e .
.venv/Scripts/python.exe -m ruff check services/platform tests/backend
.venv/Scripts/python.exe -m ruff format --check services/platform tests/backend
```

本机安装也可使用 `uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt` 和 `uv pip install --python .venv/Scripts/python.exe --no-deps -e .`。锁定结果由 `uv pip compile pyproject.toml --extra dev -o requirements-dev.txt` 生成；普通安装不重新解析。无系统 Python 时使用本机 Codex 捆绑 Python 创建 venv，后续仍在该 venv 运行。

测试的合同位置必须明确指定。真实网关子链须另指定已集成网关的 `src`；不指定时该项明确跳过，其他后端测试继续执行。

```powershell
$env:TS012_CONTRACT_DIR = 'C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
$env:TS012_GATEWAY_SRC = 'C:/YOKI/Codex/tianshu-peiban-bot/projects/tianshu-model-gateway/src'
.venv/Scripts/python.exe -m unittest discover -s tests/backend -v
```

测试全用临时数据库、合成账号与环境凭据。HTTP 测试绑定临时 loopback 端口并关闭监听，包含真实 CLI 子进程启动/停止。网关子链直接调用已集成 `tianshu_gateway.server.create_app` 和真实 `HttpConfigSource`；平台是真实服务和真实 SQLite，只有模型上游是录制服务器。它不是完整 L0、L1、真实模型或生产验收。固定身份配置只是隔离夹具；来源引用全部通过平台真正认证、随机签发和落库逻辑产生，没有把 `trusted_context` 字典替身算作 issuer 验证。

## 部署输入与本地启动

把部署文件保存在忽略的 `.runtime/`，保护该目录的本机账户访问权限。`principals` 是操作方持有的服务身份登记，凭据只通过唯一 `token_env` 获取，不能从请求 payload 指定 principal/service。凭据至少 24 个可打印非空白 ASCII 字符，建议用密码学随机值；未设置、重复、已撤销都拒绝认证。不要把示例字符串当作凭据，不把凭据写入 Git、命令行参数或日志。此环境适配器不声称具备生产密钥管理、加密备份或自动轮换传播。

最小配置消费入口的完整设置形状如下。路径、提供商及目标由本地操作者登记。示例 `.invalid` 地址没有实际接通，不会因登记而被判定为模型能力已验证。

```json
{
  "mode": "local_rehearsal",
  "storage": "sqlite_local",
  "contract_directory": "C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1",
  "database_path": "C:/deployment/local-rehearsal/platform.sqlite",
  "config_max_lifetime_seconds": 3600,
  "principals": {
    "local-operator": {
      "kind": "operator",
      "service": "platform",
      "token_env": "TIANSHU_LOCAL_OPERATOR_TOKEN",
      "account": {
        "namespace": "web",
        "immutable_account_id": "local-operator-account"
      },
      "actions": [
        "origin.issue",
        "origin.revoke",
        "entry.revoke",
        "principal.revoke",
        "config.publish",
        "config.revoke",
        "config.view",
        "capability.read",
        "task.read"
      ],
      "task_owners": ["companion"]
    },
    "gateway": {
      "kind": "service",
      "service": "gateway",
      "token_env": "TIANSHU_GATEWAY_PLATFORM_TOKEN",
      "actions": ["config.snapshot"],
      "config_versions": [7]
    }
  },
  "entries": {
    "local-config-session": {
      "owner": "local-operator",
      "kind": "local_operator",
      "account": {
        "namespace": "web",
        "immutable_account_id": "local-operator-account"
      },
      "channel": {
        "namespace": "web",
        "binding_id": "local-console",
        "channel_conversation_id": "configuration",
        "thread_id": null
      },
      "actor_id": "local-config-context",
      "audience": "self_private",
      "ttl_seconds": 300,
      "routes": [
        {
          "caller": "gateway",
          "receiver": "platform",
          "purpose": "config.snapshot"
        }
      ]
    }
  },
  "providers": {
    "provider-local": {
      "base_url": "https://model.example.invalid/v1",
      "credential_ref": "secret-ref:provider/local",
      "credential_namespace": "local-model-account",
      "model_ids": ["configured-model"],
      "capability_verification": "fixture_only",
      "verified_capabilities": ["text"],
      "reviewed_addresses": ["192.0.2.10"],
      "allow_private_http": false
    }
  }
}
```

本地管理凭据表示一个实际认证的本机操作者，`account` 必须与登记的 web 账号一致；这里没有登录网页或可用浏览器会话。服务凭据签发/分发由部署环境负责，平台按登记和当前环境值认证；CLI 不接受自称管理员的字段。

```powershell
.venv/Scripts/python.exe -m services.platform --settings .runtime/settings.json serve --port 8765
```

进程在当前终端运行，Ctrl+C 停止，不自动开启窗口、连接设备或部署。未经同源会话适配的浏览器请求（Origin/Cookie）会被拒绝；不给浏览器开放 CORS。请求正文上限 1 MiB、读取时限 5 秒，访问日志关闭，错误只返回合同错误码和有效关联 ID。

## 来源与内部接点

公共 HTTP 只有两项：

| 路径                                      | 输入 / 输出                                                        | 授权                                          |
| ----------------------------------------- | ------------------------------------------------------------------ | --------------------------------------------- |
| `POST /internal/v1/origins/resolve`       | `common#origin_resolve_request` / `common#origin_resolve_response` | 固定 resolver 凭据 + 已签发来源路径           |
| `POST /internal/v1/model-config/snapshot` | `model#config_request` / `model#config_response`                   | 独立 config.snapshot 凭据 + 来源 + 版本白名单 |

签发输入文件只有 `{"entry_id":"local-config-session"}`：

```powershell
.venv/Scripts/python.exe -m services.platform --settings .runtime/settings.json local --credential-env TIANSHU_LOCAL_OPERATOR_TOKEN issue --input .runtime/issue.json
```

输出随机 `assertion_ref`、`expires_at` 和 `mode: local_rehearsal`。引用保存在 SQLite，服务器实时检查到期、引用/入口/主体撤销及入口配置摘要。不要把该输出给浏览器。操作者把引用放进网关设置指定的 `platform_origin_env` 对应环境变量；网关授权 token 放进其 `platform_credential_ref` 对应环境变量。网关仍拥有自己的 provider/IP 白名单与上游密钥环境引用。

对话演练入口使用 `kind: rehearsal_connector`，固定 `owner/account/channel/actor_id/audience` 和显式路线，不能接受任意 payload 提供的账号/角色/群范围。所有本切片来源 `issuer=platform`，模拟 QQ 账号不表示已经运行真实 NoneBot issuer。真实 QQ/TG/网页登录入口需先实现其认证接入，再协调新协议，不能把演练配置改名后当真实认证。

多段代理同用一个引用时分别登记 `{caller:nonebot, receiver:companion, purpose:dialogue}`、`{caller:companion, receiver:memory, purpose:dialogue}`。对应 resolver 服务身份配置例如 `service:memory`、`actions:[origin.resolve]`、`resolver:{caller:companion,purpose:dialogue}`。结果 `authenticated_service=companion`、`audience_service=memory`；两者均不直接等于 issuer。没有登记的反向/直达路径拒绝。

1.0.0 的 resolve 请求只有 ref，不携带具体操作，响应也没有 purpose。因此 issuer 只能检查固定凭据对应的 `dialogue` 授权域；register/select 的具体操作由接收方自己的 operations、账号和 scope 校验。平台不声称能识别这两个操作，不允许 dialogue 读取模型配置。精确逐操作用途需未来合同或接收方分离 resolver 凭据。

Python 内部端口（没有额外 HTTP 路径）提供如下实际接点，CLI 对应动作只供受信本地演练：

| 接点                                                                   | 行为与边界                                                                                                                                                             |
| ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `origins.issue(header, entry_id)`                                      | 认证已登记入口持有者，随机签发并落库；不接受 identity/scope payload                                                                                                    |
| `origins.prepare_mapping(header, kind, request)`                       | kind 为 identity/identity_resolve/ingest，验证发布的 register/resolve/ingest 请求、原账号/channel/actor、来源与 deadline；保存随机 ticket 及最少关联字段，不留消息正文 |
| `origins.confirm_mapping(header, ticket, response)`                    | 仅受信 memory/companion 响应适配器调用；检查响应类型、关联 ID、账号/channel、版本和主体一致性，原子回填并幂等保存响应                                                  |
| `origins.revoke(header, kind, id)`                                     | 本地操作者撤销 origin/entry/principal 并审计；撤销入口用于停止后续来源和迟到事件，单个短期 ref 的失效不等价于数据范围撤销                                              |
| `origins.observe_source(header, entry_id, message_key, tombstone=...)` | 受信演练入口登记当前修订/撤回；不签发归档回执                                                                                                                          |
| `origins.verify_current_sources(header, event)`                        | 认证 companion 发布者，核验当前入口/账号/角色/受众/会话、消息修订及墓碑，不依赖过期用户 ref；未知来源或未接归档权威明确失败                                            |

首次 resolve 中 person/conversation 均可 null。memory 是人物 ID 权威，companion 是会话 ID 权威；任一回填次序均可，核心回执中的 person 仅用于交叉核对，不提前生成 memory 绑定。后续签发和解析读取明确映射。已有 ID 冲突不被覆盖，账号重绑、会话迁移另需明确流程。memory.select 的 conversation null 仍由已实现 TS-030 返回 dependency_unavailable/503，不得从 select payload 补授权。

mapping.confirm 要求调用者确为受信上游服务/响应端口，不能把外部 JSON 转交给该端口冒充可信响应。持有对应服务凭据的本地演练命令只证明认证与关联逻辑，不证明真实 connector 已收到核心 HTTP 回执。

后台来源端口只返回 `current_sources` 和明确为 false 的 `archive_verified/scope_version_verified`。它不验证核心 turn/input revision、记忆 scope_version、来源正文/归档回执；记忆消费者继续承担这些约束。此处拒绝未经归档权威确认的 archived 来源；pending 保持 pending。不得把这个部分核验包装成完整 `SourceAuthority` 或让 Memory 直接读平台数据库。正式适配器及撤销传播仍待发布和跨产品验收；迟到事件不复用过期用户引用。

CLI 对应的 prepare-mapping、confirm-mapping、observe-source、verify-current-sources 输入分别为 `{kind,request}`、`{ticket,response}`、`{entry_id,message_key,tombstone}` 和已发布 committed_event。凭据仍从 `--credential-env` 获取并按真实服务身份限制权限。

## 版本化模型配置

发布输入为严格的 `model#config_response`，包含已有 schema 的完整 providers/bindings、版本与有效期。CLI action 为 `publish`。请求里的 request_id 只关联，不参与不可变内容摘要；principal、permission 等附加字段均不合法。平台授权操作者后，在同一写事务内检查 schema、唯一 provider/workload、已登记 provider、模型对应关系、目标/凭据命名空间与能力登记、非空合法有效期，之后才发布。默认最大发布有效期 1 小时，可显式设置至最多 24 小时。不能发布未来发布时间、已到期或反向有效期。

本地 HTTP 上游只准显式登记的 loopback 地址；HTTPS 登记也不等于完成了生产网络审查。URL 不含用户名、密钥、query、fragment 或路径回退。provider credential_ref 只引用网关服务端凭据存储；平台不从浏览器接收密钥。发布方登记、gateway 的固定 URL/IP 与 secret allowlist 必须一致。能力 `fixture_only` 和 `verified_test_account` 按实际登记精确匹配，不自动提升。

新版本严格递增；同版同内容幂等，同版不同内容或迟到新旧版本返回 409。发布新版不会改变仍有效的旧版快照；请求指定版本时绝不回退 latest。null 是合同明确的最新版本查询，最新版本无权/撤销/到期时拒绝，不悄悄找旧版。未知版本 404，无配置最新查询 503，撤销/到期 410，来源/用途/版本授权错误 403。发布和撤销记录持久保存，重启不复活撤销版本。网关已开始请求固定原版；平台离线期间的缓存有效期与撤销上界仍由 gateway 的 usable_until 规则约束，没有实时推送撤销保证。

```powershell
.venv/Scripts/python.exe -m services.platform --settings .runtime/settings.json local --credential-env TIANSHU_LOCAL_OPERATOR_TOKEN publish --input .runtime/publication.json
.venv/Scripts/python.exe -m services.platform --settings .runtime/settings.json local --credential-env TIANSHU_LOCAL_OPERATOR_TOKEN view-config
```

view-config 仅给本地已认证操作者脱敏投影：provider/model、验证状态、版本和有效期。不包含完整 credential_ref、namespace、URL 或密钥。浏览器还没有接入该内部端口，更没有读取完整快照的入口。

revoke-origin/revoke-entry/revoke-principal/revoke-config 输入均为 `{"id":...}`，配置版本使用整数。撤销不可通过重新发布同版或重签来源来绕过入口/主体撤销；后续需要显式新登记和审查。

## 能力与任务投影

`projections.capabilities` 只登记本切片实际实现的来源解析、配置快照、任务投影读取三项，标出 HTTP/内部端口。没有 HA、Docker、模型执行或模拟设备成功。CLI `capabilities` 可查询。

`projections.project` / CLI `project-task` 只接受已认证 owner 自己的 `{owner,job_id,version,summary,phase,event_cursor,link,observed_at}`，字段完整、同版异内容冲突、旧版本拒绝。任务执行状态由 owner 负责；平台不运行/重试/取消任务。`projections.tasks` / CLI `tasks` 仅按登记 task_owners 返回，未配置权限返回 unconfigured、没有任务返回空数组；查询有分页，默认 50、最大 100。投影超过 60 秒标 stale，保留最后已观测阶段，不推断停止或成功。这些内部形状尚不是跨产品已发布协议，前端不能直接猜 URL。

本任务不修改前端、不实现完整登录页、通用 Grant/审批、真实 HA/Docker、生产部署、完整项目管理或完整 L0。前端和连接器可基于上述明确已实现接点继续协商正式接入。
