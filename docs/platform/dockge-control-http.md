# Dockge 控制 HTTP 合同（TS-206，v1）

本合同由 TS-206 单写，TS-207 消费。全部入口为同源 `POST /api/web/infrastructure/control/`，沿用真实管理员 Cookie、Origin、`X-CSRF-Token` 和 `external.manage`。普通动作不增加密码解锁。错误仍为平台现有 `{"code":"固定原因码"}`，成功 HTTP 200 只表示读取或持久受理；执行结果看操作记录。HTTP 不等待 SSH。

旧 `/api/web/infrastructure/{view,node,containers,container,logs,...}` 保持不变。这里的节点 ID 与 ExternalCatalog 登记一致。浏览器不得提交路径、argv、地址、凭据或 SSH 选项。所有容器 ID 必须完整 64 位小写 hex；栈 ID、计划 ID、版本 ID 使用服务端原值，不能用显示名代替。

## 通用结构和状态

`connection_revision` 是平台连接修订号（正整数）；`revision` 是节点给出的栈输入指纹（字符串）。二者承担不同校验。外部编辑、重新登记节点或容器换 ID 后，旧动作/计划不得转投新目标。

`ActionAvailability = {"enabled":true,"reason_codes":[]}`。`Maintenance = {"required":false,"owner":null,"reason_codes":[]}`。受保护平台栈或 Dockge 执行依赖返回维护归属和禁止原因，不作为普通 Compose 成功处理。

`Observation = {"status":"ok","freshness":"current","observed_at":"2026-10-19T00:00:00Z","reason_codes":[]}`。`status` 为 `ok/partial/unavailable/unsupported/unknown`；`freshness` 为 `current/stale/missing`。旧样本可以保留显示，但不得当作当前控制授权。

`Operation` 完整字段：

```json
{
  "operation_id":"infra-control:11111111-1111-4111-8111-111111111111",
  "client_id":"11111111-1111-4111-8111-111111111111",
  "kind":"action",
  "node_id":"fixture-nas",
  "connection_revision":1,
  "stack_id":"stack_aaaaaaaaaaaaaaaaaaaaaaaa",
  "container_id":null,
  "action":"stack.restart",
  "title":"重启 Compose 栈",
  "target_label":"synthetic-example",
  "state":"queued",
  "stage":"queued",
  "acceptance":"not_sent",
  "created_at":"2026-10-19T00:00:00Z",
  "updated_at":"2026-10-19T00:00:00Z",
  "finished_at":null,
  "settled":false,
  "reason_codes":[],
  "can_refresh":false,
  "result":null
}
```

`kind` 为 `inventory/detail/plan/action/execute`。`state` 为 `queued/running/observed/partial/failed/unknown`。`observed` 表示对应目标读回或计划已生成；`partial` 表示有部分结果/实际启动或健康未完全满足；`unknown` 不能推断未发送。`stage` 是有限长度固定原因词，客户端可显示后备文案；首版包括 `queued/reading/planning/submitting/reconciling/finished`。`acceptance` 为 `not_sent/submitted/unknown/rejected`，与运行及健康状态分开。

`result` 依 kind 为库存数据、详情数据、Plan 或执行回读。执行回读字段为 `{"runtime_state":"running","health_state":"none","containers":[],"version_id":null,"node_operation_id":"..."}`；`runtime_state` 为 `running/stopped/partial/absent/unknown`，`health_state` 为 `healthy/unhealthy/starting/none/mixed/unknown`，无健康检查为 `none`。容器结果为 `{"container_id":"完整64位ID","state":"running","health":"none"}`。

执行回读另外必有 `command_state: not_sent/sent/succeeded/failed/unknown` 和 `execution_active: true/false/null`。实际运行观测不能证明命令成功；command unknown 始终保持 unknown。active 为 true 或 null 时操作尚不能释放同栈队列，只有节点明确 false 才可结案。unknown 的原操作仍可查询，不能因此生成新动作；partial 且 active 非 false 也继续轮询。stage 还会保留节点 `accepted/validating/pulling/applying/observing/complete/unknown` 阶段。

节点 health `not_configured` 等价映射为 `none`（未配置健康检查），`mixed` 原样保留为混合结果；runtime `absent` 原样保留为未创建运行容器。它们都不冒称健康或已停止。inventory/detail操作的result为null，实际数据在view/detail缓存投影读取，避免操作历史重复存储全部库存。

提交的 `client_id` 必须是规范 UUID。相同键与相同语义重放原记录；同键不同语义 409 `idempotency_conflict`。动作提交丢响应后，**只用 operation 的 client_id 查询原操作**；不生成新键重发。操作保存在既有可备份 `.infrastructure.sqlite`，离开页面、HTTP 取消或重启不会删除记录。

## view：节点控制能力与栈列表

