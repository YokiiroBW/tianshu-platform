# CONNECT-B NAS 配置准备单（2026-09-28）

本单只供总控在受控部署中审阅和执行；CONNECT-B 未连接或修改 NAS，也未读取现场私密数据。以现场已知登记为前提：Platform operator `admin`、账号 `{namespace:web,immutable_account_id:household-admin}`、entry `web-actor`（`local_operator/self_private`、`actor:household`、channel `web-household/private-chat`）；Companion 仅有 `roles.actor:household` 的 `version:1/persona`，尚无 Life actor、Persona 管理凭据与 Life reader。保留原有配置的其它 principals/actions/routes、input_entries、services、TLS、数据库和日志设置；以下是**增量形状**，不应覆盖整个 JSON。所有 `REPLACE_*` 均为不可运行占位，由总控签发/核验；各独立凭据不得复用。所列 `/etc/tianshu`、`/srv/tianshu` 路径是建议的现场配置位置，执行前须与实际挂载和服务账号核对。

## 1. Platform 私有配置增量

在既有 `mode:"service_https"`、`web.origin:"https://platform.internal:8443"`、`tls`、`web.principal:"admin"` 上，给 `principals.admin.actions` 加 `persona.read`、`life.read`、`knowledge.read`、`memory.read`、`external.manage`，并保留既有 `origin.issue`、`source.register`、`source.dispatch`、`mapping.prepare` 等。`entries.web-actor.routes` **追加** `{caller:"platform",receiver:"memory",purpose:"dialogue"}`，保留原来的 Platform→Companion、Companion→Memory、Companion→Platform route；不改 account/channel/actor。Memory issuer 再登记一个独立 service principal：

```json
"memory-browser-resolver": {
  "kind": "service", "service": "memory",
  "token_env": "MEMORY_BROWSER_TO_PLATFORM_ISSUER_TOKEN",
  "actions": ["origin.resolve"],
  "resolver": {"caller": "platform", "purpose": "dialogue"}
}
```

此 resolver 的 token 值供 Memory `callers.platform.issuer_token` 使用。它不是 Memory browser Bearer，亦不是既有 resolver.caller=`companion` 的来源权限；同一个 `origin.resolve` wire 只接受当次正式 `assertion_ref`。

以下键须加入同一份 Platform 设置文件；`/etc/tianshu/contracts/persona-management/v1` 是只读挂载的总控发布包，manifest SHA256 必须是 `72ae9ee2fd5e0140e122877c35d90eb747f1d56804c33d8f01c9eb38413c0e2d`（根发布提交 `f705475d9ee26df29ea634349d8c18398418b40f`）。`web_personas` **只**用 `published_directory`，不填 `candidate_directory`/`allow_candidate`：

```json
{
  "persona_connections": {
    "companion-personas": {
      "base_url": "https://companion.internal:8765",
      "token_env": "PLATFORM_TO_COMPANION_PERSONA_TOKEN",
      "ca_file": "/etc/tianshu/tls/ca.pem", "timeout_seconds": 10
    }
  },
  "web_personas": {
    "enabled": true, "connection_id": "companion-personas",
    "allowed_subjects": ["actor:household"],
    "published_directory": "/etc/tianshu/contracts/persona-management/v1"
  },
  "web_life": {
    "enabled": true, "base_url": "https://companion.internal:8765",
    "token_env": "PLATFORM_TO_COMPANION_LIFE_TOKEN",
    "ca_file": "/etc/tianshu/tls/ca.pem"
  },
  "web_knowledge": {
    "enabled": true, "base_url": "https://knowledge.internal:8131",
    "token_env": "PLATFORM_TO_KNOWLEDGE_TOKEN",
    "ca_file": "/etc/tianshu/tls/ca.pem",
    "projects": [{"project_id": "household-knowledge", "label": "家庭项目资料"}]
  },
  "web_memory": {
    "enabled": true, "base_url": "https://memory.internal:8130",
    "token_env": "PLATFORM_TO_MEMORY_BROWSER_TOKEN",
    "ca_file": "/etc/tianshu/tls/ca.pem", "entry_id": "web-actor"
  }
}
```

