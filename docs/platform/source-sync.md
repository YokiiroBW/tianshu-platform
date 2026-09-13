# TS-013 来源授权与批量回填

平台实现 source-sync/v1 1.0.0 的真实服务认证、SQLite 事务、`input/current` 读取和 Core HTTPS 回填适配。真实 QQ/网页登录适配器、真实 Core/Memory 联合链、生产部署和完整 L0 均未验收。测试中的 Core 回执服务器是明确的合成 HTTP 替身。

启动绑定根发布提交 `154f068` 的 `contracts/source-sync/v1`，manifest LF SHA256 必须为 `178d0ce66210bdfad4cfb85d8b5f0905b0b67f834e2a530efe5636ff0373633d`；逐项验证本包所有文件、旧 text manifest/五份 schema 和 profile manifest/文件。只执行已核对字节的纯关系规则；认证、当前权限、历史证据和事务由产品代码负责。无 candidate 协商，不改既有 wire/hash。

## 配置和认证

[完整最小合成配置](source-sync.settings.example.json) 可复制到忽略的 `.runtime/`，然后将数据库、合同、证书、私钥和 Core 地址改为本次隔离运行的绝对路径。示例不包含任何 token 值；所有环境凭据须由部署方独立设置，至少 24 个非空白 ASCII 字符，不能重复。不要把配置的服务名、账号或 `trusted_application` 标签理解为已完成外部渠道身份认证。

| 环境凭据 | 实际身份/用途 |
| --- | --- |
| `TS013_APPLICATION_TOKEN` | 已登记应用入口；只能提交其固定 account/channel 下的输入，准备/发送该入口的批量命令 |
| `TS013_CORE_PLATFORM_TOKEN` | Core→Platform；`source.input`，以及固定 platform→companion 的旧 origin resolve |
| `TS013_MEMORY_PLATFORM_TOKEN` | Memory→Platform；`source.current`，以及固定 companion→memory 的旧 origin resolve |
| `TS013_PLATFORM_CORE_TOKEN` | Platform→Core 的单独出站凭据；须在 Core 登记为 platform 服务，Platform 不把它当 Core→Platform 凭据 |
| `TS013_OPERATOR_TOKEN` | 本机操作员；撤销 ref、entry 或 principal |

`input_entries` 是独立于 actor 的物理入口登记：owner、不可变 account、完整 channel（含 namespace/binding/conversation/thread）、audience、TTL、当前 actor entry 列表、默认角色和 routing_version。actor entry 仍使用既有简单 route：入口服务→companion、companion→memory。每个 actor 唯一；默认集合只表达路由意图，不能授权未登记或已撤销角色。修改 defaults 时递增 routing_version。可选 actor entry `expires_at` 表示底层权限到期，区别于一次短期 origin 的 TTL。

当前配置与主体凭据有效性摘要持久化。重新启动同一配置不重置 head；新配置激活后旧进程失败关闭，不能继续用旧进程内权限。部署变更应停止旧服务后串行启动，不支持多个不同配置进程同时管理同一数据库。环境凭据变化、到期、主体/entry 撤销及映射事务被纳入水位；没有热配置公共写入 RPC。

`mode=local_rehearsal` 保留原 loopback HTTP。`mode=service_https` 必须以真实 TLS 启动；不接受明文或 `X-Forwarded-Proto` 冒充 TLS，仍拒绝 Cookie/Origin，尚无浏览器登录会话。

```powershell
.venv/Scripts/python.exe -m services.platform --settings .runtime/settings.json serve --host 127.0.0.1 --port 9443
```

前台 Ctrl+C 停止。证书/私钥使用绝对路径，最低 TLS 1.2；客户端必须信任部署证书链并验证主机名。Core 配置只接受固定 HTTPS origin，路径固定为 `/internal/v1/conversation/ingest-actors`，可选绝对 `ca_file`，不允许 URL 账号、query/fragment、关闭校验、跳转或环境代理。缺凭据、未信任证书、超时或断线均失败；响应限 1 MiB。没有生产 PostgreSQL 适配，`sqlite_local` 始终是显式隔离存储选择。

## 输入到历史回填

