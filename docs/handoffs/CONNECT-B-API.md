# CONNECT-B 浏览器 API（2026-09-27）

本文件供 CONNECT-U 消费。以下为本轮后端目标契约；标为「已有」的路由在基线存在，标为「新增」的路由由 CONNECT-B 实现并验证后在本文件更新状态。所有 `/api/web/*` 使用既有同源 Cookie、`X-CSRF-Token`、精确 `Origin`、`POST application/json`，请求体最多 16 KiB；先取 `GET /api/web/session`。失败沿用 `{schema_version:1,request_id,code,execution_state,retryable}`，不能把失败当空数据。所有上游凭据、URL、本地路径和原始授权只留服务端。

## 页面与连接状态

| 能力 | 浏览器端口 | 当前基线 |
| --- | --- | --- |
| 登录/对话 | `/api/web/session`, `/api/web/messages`, `/api/web/snapshot` | 已有；模型与 Core 仍需实测 |
| 模型供应商 | `/api/web/providers/*` | 已有，NAS 已部署 |
| 人格目录/历史/版本/比较 | `/api/web/personas/{catalog,history,revision,compare}` | 已有；候选合同仅 local_rehearsal。正式 v1 消费器已备，最终 manifest 未发布前生产拒启 |
| 资产 | `/api/web/assets/{state,connection,libraries,browse,search,entry}` | 已有，须部署登记及对端授权 |
| 家庭 | `/api/web/home/{view,refresh,control}` | 已有，须部署登记及对端授权；控制需独立解锁 |
| 任务 | `/api/web/tasks/{view,detail}` | 已有，仅平台真实台账，其它来源按未接入显示 |
| Memory 人物画像/本人记忆 | `/api/web/memory/{state,overview,subjects,records}` | 本轮新增固定账号/actor 的只读连接器；上游由 CONNECT-M 实现 `/internal/v1/memory/browser/*`，以其最终交接和联合验证为准 |
| Memory 项目知识/资料/研究笔记 | `/api/web/knowledge/{state,query,documents,document,notes}` | 本轮新增只读连接器；对端 `/local/v1/project-knowledge/action` 已支持经受控 TLS/Host 的服务绑定，但 NAS 尚未部署该独立进程与权限 |
| Companion 生活/已发布日记 | `/api/web/life/{state,actors,snapshot,diaries,revision}` | 本轮新增只读连接器；对端四条 `/internal/v1/life-read/*` 已存在，需部署读者与剧情授权 |
| 统一连接摘要 | `/api/web/connections/view` | 本轮新增，只报告实际静态配置与最近一次真实读数，未验证不标 connected |

## 新增只读端点的目标形状

### 管理员外部连接（后端候选实现）

同源 `/api/web/external/*` 仅管理员显式 `external.manage` 动作；沿用 Cookie/CSRF/Origin，保存与检测前调用 `/unlock` 以当前账号密码二次解锁，窗口 15 分钟。页面不填内部 principal/token_env。配置只对 **HA** 与 **AssetLink** 两种固定连接生效，部署仅限定可选种类、目标 CIDR 与是否允许私有 HTTP，不预登记用户要填的具体 URL/实体。

| 端点 | 请求 | 响应 |
| --- | --- | --- |
| `POST /api/web/external/view` | `{}` | `{revision,unlocked,assets,home}`；每项含 `configured,enabled,url,credential_configured,ca_configured,last_test`，home 另含登记 `entities`；绝不回显凭据/CA 私有内容 |
| `POST /api/web/external/unlock` | `{password}` | `{unlocked:true,expires_in:900}`，复用网页登录限流 |
| `POST /api/web/external/lock` | `{}` | `{unlocked:false}` |
| `POST /api/web/external/save` | `{kind:"assets"|"home",expected_revision,client_id,value,credential:{action:"keep"|"replace"|"clear",value?},ca:{action:"keep"|"replace"|"clear",value?}}` | `{revision,state:"saved_unverified",applied:true,kind}`；CAS 冲突 409，凭据与 CA 分别 keep/replace/clear |
| `POST /api/web/external/test` | `{kind}` | `{kind,state:"connected"|"unavailable"|"unauthorized",code,checked_at,revision}`；只对已经保存的固定目标执行一次真实只读请求，不接受 URL、path、method 或请求体 |