`household-knowledge` 是本准备单提出的**新项目 ID**，总控核准后在 Platform 与独立 Knowledge 配置中逐字一致；`checkouts` 现在不配置，前端续接页应显示未登记，不能冒用测试工作树。Persona 的 `PLATFORM_TO_COMPANION_PERSONA_TOKEN` 的**值**须等于 Companion `personas.admin_token_env` 指向的管理凭据；Core 当前四个 Persona 只读操作仍走 `/internal/v1/persona/manage`，没有另一条纯只读 Persona caller。该凭据也有管理写权限，必须只由 Platform 服务端持有、限定内部网络与文件权限；网页只能通过自身 `persona.read` 固定四读入口，不能拿到凭据或自选上游操作。若不能接受该权限边界，不应启用这条生产连接。

Memory 私有 scope 尚未核实前，先将 `web_memory.enabled` 设为 `false`（或暂不加入该段）；其真实读取会因缺 person/conversation 映射而明确不可用，不应先写猜测的 scope。其余读者的 `token_env` 名称和值必须互不复用，也不能与现有 Platform principal、Companion/Core/Gateway 凭据相同。

外部 AssetLink/HA 使用固定类别与私有目录，不在部署 JSON 写管理员输入的 URL、Bearer、CA 或实体。仅在总控审定实际私有子网后替换下面的 CIDR，占位串会被校验拒绝；`home` 旧静态段应移除：

```json
{
  "principals": {
    "asset-web-reader": {
      "kind": "service", "service": "platform",
      "token_env": "PLATFORM_ASSET_WEB_READER_TOKEN",
      "actions": ["asset.read"], "asset_connections": ["assetlink-managed"]
    }
  },
  "asset_connections": {"assetlink-managed": {"managed_external": true}},
  "web_assets": {
    "enabled": true, "principal": "asset-web-reader",
    "allowed_connections": ["assetlink-managed"]
  },
  "web_external": {
    "directory": "/srv/tianshu/external-connections",
    "allowed_cidrs": ["REPLACE_WITH_REVIEWED_PRIVATE_CIDR"],
    "assets_connection_id": "assetlink-managed"
  }
}
```

`asset-web-reader` 是新增 service principal，不能取代网页 operator。保存时目标必须属于已审私有 CIDR；AssetLink 仅 HTTPS 精确 `/assetlink/v1/control`，HA 仅登记实体状态读，私有 HTTP 需管理员显式勾选。保存返回 `saved_unverified`，只有固定真实 `external/test` 通过才是 connected。目录初始化、key/DB 一致备份、ACL 与回退见 [CONNECT-B-EXTERNAL.md](CONNECT-B-EXTERNAL.md)。

## 2. Memory 本人浏览身份与正式 scope

Memory 聊天进程继续使用既有私有 `mode:"source_sync"`、`database_path`、source-guard、Platform/Core 来源配置与 8130 TLS；在**同一**私有配置中增量登记 `callers.platform` 和 `browser_readers.platform`。Memory `callers.platform` 的 `token` 值与 Platform 环境变量 `PLATFORM_TO_MEMORY_BROWSER_TOKEN` 相同；`issuer_token` 的值与 Platform `MEMORY_BROWSER_TO_PLATFORM_ISSUER_TOKEN` 相同，两个值必须不同：

```json
{
  "callers": {
    "platform": {
      "token": "REPLACE_WITH_BROWSER_BEARER",
      "issuer": "platform",
      "issuer_url": "https://platform.internal:8443/internal/v1/origins/resolve",
      "issuer_token": "REPLACE_WITH_DISTINCT_ISSUER_BEARER",
      "issuer_ca_file": "/etc/tianshu/tls/ca.pem",
      "allowed_actors": ["actor:household"], "operations": ["browse"]
    }
  },
  "browser_readers": {
    "platform": {
      "account": {"namespace": "web", "immutable_account_id": "household-admin"},
      "actor_id": "actor:household",
      "scopes": [{
        "actor_id": "actor:household", "audience": "self_private",
        "person_id": "REPLACE_FROM_VERIFIED_PLATFORM_RESOLVE",
        "conversation_id": "REPLACE_FROM_VERIFIED_PLATFORM_RESOLVE"
      }]
    }
  }
}
```