[合成请求/回执](source-sync.fixtures.json) 引用正式发布样例，包含应用登记输入、新批量信封、input 查询、Core inline response 和 current 查询。这些示例 ref/deadline 不可直接用于运行，也不是实际 Core 成功证据。测试助手 `tests/backend/source_fixtures.py` 会经平台真正签发 ref，并更新期限及对应回执关联。

1. 已验证外部提交的受信应用调用 `sources.register_input(header, entry_id, physical_input)`。平台再次认证登记 owner，核对 account/channel，持久登记完整 message_key、author、kind、物理正文摘要、ingress 和有效期；同版本不同正文、其他作者、旧版本、墓碑复活均拒绝。未知 edit/retract 失败关闭，须先登记原始输入。返回 `source_input` ref，只证明原作者输入，不是 actor origin，不能用于 origins/resolve 或 Memory identity。
2. 应用把该 ref 放入正式 fanout_request 的 command.origin。`sources.dispatch(header, request)` 在本地 prepare 事务保存完整语义/路由，再发送到固定 Core HTTPS；收到实际 TLS 响应后才进行 confirm。CLI 对应 `register-input` 输入 `{entry_id,input}`；`dispatch-fanout` 输入正式 fanout_request。不要把模型输出转给受信端口当作认证结论。
3. Core 用自己的服务凭据 `POST /internal/v1/source-access/read`，operation 固定 `input`。返回作者/channel/digest/audience 与逐 actor 真正签发上下文；实际 ingress 从先前登记取得，不从 payload 取 caller。首次 actor person/conversation 可 null。Core 使用 actor origin 调用现有 Memory identity，再由 Core 原子产生 P/A 与完整 inline 回执。
4. Platform 确认成功 outcome 完整覆盖冻结目标集合，逐项核对 request/ingest digest、actor、collection author/channel、person/conversation、binding_version、精确 revision、角色/物理 receipt、accepted_origin 和 accepted_at。成功 receipt 不得别名；A+revision 历史不可被第二份回执改写。forbidden 必须双 null。仅在全部核对成功后，同一事务回填 Memory person/binding、Core channel/conversation、全部历史 admission 与当时 entry 快照；故障连同水位回滚。仅物理受理时不虚构 person。

受信应用端口 `sources.prepare_mapping(header, request)` / `sources.confirm_mapping(core_header, ticket, response)` 保留用于真实适配器/测试；后者要求 companion 服务凭据。CLI `prepare-fanout`、`confirm-fanout` 只证明本地服务认证和关联，不证明外部服务已执行；常规真实发送使用 `dispatch-fanout`。没有新增映射 HTTP RPC，也不需要 Core facts 或 Memory SourceAuthority 完成首人回填。

## 路由重试和响应丢失

幂等按实际 ingress 服务、固定操作、command.idempotency_key 保存完整 `{input,target_actor_ids}` 摘要。首次 prepare/input 解析后持久冻结 effective_actor_ids、defaults 和 routing_version；同 key 改正文或目标返回 409。空 defaults 保持 unrouted，显式 A/B 不会吞掉 forbidden B。

同 key 重试可换 request_id、deadline 和新有效 source_input ref，但原始 input/targets 不变。authority 返回该命令首次 defaults/version，actor_contexts 则重新核验冻结集合的当前权限；defaults 改为 A/B 不能让原 A 命令扩到 B。新路由意图必须用新 key。Core 同时保存其 routing_record。

Core 可能已提交而 TLS 响应丢失：Platform 返回 dependency_unavailable，未确认 admission 的 current 仍 503。不要换 key 假装首次执行。经可信入口重登记相同精确输入，使用原 key/targets、新 ref/deadline 调用 dispatch；Core 返回 duplicate 的历史 admission，Platform核对本地保留的当时 actor origin、输入摘要、entry 与当前权限后一次回填。历史 origin 已过期可以保留，当前 actor 授权仍须有效。原输入已被更新/撤回时不再授权旧版本回填。

## 当前权限及旧通路

Memory 凭据使用同一路径 operation=`current`，传入正式 admission 集合和可选 viewer。未知本地历史返回 503，伪造/更改历史返回 403；每个 selector 唯一，响应逐项关联 admission_digest、当时 entry_id/digest、账号、scope、binding_version 和 allowed/denied。