`assets.value={enabled,endpoint}`，endpoint 必须 HTTPS、精确 `/assetlink/v1/control`；实际浏览仍使用既有 AssetLink 五项读与对端 ACL。`home.value={enabled,base_url,allow_private_http,entities:[{entity_id,label,kind,unit?}]}`，kind 只允许 `sensor/light/switch` 的现有固定读数类型、实体数与字符串有界；读配置不自动生成控制模板。`credential` 对两类都是对应上游 Bearer；`ca` 仅接受最多 16 KiB 的有效 PEM X.509 证书链，拒私钥。HTTP 只在管理员显式选择、目标地址在部署允许的私有范围且地址快照已 pin 时可用。保存成功会更新服务端连接快照、令所有旧资产/设备页面作用域失效；当前保存会话保留原解锁截止时间可立即检测，不延长 15 分钟，其它会话解锁失效。保存仅代表 `saved_unverified`，检测成功后才称 connected，`last_test` 只属于所测 `revision`。新凭据不出日志/响应，独立加密库、key 与 seal 必须一致备份。

`web_external` 部署只指定私有目录、允许的 CIDR 与固定 AssetLink connection ID；该 ID 在 `asset_connections` 以 `{managed_external:true}` 占位并由固定 `asset.read` 服务主体及 `web_assets` 页面绑定。网页自填目标 URL/实体，不需运维预登记其具体值。私有目录必须在停服安装阶段显式执行 `python -m services.platform.external_catalog init <绝对目录>` 并设置仅服务账号/管理员可读写 ACL；运行与只读预检只执行 `... check <绝对目录>`，缺任一目录/库/key/seal 即拒启，不自动重建。部署允许 CIDR 变化后每次使用已保存 pin 都重新按当前白名单核验，DNS 仅在保存时解析并固定。HA 私有配置不与旧静态 `home` 并用。

`/api/web/knowledge/state {}` → `{available,code,projects:[{project_id,label}],peer:{configured,verified_at,code}}`。`available` 仅表示本平台连接配置完整；`verified_at:null` 表示从未实际读取。项目列表是部署端显式允许的闭集，不从浏览器传 URL/凭据。`/query {project_id,text,budget_bytes}`、`/documents {project_id,limit,cursor}`、`/document {project_id,document_id,expected_version,expected_hash,limit,cursor}`、`/notes {project_id,text,budget_bytes}` 分别只调用 Memory 的 `query/document_list/document_read/note_query`。成功体为 `{project_id,operation,result}`，其中 `result` 是 Memory 对应只读操作的有界原生结果；请求不得指定 `operation` 或 `arguments`。分页光标只由上页返回，`cursor` 首屏为 `null`。研究笔记的新增/修订/撤回没有网页写入口。

**错题、经验、交接（本轮新增，候选实现中）**：`knowledge/state.projects` 每项另有 `checkouts:[{id,label}]`，由 Platform 部署明确登记并由 Memory 逐请求独立核验；无 checkout 时为 `[]`。`POST /api/web/knowledge/lessons {project_id,text,budget_bytes}`、`/experiences {project_id,text,budget_bytes}`，预算 256..32768、文本 1..1024；成功均沿用 `{project_id,operation,result}`，其中 `operation` 分别为 `lessons`/`experiences`，`result` 是对端有界的 `lessons` 或 `entries` 检索结果及 `omissions/retrieval/trust`。它们是关键词检索，不代表全量目录。Memory 的 `experience_query` 另有 `review` 权限，缺权限 403 `upstream_forbidden`，未显式开启新增 HTTP 读 503 `knowledge_operation_not_enabled`。