**不能从 `web-household/private-chat` 字符串猜 person/conversation ID，也不能插入数据库造映射。** 总控使用正式 Platform `origin.issue` 对当前 `web-actor` 发新引用，再以已登记的 `memory-browser-resolver` 服务凭据调用 Platform `/internal/v1/origins/resolve`，确认返回 `context.verified_account`、`authenticated_service=platform`、`audience_service=memory`、`allowed_scope` 与账号/actor/channel一致，并只把当次 `allowed_scope.person_id/conversation_id` 用于上面的精确 scope 登记。`origin.issue` 本身只回 assertion_ref/expiry，**不回 scope**。若任一 ID 为 null 或 resolve 拒绝，应停在 `memory_identity_not_ready`/未配置，不能改 SQLite。Memory `identity/register` 可经正式 Core 身份调用创建 person，但 Platform `identities` 还需以真实 Memory 响应走 `prepare-mapping`/`confirm-mapping`；`channels` 的 conversation ID 仅在 Core 成功接受真实输入后，由 Platform `dispatch-fanout` 对真实 fanout 响应确认写入。Core 返回 `dependency_unavailable` 时这一步不会发生，须先排查失败阶段，再以正式路径重试。

可审阅的 Platform 本地签发命令形状（输入文件仅 `{"entry_id":"web-actor"}`；需已设置独立 operator 环境凭据，输出 ref 是短期敏感引用，不进工单或仓库）：

```sh
python -m services.platform --settings /etc/tianshu/platform.json \
  local --credential-env WEB_ADMIN_INTERNAL_TOKEN issue --input /run/tianshu/issue-web-actor.json
```

Platform 的 `local` 参数顺序应为 `local --credential-env ... issue --input ...`；`origin.resolve` 是受信 HTTPS 服务端口，不是 CLI 接受任意 scope 的操作。配置生效后 Memory 每次浏览仍用新 origin 重解析并重新检查来源/权限；旧 ref 不会成为长期授权。

## 3. 独立 Knowledge 新库/项目

知识服务是**第二个进程、独立本地 SQLite/guard 与私有配置**，不使用聊天 Memory 的数据库。建议独立目录 `/srv/tianshu/knowledge`（总控审定所有权与备份），配置形状如下；项目 root 必须是总控明确认可的非秘密现存目录，`urls:[]` 不授任何远端来源：

```json
{
  "database_path": "/srv/tianshu/knowledge/knowledge.sqlite",
  "contract_directory": "/etc/tianshu/contracts/text-dialogue/v1",
  "source_sync": {"recovery_path": "/srv/tianshu/knowledge/source-guard.json"},
  "knowledge": {
    "projects": {
      "household-knowledge": {
        "root": "/srv/tianshu/projects/household-knowledge",
        "host": "local", "default_branch": "main", "urls": []
      }
    },
    "clients": {
      "platform-project-reader": {
        "credential_sha256": "REPLACE_WITH_SHA256_OF_PLATFORM_TO_KNOWLEDGE_TOKEN",
        "projects": ["household-knowledge"],
        "permissions": ["query", "document_list", "document_read", "note_query", "lesson_query", "experience_query", "review"],
        "http_read_operations": ["lesson_query", "experience_query"]
      }
    }
  }
}
```

`credential_sha256` 是 Platform 独立知识 Bearer 的 SHA256 十六进制摘要，明文只由总控注入 Platform 环境。没有真实 checkout 时，Memory 不配置 `knowledge.worktrees`，client 不授 `continuation_recover/continuation_check`，也不在 Platform `projects` 加 `checkouts`。`experience_query` 需要同名 permission 加独立 `review`；禁止授 import/delete/写状态/目录 apply 给浏览 client。知识库仅配置项目**不会**生成已初始化项目行：首次经审阅的真实 `import` 等写操作才登记，先期读取应明确 `project_uninitialized` 409，而非伪造空资料列表或导入测试文件。总控确认真实非秘密来源与独立写入身份后再按 Knowledge 正式导入流程授权。

