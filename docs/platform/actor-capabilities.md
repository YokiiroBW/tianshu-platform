# 角色调用插件能力

TS-310 / AL-07，在现有插件实例、角色登记、Vault 和执行账本上增加角色入口。此版本为本地候选实现；合同来自 TS-309 `1d5523e7c968d1deccc569ef396d953b8c1f2241`，不是生产发布。

## 配置与使用

1. 在插件中心安装具有实际 action handler 的插件，配置实例参数和凭据并启用。
2. 在实例“作用范围”添加“角色能力”，选择现有已启用角色，勾选允许调用的动作并保存。没有勾选动作即没有角色调用权。工作区绑定的既有语义保留。
3. 内部 Companion 服务凭据必须显式拥有 `actor.capabilities` action。浏览器 Cookie、管理员身份和其他服务身份不能调用该内部入口。
4. Companion 调用 `POST /internal/v1/actor-capabilities/operate`，先 `discover` 获取角色版本和可用动作，再用返回的 binding 调用 `invoke`。所有后续操作使用原 `intent_key` / `invocation_id`。

角色列表只投影 RoleRuntime 的 `actor_id/name/version`。角色须在现有 RoleRuntime 中处于 `active` 且 `enabled`；静态身份名称不能替代实际角色登记。`role_version` 是 Platform 本地登记的 `version`，不是 Companion、Memory 或人格版本。

实例配置和原 Vault 继续拥有账号凭据，角色请求不能指定新账号或改写绑定。此桥只暴露插件已经实现的动作，不把 Host 的 `http.client`、`secrets.read` 等底层端口直接交给角色，也不自动获得 B 站 Cookie 能力。复用媒体账号的动作须由相应适配器单独提供。

## 身份与执行

`actor_capabilities/application.py` 管理服务身份、当前角色和 wire 操作；`plugins/actor_bridge.py` 从插件仓库解析角色绑定、投影动作和回执。`service.py` / `server.py` 只做装配。没有第二套账号、执行队列、授权表或数据库迁移。

绑定必须是完整的 `scope={kind:"character", character_id:actor_id}`，处于启用状态，并在 `entries` 中明确包含 action。`binding_revision` 是所属 `instance.revision`，代表实例配置与绑定的有效快照；网页传入的 `binding.revision` 不是授权版本。

服务器构造稳定 principal `actor-capabilities:` + `actor_id`。原账本以 principal、实例、动作、意图键去重，因此同一角色在对话和后台重试同一意图不会新建调用；不同角色各有自己的调用所有权。同键不同输入或绑定沿用原有 `idempotency_conflict`。

原执行器在受理、排队启动和每次 Host 回调前复核角色、当前配置版本、绑定和动作。`jobs.manage.submit` 继承 principal 与 binding，新增 action 检查防止父动作借子任务跳出所选 `entries`；`get/cancel` 子调用只能访问同一角色和绑定的调用。角色调用不允许 `schedule.put/remove`，因为既有实例调度器使用实例自动化身份，不保留角色授权；角色计划应由 Companion 按时使用同一入口提交。原管理员和实例调度器行为不变。

## 五种操作与回执

| 操作        | 行为                                                                                                                                     |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `discover`  | 可省略 `role_version`，返回当前版本。最多 128 项；停用、配置不足、缺少端口或非本地 schema 的动作显示不可用。发现结果不授予权限。         |
| `invoke`    | 校验参数并走原插件执行器，返回原账本调用的窄投影。执行继续异步进行。                                                                     |
| `get`       | 核对当前角色及当前 binding/action 归属，读取原调用。配置变更后仍保留回执中的原 `binding_revision`，不改写历史。                          |
| `reconcile` | 调用原执行器的 reconcile handler，保持原 invocation。必须仍匹配原配置版本，避免使用旧账号快照执行；未声明 handler 的动作不能假装已对账。 |
| `cancel`    | 取消原调用并读取同一回执，不建立补偿任务；已发生的外部副作用不会被撤回。                                                                 |

`execution` 与 `effect` 分开。读操作保留 `none`；已确认没有写入保留 `not_applied`；`unknown` 不视为成功、不自动重发。进程重启沿用原执行账本的 interrupted/unknown 语义，不自动重新执行。角色或绑定撤销阻止后续排队/Host 工作，不能抹掉既有事实。

输出先经原执行器按 action 的 output schema 校验，再经原凭据去敏，只投影候选合同字段。配置、原始参数、snapshot、checkpoint、内部 principal 和凭据不成为回执顶层字段。请求最多 256 KiB、响应最多 1 MiB，过大数据拒绝而不截断。动态 action schema 只允许自身 fragment 引用，不做网络解析。

HTTP 错误使用本候选的 `execution` 字段。受理后读回执失败或超时保留 `unknown/completed`，不能误报 `not_started`；错误不带原异常文本，`retryable=false` 不指示自动重试外部写入。既有 BotDelivery 等端口错误合同不变。

## 合同与验证入口

`TS012_CONTRACT_DIR` 仍指向完整合同集的 `text-dialogue/v1`。首次使用角色协议时，按 LF 规范化校验 `actor-capabilities/candidate-v1` manifest 与所有文件 SHA256；manifest 固定为 `6e36fbfa6c82cadc93b2d0642c3591b596f4e9d7dde0b8774742ba63db1631ef`。缺少候选合同不会阻止既有平台启动，但角色入口返回依赖不可用。

受影响测试：`test_actor_capabilities.py`、`test_actor_capabilities_http.py`、`test_actor_capabilities_pg.py`、`test_plugins.py`、`test_plugins_web.py`。浏览器使用 `apps/web/playwright.plugins.config.ts` 与隔离合成角色、实际插件进程和 loopback 提供者。PG 专项仅在显式配置隔离 `TS237_TEST_POSTGRES` 后运行。

本卡不接通 Companion 的自主规划消费、不提供新的内容发现插件，不改 Memory/Companion、群聊观察或 BotDelivery。模型、真实账号、NAS 和生产 PostgreSQL 均需各自后续验收；本地合成用例不能证明真实平台账号动作可用。