`POST /api/web/knowledge/continuation {project_id,checkout_id,text,budget_bytes}`：预算 4096..32768，仅在用户主动点击时发出；`checkout_id` 必须在本项目 `checkouts` 闭集。Platform 调 Memory `continuation_recover`，完整封装包只保存在本网页登录会话内 15 分钟，最多 4 个，登出/权限撤销即失效。成功返回 `{project_id,operation:"continuation",result:{handle,expires_in:900,status,checkout:{id,branch,head,dirty,collected_at},index:{total,listed,truncated},state:{version,current,stale_evidence,goal,constraints,unfinished}|null,omissions,budget:{limit_bytes,used_bytes,over_budget},revision}}`；仅这些白名单字段，没有 seal、源路径、文件 locator、Git 命令、凭据或完整包。`POST /api/web/knowledge/continuation-check {project_id,handle}` 将会话内原包交给 Memory `continuation_check`，返回 `{project_id,operation:"continuation-check",result:{valid,reason,differences,observed,checkout_id,checked_at}}`。handle 只在同一会话/项目有效，失效为 409 `continuation_handle_expired`，服务端每次仍做当前项目授权；校验结果 `valid:false` 必须显示为过期/差异，不能当服务故障或当前有效。两个端点不接受路径、原包、seal 或自选 operation。Memory 只读观察实际 Git/文件，可能耗时，不做后台轮询，也不运行导入/扫描/写入。

`/api/web/life/state {}` → `{available,code,peer:{configured,verified_at,code}}`。`/actors {limit,after_actor_id}`、`/snapshot {actor_id}`、`/diaries {actor_id,limit,after}`、`/revision {actor_id,diary_id,revision_id,expected_diary_version}`。成功体为对端相应只读端口的 JSON 答案，保留 `state_basis=last_persisted` 与已发布指针；日记列表不含正文，只有 `/revision` 在对端核对发布版本后返回正文。浏览器不能传 reader_id、上游 URL 或 token。

`/api/web/connections/view {}` → `{connections:[{id,state,code,detail,checked_at}]}`。`detail` 可为 `null`。`state` 只取 `connected`（有真实成功读数）、`unverified`（有配置未实测）、`not_configured`、`unauthorized`、`unavailable`。该摘要不主动探测任意目标，不把 health/容器 running 当业务成功；用户可在对应页面实际读取后刷新。对话、供应商、人格、家庭与任务若没有本摘要可证明的真实调用回执，保守显示 `unverified`，其对应业务页面的实际状态更精确。

固定连接项及顺序：`dialogue`、`providers`、`personas`、`knowledge`、`life`、`assets`、`home`、`tasks`、`memory_profiles`。`memory_profiles` 与 `knowledge` 是独立身份和页面，项目资料不能冒充人物记忆。

### Memory 人物/记忆只读页

`/api/web/memory/state {}` → `{available,code,peer:{configured,verified_at,code},actor_id}`；`actor_id` 是部署登记的固定本人 actor，未授权时为 `null`，`verified_at` 只有成功真实读取后才非空。

`/api/web/memory/overview {}` → `{schema_version:1,verified_at,scope_version,memory_group_count,counts_truncated}`；截断时计数只是下界，人物数不在 overview 中估计。`/subjects {limit,cursor}` → 公共字段加 `items:[{subject,categories,group_count,group_count_truncated}],next_cursor`。`/records {subject,limit,cursor}` → 公共字段加 `items:[{semantic_group_id,category,field_key,item_key,units:[{record_id,record_version,statement,conditions,negations,valid_time,uncertainty,reality}]}],next_cursor`。`subject` 只能是 `null`（本人记忆）、`{kind:"person",person_id}` 或 `{kind:"group",conversation_id}`；首页 `cursor:null`，`limit` 1..50。空 items 但 next_cursor 非空时仍须翻页。成功体保留 Memory 的 `verified_at`/`scope_version`，平台核验后移除上游 `scope` 与 `request_id`；`origin` 从不出浏览器。

同源端不接受浏览器 account、actor、scope、origin、Bearer 或上游 URL。服务器配置 `web_memory` 固定 HTTPS endpoint、独立 `token_env`、`entry_id` 与 CA；该 entry 必须属于网页登录 operator 同一稳定账号，是 `local_operator/self_private`，并有 `{caller:platform,receiver:memory,purpose:dialogue}` 精确 route。每次浏览读取先由真实 Platform `Origins.issue` 签新 assertion，再发给 Memory；Memory 用自己的发行方凭据正式 resolve，要求其 `browser_readers` 固定账号/actor/scope 与当次解析结果一致。缺身份映射为 `memory_identity_not_ready`，撤权/失效不是空集合。服务 Bearer 只获 Memory `browse`，不能调用生成上下文或写入端口。用户批准/遗忘写流程仍需完整用户确认，本路由不提供写按钮。