请求：`{"node_id":"fixture-nas","refresh":false,"query":"","page":0,"page_size":50}`。除 `node_id` 外可省略，`page_size` 1–200。首次无样本会自动排后台读取；`refresh:true` 显式刷新，同节点已有刷新则复用，不因页面轮询重复建任务。

响应完整字段：

```json
{
  "configured":true,
  "node_id":"fixture-nas",
  "connection_revision":1,
  "capabilities":{"available":true,"reason_codes":[],"create":{"enabled":true,"reason_codes":[]}},
  "stacks":[{
    "stack_id":"stack_aaaaaaaaaaaaaaaaaaaaaaaa",
    "project_name":"synthetic-example",
    "display_name":"synthetic-example",
    "revision":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "ownership":"platform",
    "exists":true,
    "runtime_state":"running",
    "health_state":"none",
    "containers":["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
    "maintenance":{"required":false,"owner":null,"reason_codes":[]},
    "allowed_actions":{
      "start":{"enabled":true,"reason_codes":[]},
      "stop":{"enabled":true,"reason_codes":[]},
      "restart":{"enabled":true,"reason_codes":[]},
      "update":{"enabled":true,"reason_codes":[]},
      "configure":{"enabled":true,"reason_codes":[]},
      "restore":{"enabled":true,"reason_codes":[]}
    }
  }],
  "containers":[{
    "container_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "stack_id":"stack_aaaaaaaaaaaaaaaaaaaaaaaa",
    "maintenance":{"required":false,"owner":null,"reason_codes":[]},
    "allowed_actions":{"start":{"enabled":true,"reason_codes":[]},"stop":{"enabled":true,"reason_codes":[]},"restart":{"enabled":true,"reason_codes":[]}}
  }],
  "total":1,"page":0,"page_size":50,"has_more":false,
  "observation":{"status":"ok","freshness":"current","observed_at":"2026-10-19T00:00:00Z","reason_codes":[]},
  "refresh":null
}
```

`ownership` 为 `platform/external/unknown`。`refresh` 为 Operation 或 null。`containers` 是控制描述，按 full64 ID 与原只读容器数据联结；没有描述的容器不能猜允许动作。`configured:false` 时列表为空，能力 available=false 并给 `infrastructure_control_not_configured`。未配置和不可用均不能伪造空成功。

## detail：栈配置、版本与维护归属

请求：`{"node_id":"fixture-nas","stack_id":"stack_aaaaaaaaaaaaaaaaaaaaaaaa","refresh":false}`。轮询默认只读缓存；首次缺失自动排读取，显式刷新复用该栈正在运行的读取。

响应：`{"node_id":"fixture-nas","connection_revision":1,"stack":Stack,"compose_content":"services:\n  app:\n    image: synthetic:v1\n","versions":[Version],"observation":Observation,"refresh":Operation|null}`。

`Stack` 与 view 相同。`Version = {"version_id":"服务端版本ID","created_at":"UTC时间","label":"显示标签","current":false,"restorable":true,"reason_codes":[]}`。无记录为 []，读取失败须在 observation 标明原因。

`compose_content` 仅为节点脱敏编辑内容或 null。节点插入的 opaque 秘密占位符必须原样保留，平台不向浏览器还原秘密；提交不带 `.env`，节点原样保留既有 `.env`、数据卷与 bind 数据。不能把星号掩码当新值写回。此处展示的恢复仅恢复配置/运行版本，不宣称恢复数据写入。

容器控制详情使用同一detail接口的另一请求：`{"node_id":"fixture-nas","container_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","refresh":false}`，不得同时传stack_id。返回 `{"node_id":"fixture-nas","connection_revision":1,"container":ControlDescriptor|null,"observation":Observation,"refresh":Operation|null}`。ControlDescriptor与view.containers行一致。未检查过的容器首次自动排后台读取；前端只据该确切full64 ID当前描述允许动作。view.containers返回所有已获得节点单独inspect来源的描述，与栈分页独立，不为每次页面轮询给全部容器发SSH。

节点尚无“当前保存版本”权威字段，version.current暂固定false，不能用包含文件身份的revision等号猜测当前版本。节点schema/语义固定来源为根提交 `6834fd5d0b43294b604b826fceca9587d24737c4`；HTTP configure映射节点stack.plan(action=deploy)，不改变浏览器用词。

## plan：具体预览（异步）

请求字段：

```json
{
  "node_id":"fixture-nas",
  "expected_connection_revision":1,
  "client_id":"22222222-2222-4222-8222-222222222222",
  "action":"configure",
  "stack_id":"stack_aaaaaaaaaaaaaaaaaaaaaaaa",
  "expected_revision":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "compose_content":"services:\n  app:\n    image: synthetic:v2\n"
}
```

