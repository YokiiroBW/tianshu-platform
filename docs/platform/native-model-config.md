# 平台原生模型配置（model-protocol/v1）

TS-015 在唯一 `platform.Models` owner 内增加原生 `openai-responses` 配置生产者：独立持久表、独立
`native_config_version` 序列、独立撤销键空间、独立 HTTP 快照端口与合同自有错误信封。旧
`text-dialogue/v1` Chat 配置的表、版本、权限、状态码和路由全部保持原样。

合同依据为根 `contracts/model-protocol/v1` 1.0.0（`implementation_baseline_published`）：

| 项                       | 值                                                                 |
| ------------------------ | ------------------------------------------------------------------ |
| manifest LF SHA256       | `52711a71de56dbceebd1d5d96b2baf59a2d9551168029972d59111480f815141` |
| 唯一外部依赖             | `text-dialogue/v1/schemas/common.json`，SHA-256 `b296a79d…ef9af`    |
| 平台端口                 | `POST /internal/v1/model-config/native/snapshot`                    |
| 平台实现状态             | 生产者已实现；运行端口默认关闭，由设置显式开启                      |
| 网关消费 / 真实模型      | 未验证：`runtime_routes_enabled=false`，网关由其任务独立适配        |

平台启动时按固定摘要校验该包的全部交付文件（含 `schemas/model.json`）及其 `common.json` 依赖摘要；
任一字节不符即明确失败，不回退到其他版本，也不从候选目录或网络加载。

## 与旧 Chat 的隔离

| 边界         | 旧 Chat                          | 原生 Responses                                      |
| ------------ | -------------------------------- | --------------------------------------------------- |
| 合同         | `text-dialogue/v1` 1.0.0         | `model-protocol/v1` 1.0.0                            |
| protocol     | `openai-chat-completions`        | `openai-responses`                                   |
| workload     | `companion.text`                 | `native.responses`                                   |
| 版本字段     | `config_version`                 | `native_config_version`                              |
| 授权列表     | `caller.config_versions`         | `caller.native_config_versions`（缺省为空，不继承）  |
| 持久表       | `configs`                        | `native_configs`                                     |
| 快照路径     | `/internal/v1/model-config/snapshot` | `/internal/v1/model-config/native/snapshot`      |
| 错误信封     | `common#error`                   | `model#error`（`contract`、`retryable=false`）       |
| 撤销/到期    | 410                              | 撤销 403 `forbidden`，到期 503 `dependency_unavailable` |

同一整数在两张表中是两份无关配置：原生 7 不影响 Chat 7，也不影响 Chat 的 latest 选择；撤销其中一方
不改变另一方。版本单调、去重摘要和撤销标记都只在各自表内计算。

## 设置

原生端口默认不存在。只有部署显式设置才注册路由；CLI 管理口与端口开关相互独立（关闭端口仍可本地发布，
但没有任何 HTTP 读取者能拿到配置）。

```json
{
  "native_config_http": true,
  "providers": {
    "provider-native": {
      "protocol": "openai-responses",
      "base_url": "https://model.example.invalid/v1",
      "credential_ref": "secret-ref:provider/native",
      "credential_namespace": "native-model-account",
      "model_ids": ["configured-native-model"],
      "capability_verification": "fixture_only",
      "verified_capabilities": ["text", "stream"],
      "reviewed_addresses": ["192.0.2.10"],
      "allow_private_http": false
    }
  }
}
```

`protocol` 是注册表的协议钉：原生发布要求登记项显式 `openai-responses`，旧 Chat 发布只接受未钉协议或
`openai-chat-completions` 的登记项。同一 provider 不会被两种协议共用，避免把 Responses 配置塞进 Chat
标签。provider_id 不重复，binding workload 不重复，binding 的 model_id 必须等于 provider 的 model_id。

## 发布、撤销与本地视图

发布输入是发布时形状的 `model-protocol/v1#config_response`（含 `native_config_version`、providers、
bindings、有效期）；`request_id` 只做关联，不参与不可变摘要。除 schema 外，写事务内还检查：provider
注册按 protocol/精确 base_url/credential_ref/credential_namespace/模型/能力/已审地址逐项匹配、
`state_references=reject`、`model_policy`/`reasoning_policy` 均为空字段的 `preserve_client`、有效期
非空且不超过最大发布时长（默认 1 小时，可显式放宽到 24 小时）、文档中不含任何已登记环境凭据明文。

```powershell
.venv/Scripts/python.exe -m services.platform --settings .runtime/settings.json local --credential-env TIANSHU_LOCAL_OPERATOR_TOKEN publish-native --input .runtime/native-publication.json
.venv/Scripts/python.exe -m services.platform --settings .runtime/settings.json local --credential-env TIANSHU_LOCAL_OPERATOR_TOKEN view-native
.venv/Scripts/python.exe -m services.platform --settings .runtime/settings.json local --credential-env TIANSHU_LOCAL_OPERATOR_TOKEN revoke-native --input .runtime/native-revoke.json
```