### 上游结果的可渲染字段

- `knowledge/query`：`result={project_id,blocks:[{reference,text,spans,provenance,...}],omissions,retrieval:"lexical",trust}`，块可为空，`omissions` 说明预算或过期资料遗漏。
- `knowledge/documents`：`result={project_id,project_revision,items:[{document_id,kind,version,state,...}],next_cursor,omissions,trust}`；`next_cursor=null` 表示本页终止。目录条目不保证源文件仍新鲜，点击阅读再核实际来源。
- `knowledge/document`：`result={project_id,project_revision,document_id,version,hash,blocks:[{reference,text,...}],next_cursor,omissions,trust}`；浏览器输入 `expected_version` 必须来自目录、`expected_hash` 首屏可为 `null`，续页须沿上一页已确定的 hash；版本/来源变化返回冲突，不拼接旧页。
- `knowledge/notes`：`result={project_id,notes:[...],omissions,retrieval:"lexical",trust:"operator_research_note_with_source_versions",project_workdir:"authoritative_over_notes"}`。显示来源有效性与遗漏，笔记不能当当前工作目录事实。
- `life/actors`：`{schema_version:1,fictional:true,items:[{actor_id,actor_version,world_id,room_id}],next_after_actor_id}`。
- `life/snapshot`：`{schema_version:1,fictional:true,actor_id,actor_version,world_id,world_version,room_id,room_version,timezone,activity,mood,outfit_ref,changed_at,observed_at,state_basis:"last_persisted"}`；不是实时推进后的状态。
- `life/diaries`：`{schema_version:1,fictional:true,actor_id,items:[{diary_id,actor_id,day,state,version,published_revision_id,fictional,captured:{recipe_id,recipe_version,material_version}}],next_after}`。`next_after` 为 `null` 或 `{day,diary_id}`；列表从不返回正文。
- `life/revision`：`{schema_version:1,fictional:true,actor_id,diary_id,diary_version,revision_id,content,created_at,captured}`；`captured` 含 recipe/material/config 版本。仅已发布指针的正文会被上游返回。

知识、生活与 Memory 页失败：未配置 `*_not_configured` 503；缺页面读动作 `*_read_required` 403；独立上游凭据缺失 `*_credential_missing` 503；对端拒权 `upstream_forbidden` 403；失效版本或来源 `version_conflict`/`stale_evidence`/`cursor_stale`/`invalid_cursor`/`scope_changed` 409；上游失联 `dependency_unavailable` 503；超时 `timeout` 503；回答不合请求 `invalid_upstream` 502。失败不会变成 `{items:[]}`。

## 现有端口精确参照

- 人格、资产、家庭、任务的细节以本仓库 `docs/platform/{personas,web-assets,home-devices,task-center}.md` 为准，本轮不更改现有形状。
- Memory 上游是固定身份 + 每请求 Bearer，`document_list`/`document_read` 要求对端显式同名 permission。`knowledge_cli serve` 默认 loopback；其 `--host`/`--tls-certfile`/`--tls-keyfile`/`--allowed-host` 可在 `server_runtime.resolve_binding` 校验后组成受控非 loopback TLS 入口。NAS 尚未运行该独立进程，部署须显式配置固定身份、权限及证书。
- Companion 生活上游由 `life_readers` 和现有剧情授权双重限制，读数不 tick；只有已发布日记可读。
- 单纯无业务数据应显示为空态；未配置、未授权、上游故障和功能尚未提供须分开显示。

## 部署阻断（给总控）

Memory 知识服务已有受控非 loopback TLS 能力；当前 NAS 未部署知识服务独立进程，尚缺经审阅的绑定/证书、固定身份凭据、项目白名单、逐操作权限、库 schema 与实际业务读取验收。CONNECT-B 仅接固定登记端点，不做开放代理、DNS/网段扫描或读取 Memory 数据库。