新数据库从 schema 1 先迁移 profiles→sources/schema 3，再执行 Knowledge `migrate`；`migrate` 已安装 lessons、research notes、directories 与 catalog 的新库结构，勿对新库重复执行对应升级命令。每一步须停写、独立**全新**备份路径，guard 与数据库一起保留；这些命令只是准确顺序，**本任务未执行**：

```sh
tianshu-memory --config /etc/tianshu/knowledge.json migrate-profiles --backup /srv/tianshu/knowledge/backups/schema1.sqlite
tianshu-memory --config /etc/tianshu/knowledge.json migrate-sources --backup /srv/tianshu/knowledge/backups/schema2.sqlite
python -m tianshu_memory.knowledge_cli --config /etc/tianshu/knowledge.json migrate --backup /srv/tianshu/knowledge/backups/schema3.sqlite
```

服务启动端口与主机名闭集（绑定 IP、证书/私钥由总控按现场填写，`--host` 不能写 DNS 名；`--allowed-host` 必须与 Platform 请求的 Host 一致）：

```sh
python -m tianshu_memory.knowledge_cli --config /etc/tianshu/knowledge.json \
  serve --client platform-project-reader --host REPLACE_WITH_REVIEWED_BIND_IP --port 8131 \
  --tls-certfile /etc/tianshu/tls/knowledge.pem --tls-keyfile /etc/tianshu/tls/knowledge-key.pem \
  --allowed-host knowledge.internal:8131
```

聊天 Memory 8130 同样要求非 loopback 的 TLS/Host 白名单：`tianshu-memory --config /etc/tianshu/memory.json serve --host REPLACE_WITH_REVIEWED_BIND_IP --port 8130 --tls-certfile /etc/tianshu/tls/memory.pem --tls-keyfile /etc/tianshu/tls/memory-key.pem --allowed-host memory.internal:8130`。CA `/etc/tianshu/tls/ca.pem` 必须验证这两个名称，探针 alive/ready 不代替任何业务读成功。

## 4. Companion 正式角色与 Life 授权

保留现有 `roles."actor:household"={"version":1,"persona":...}`，在 Companion 私有配置中追加 `personas` 与一个**单独**的 Life 只读调用服务。Persona 管理 token 与 Platform `PLATFORM_TO_COMPANION_PERSONA_TOKEN` 是同一值；Life token 与 Platform `PLATFORM_TO_COMPANION_LIFE_TOKEN` 是另一值。Core `services` 里既有 Platform issuer HTTPS 地址/CA、聊天服务设置与 binding 不在此重复；Life 只读 caller 无需伪造 origin：

```json
{
  "personas": {"admin_token_env": "COMPANION_PERSONA_ADMIN_TOKEN"},
  "callers": {
    "platform_life": {"token_env": "COMPANION_LIFE_READER_TOKEN"}
  },
  "life_readers": {
    "platform_life": {"reader_id": "reader:platform-household-life", "actor_ids": ["actor:household"]}
  }
}
```

Persona 初次正式导入可在**停 Core 且数据库 owner 已释放**时用现有 CLI 对当前私有配置做一次 `import`；它只导入已登记 `roles`，初始 seed 发布为可读角色，不创建 Life 世界/角色/日记，也不会覆盖后续已发布人格。命令和无正文状态检查：

```sh
python -m tianshu_companion.persona_cli --database /srv/tianshu/companion/companion.db \
  --config /etc/tianshu/companion.json import
python -m tianshu_companion.persona_cli --database /srv/tianshu/companion/companion.db \
  --limit 20 catalog
```