`action` 为 `create/configure/update/restore`。create 省略 stack_id/expected_revision，提交 `project_name`（部署登记允许的新项目名）和 compose_content；configure 提交 stack_id/expected_revision/compose_content；update 只提交 stack_id/expected_revision；restore 提交 stack_id/expected_revision/`restore_version_id`。字段严格按动作组合，不接受任意路径/环境/命令。返回 `{"operation":Operation}`；轮询 operation 得到 `result:Plan`。

`Plan` 完整字段：

```json
{
  "plan_id":"服务端计划ID",
  "node_id":"fixture-nas",
  "connection_revision":1,
  "stack_id":"stack_aaaaaaaaaaaaaaaaaaaaaaaa",
  "project_name":"synthetic-example",
  "action":"configure",
  "expected_revision":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "expires_at":"2026-10-19T00:10:00Z",
  "executable":true,
  "changes":["app 镜像 synthetic:v1 → synthetic:v2"],
  "diff":"脱敏配置差异",
  "images":["synthetic:v2"],
  "ports":["127.0.0.1:18080 → 8080/tcp"],
  "mounts":["命名卷 synthetic-data → /data"],
  "warnings":[],
  "blockers":[],
  "maintenance":{"required":false,"owner":null,"reason_codes":[]}
}
```

changes/images/ports/mounts/warnings/blockers 都为有界字符串数组，diff 为有界脱敏文本。真实端口冲突、目标变化、外部编辑、路径/归属问题放 blockers，`executable:false`；不增加额外确认页。预览一次后直接 execute。

## execute：执行预览过的计划

请求：`{"node_id":"fixture-nas","expected_connection_revision":1,"plan_id":"原计划ID","client_id":"33333333-3333-4333-8333-333333333333"}`。

返回 `{"operation":Operation}`。平台绑定本地已生成计划与节点/连接修订号，节点再次核对输入版本。计划过期/外部变更为明确失败，不套用旧内容。execute 不重新接收 compose_content。

## action：普通启停重启

容器请求：`{"node_id":"fixture-nas","expected_connection_revision":1,"client_id":"44444444-4444-4444-8444-444444444444","action":"container.restart","container_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`。

栈请求：`{"node_id":"fixture-nas","expected_connection_revision":1,"client_id":"55555555-5555-4555-8555-555555555555","action":"stack.stop","stack_id":"stack_aaaaaaaaaaaaaaaaaaaaaaaa","expected_revision":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`。

action 只允许 `container.start/container.stop/container.restart/stack.start/stack.stop/stack.restart`。返回 `{"operation":Operation}`。镜像更新通过 plan(update) 和 execute。普通动作单次点击，不另加密码或多确认；同栈进行中的另一变更串行等待，无法确认的旧操作不盲目重发。

## operation：查询/恢复原操作

请求严格二选一：`{"operation_id":"infra-control:...","refresh":false}` 或 `{"client_id":"规范UUID","refresh":false}`。不要求重带节点，因此 POST 响应丢失可只凭原 client_id 查回。不存在为 404 `infrastructure_operation_not_found`。

返回 `{"operation":Operation}`。默认纯本地读取；`refresh:true` 在原操作支持时排读回，复用正在运行的读回任务。已经/可能发送的未知操作只向原节点原 operation_id 查询，不能重新 submit；连接修订号变更则保持未知并返回明确原因，绝不投递新配置。重启也遵循此规则。

## operations：分页操作记录

请求：`{"node_id":"fixture-nas","stack_id":"stack_aaaaaaaaaaaaaaaaaaaaaaaa","cursor":null,"page_size":20}`，所有字段可省略。可选 `state` 使用通用状态枚举。page_size 1–100。默认只列 plan/action/execute，不把每次页面读取填满历史。

返回：`{"items":[Operation],"page":{"returned":1,"has_more":false,"next_cursor":null}}`。按固定创建时间和 operation_id 倒序，游标绑定筛选；不匹配 409 `cursor_conflict`。任务中心复用同一台账的 action/execute，归属 `platform.infrastructure`，不新建执行库。

## 原因码与验证边界

- 400 `invalid_input`；401 `session_expired`；403 `external_manage_required` 及既有 CSRF/Origin 错误。
- 404 `infrastructure_node_not_registered/infrastructure_stack_not_found/infrastructure_container_not_found/infrastructure_operation_not_found`。
- 409 `revision_conflict/idempotency_conflict/cursor_conflict/infrastructure_node_disabled/infrastructure_plan_expired/infrastructure_target_changed/infrastructure_maintenance_required`。
- 429 `too_many_requests`；503 `infrastructure_not_configured/infrastructure_control_not_configured/infrastructure_store_unavailable/infrastructure_executor_unavailable`。
- 远端执行的具体拒绝、超时、部分结果写 Operation.reason_codes；HTTP 受理不伪装执行成功。

所有样例为合成数据。真实 Platform HTTP 会话/CSRF、持久台账与固定节点执行器分别验收；浏览器 route 替身不能称为真实节点或 NAS 控制成功。