后台 viewer=null 不依赖历史短期 ref 未过期；重新核对当前主体、input entry、actor entry、两个 route、精确 person/channel/scope/binding 及底层到期/撤销。某 actor 撤权只否定该 actor；原始物理版本/分类/墓碑仍由 Core facts 负责，Platform 不冒充 Core owner。在线 viewer 必须解析出完整 scope/ref/期限匹配的 platform issuer、companion→memory 上下文；传输身份 memory 不覆盖业务 caller companion。

既有 text origin/mapping/model-config 端点与 26 项回归保留。旧单 actor ingest 仍用其原 actor origin，不改装成 source_input。旧 observe/current_sources 仍明确是 partial，不能建立完整 admission_history。无法证明当时 entry/admission 的 legacy Core来源在新 current 返回 503，尚未提供 legacy 证据导入/恢复批准工具；不从当前 collector、昵称或 ID 前缀猜历史授权。

## 迁移、备份与恢复

首次启动对既有 TS-012（user_version=0）数据库先用 SQLite backup API 生成同目录唯一 `*.pre-source-<随机值>.sqlite`，包括当时 WAL 数据，随后事务化创建增量表并设 user_version=1；不会改写旧 origins/mappings/configs/observations 为伪新来源。新空数据库无需迁移备份。更高或未知 schema version 拒绝启动。迁移异常不开放服务，保留备份和原失败库供检查；不要重复抹库重试。

备份和数据库同等敏感。真实数据的迁移不在本任务授权范围，本次仅验证临时合成库。维护迁移时先停写、备份并隔离运行；恢复使用 SQLite 完整备份，不能只拷贝正在写入的 `.sqlite` 主文件或混用另一次 WAL/SHM。

head 的 generation/sequence 持久保存在自有库内，相关写入由事务触发器一起推进。普通重启保持不变；故障注入证实回填失败也回滚 head。恢复较旧备份可能回退 sequence；丢失 head/新建库会产生新 generation。**这两种情况都不是自动恢复许可**：Memory 必须按正式包拒绝双 owner 水位回退/换代，保留否定和 suppression；完成受信恢复核验前保持服务链关闭。此版本未实现恢复批准、重建或生产恢复操作。禁止手动把序号改大冒充追平。

## 实际验证

安装沿既有 `requirements-dev.txt` 和 `pyproject.toml`，未修改依赖锁。测试均为临时数据库与合成身份。TLS证书生成使用单独工具解释器的 cryptography，不作为产品依赖、不修改系统证书库；生成的私钥只在临时目录。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
.venv/Scripts/python.exe -m pip install --no-deps -e .
$env:TS012_CONTRACT_DIR = 'C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
$env:TS012_GATEWAY_SRC = 'C:/YOKI/Codex/tianshu-peiban-bot/projects/tianshu-model-gateway/src'
$env:TS013_TLS_PYTHON = 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:PYTHONDONTWRITEBYTECODE = '1'
.venv/Scripts/python.exe -m ruff format --check services/platform tests/backend
.venv/Scripts/python.exe -m ruff check services/platform tests/backend
.venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_sources.py -v
.venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_source_https.py -v
.venv/Scripts/python.exe -m unittest discover -s tests/backend -v
```

本机工具 Python 已含 cryptography 50.0.1；其他主机可使用单独工具 venv 安装该版本并指向它。TLS脚本 `tests/backend/make_tls_fixture.py <已存在隔离目录>` 生成仅一天有效的 localhost 证书，供本地服务示例使用。缺 TS013_TLS_PYTHON 则四项 TLS 明确 skip；缺 TS012_GATEWAY_SRC 则真实网关子链 skip，不能记为全部通过。设置 PYTHONDONTWRITEBYTECODE 防止向只读集成源码写缓存。

验收覆盖原26项、新来源15项和真实TLS4项，共45项；TLS中 Core/Memory调用方仍为合成 fixture，平台是真实服务/认证/事务。真实完整 Core→Memory→Platform 首次身份、同步屏障和外部发送由协调者在各产品提交集成后验收；L0 仍未通过。