若服务运行持有 owner，离线 CLI 应返回 `service_running`，不能绕锁写库；也不能用合成角色/测试夹具初始化现场。`personas.admin_token_env` 是管理端口的唯一凭据，当前四个读取也复用它，**不等于** `life_readers`。Companion 可以由 `python -m tianshu_companion.runtime_cli --config /etc/tianshu/companion.json --contracts /etc/tianshu/contracts/text-dialogue/v1 --database /srv/tianshu/companion/companion.db --host REPLACE_WITH_REVIEWED_BIND_IP --port 8765 --tls-cert /etc/tianshu/tls/companion.pem --tls-key /etc/tianshu/tls/companion-key.pem --print-config` 先只打印路径/绑定，再由总控去掉 `--print-config` 启服；这一步不会证明业务授权。

`life_readers` 只登记可读身份与 actor 闭集，**不创建** `life_actors` 或 `life_access`。现有 Core 没有可直接调用的 Life 管理 CLI/HTTP 初始化命令，不能编造 `life init` 或对 SQLite 插行。总控须在停服、独占 owner、审阅真实世界/房间/角色设置与备份后，由受信应用维护入口按 `core.life.create_world(...)` → `create_room(...)` → `configure_actor("actor:household",...)` → `set_diary_access("actor:household",readers=["reader:platform-household-life"],expected=...)` 的正式领域方法完成；已经存在的行要按真实版本 CAS，不重复创建。该维护入口和具体真实值尚待总控落实，完成前**不要**把生活页空态解释为故障，也不要造虚构日记以求页面显内容。日记必须通过正式写作、人工审阅、显式发布；`diaries` 只列已发布，`revision` 只读已发布指针。

## 5. 外部库准备和真实空态验收

安装外部连接目录须先停 Platform、由总控设置 Linux 所有权和仅服务账号/管理员可读写权限，再执行（父目录须已存在，目标目录必须全新）：

```sh
python -m services.platform.external_catalog init /srv/tianshu/external-connections
python -m services.platform.external_catalog check /srv/tianshu/external-connections
python -m services.platform --settings /etc/tianshu/platform.json preflight
```

Platform `preflight` 只读且必须在发布目录/CA/外部库就位后运行；随后总控才可使用既有 `serve` 入口和真实 TLS。外部目录的 `external.sqlite`、`external.key` 与目录权限是一份备份单元，不得单独重建 key；详见 [CONNECT-B-EXTERNAL.md](CONNECT-B-EXTERNAL.md)。管理员网页先 `view` 应显示两类 `configured:false`，保存配置后 `saved_unverified`，只有固定检测真实读取成功才显示 connected。没有已审 AssetLink/HA 端点、CA、实体或私有 CIDR 时保持未配置，不填公网/测试目标。

首次受控业务验收顺序及**诚实空态**：

1. `web_personas` 对正式发布目录启动预检通过；真实 Companion 已导入 `actor:household` seed 后，目录读取应见该 actor 与已发布指针。这里不查看人格正文；若 Core 管理凭据缺失，报告上游拒权，不把角色写进测试库。
2. `web_life/state` 可显示连接已登记；Life actor/剧情授权未建立时，`actors` 可为空，`snapshot` 应为 404；建立真实 actor 与 `life_access` 后 `actors/snapshot` 才能成功。没有经过正式人工发布的日记时，`diaries.items=[]` 是真实空态；不能期望 `revision` 正文。
3. `web_knowledge/state` 显示固定项目与尚未业务验证；新库只迁移/登记配置但未导入真实资料时，读取 `project_uninitialized` 409 是预期。只有经审阅的真实导入完成后才能验目录/查询；没有 checkout 时 continuation 显示未登记，不作复用测试包。
4. `web_memory` 在真实 person/conversation 映射和 scope 精确登记前保持 disabled/`memory_identity_not_ready`；完成正式 `origin.issue`+HTTPS resolve+Memory 登记后，`overview` 可返回 `memory_group_count:0`，这才是经实际业务端口核验的空记忆；缺映射/拒权/来源故障不能变成空集合。
5. `/api/web/connections/view` 的 `connected` 只来自上述业务成功读或外部固定检测回执；进程 alive、TLS 握手成功、配置存在均只算未验证。所有真实读数与回退证据由总控记录，CONNECT-B 没有执行现场操作。