三个动作复用既有 operator 发布权限 `config.publish` / `config.revoke` / `config.view`，不新增写服务，
也不新增网页入口。`view-native` 只给已认证本地操作者脱敏投影：版本、有效期、availability 与
provider/model/协议/验证状态；不含 URL、credential_ref、credential_namespace 或任何密钥。

`native_revoke` 输入为 `{"id": <整数版本>}`；未知版本 404。撤销后的版本不能再发布同版内容（403），
也不能被读取。重启后表中原样恢复，不需要重放。

## 快照端口

| 项       | 要求                                                                                        |
| -------- | ------------------------------------------------------------------------------------------- |
| 输入     | `model-protocol/v1#config_request`（`query`、`native_config_version`、`contract` 只此三项） |
| 输出     | `model-protocol/v1#config_response`，`request_id` 取自请求                                   |
| 错误     | `model-protocol/v1#error`，`execution_state=not_started`，`retryable=false`                  |
| 认证     | 服务凭据 `config.snapshot` + 已签发来源（receiver `platform`、purpose `config.snapshot`）    |
| 授权     | 显式 `caller.native_config_versions`；缺失即空集，绝不继承 `config_versions`                 |
| 浏览器   | 带 Origin/Cookie 的请求在读取正文前被拒绝，无 CORS                                           |

选择与错误语义（与 `examples/relations.json` 的脚本化关系一致）：

| 情况                                             | 结果                             |
| ------------------------------------------------ | -------------------------------- |
| 指定版本在授权集内，存在、未撤销、未过期         | 200 快照                         |
| 指定版本不在授权集内                             | 403 `forbidden`                  |
| 指定版本已授权但不存在                           | 404 `not_found`                  |
| 指定版本已撤销                                   | 403 `forbidden`                  |
| 指定版本已到期（含 `now == usable_until`）       | 503 `dependency_unavailable`     |
| `native_config_version` 为 null 且授权集为空     | 403 `forbidden`                  |
| null：在授权集内取最大且未撤销、当前有效的版本   | 200 该版本                       |
| null：授权集非空但无任何可用版本                 | 503 `dependency_unavailable`     |
| 存储文档摘要与内容不一致                         | 503 `dependency_unavailable`     |
| 端口未开启                                       | 404 `not_found`（原生信封）      |

null 版本不接受调用者自带版本偏好，也不会先读全局最高版本再越权：筛选在 SQL 中按调用者授权集完成。
请求体的 `principal_id`、`permission`、`config_version` 等额外字段一律非法；身份、namespace 与版本
授权只来自服务认证和来源断言，绝不从 JSON 字段推断。

## 本地存储与迁移

`native_configs(version INTEGER PRIMARY KEY, document, digest, revoked)` 与 `configs` 同形、异表。
本地 SQLite schema 版本提升到 2：

- 新建库直接使用 2。
- 旧版本 1 库在迁移前生成唯一备份 `<db>.pre-native-<hex>.sqlite`（含 WAL），再在同一
  `BEGIN IMMEDIATE` 事务内建表并写入 `user_version=2`；失败整体回滚，不留半迁移状态。
- 版本 0 的演练期旧库仍沿用原有 `.pre-source-<hex>.sqlite` 备份路径，不会额外产生 native 备份。
- 备份文件是忽略的运行产物，不进库、不提交。

## 实际验证

```powershell
# 最窄（本次新增）
$env:TS012_CONTRACT_DIR = "<已发布 contracts>/text-dialogue/v1"
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p "test_native*.py" -v

# 受影响后端回归（含旧 Chat / 网页 / 资产）
.runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -v

# 静态与格式
.runtime/venv/Scripts/python.exe -m ruff check services/platform tests/backend
.runtime/venv/Scripts/python.exe -m ruff format --check services/platform tests/backend
```

`test_native_models.py` 使用已发布 `examples/documents.json` 的原生样例（仅把 provider id 换成本地合成
登记名）覆盖：协议钉注册与逐项绑定、能力不越权、版本隔离与撤销键空间隔离、显式授权与 null latest、
到期/撤销/摘要错误码、同版去重与不可覆盖、真实 HTTP 端口与 `model#error` 信封、端口关闭时 404、
浏览器/重复 Authorization/超限正文/畸形 JSON 拒绝、重启后同一快照、v1→v2 迁移备份，以及真实 CLI
子进程的 publish-native/revoke-native/view-native 脱敏。

## 未验证与下一步

- 网关消费（`POST /v1/responses`、route_context、原生 SSE/回执）由其任务与协调联合验收，本平台不实现、
  不猜测其形状，也不改其工作树。
- 没有任何真实模型账号、真实供应商调用、付费模型、真实设备或生产部署：全部验证使用合成 fixture。
- 网页控制台未接入原生配置，仍只读旧 Chat 投影；本轮不改 `apps/web`。
- 根 manifest 的 `runtime_routes_enabled=false` 与接口目录的
  `runtime_disabled_until_joint_acceptance` 仍然有效：本地设置开启端口只是隔离演练，不等于合同开放。
