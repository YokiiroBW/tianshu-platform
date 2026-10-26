# 普通群参与（AL11 实施候选）

普通群文本可进入候选目录，只有经原 Companion 服务认证的 `engage` 决定才尝试领取宿主归属并进入原 Bot 受理链。当前没有 AL12 自动消费者，所以默认仅记录候选，不自动参与。明确触发、观察归档与 ACK 仍走原路径；新合同缺失、pin 校验失败或旧宿主不支持新 RPC，都只使参与不可用。

本实现消费 `observation-participation/candidate-v1`，manifest LF SHA256 为 `347b60a1dde6967adf559597e5bd7dfaa948f5b268d158d04ea3e3ad78b7c826`。候选包放在现有合同根下，不修改已发布 observation-source、Core ingest 或 BotDelivery 协议。它仍是本地实施候选，不能据此宣称真实 QQ、SDK 或 PostgreSQL 已验收。

## 实际入口

以下均为内部 POST；使用现有 `dialogue.send` 动作且实际服务必须为 `companion`。浏览器 Cookie/Origin 不授予权限，模型不能指定地址、凭据或原作者。

| 入口 | 请求 | 返回 |
| --- | --- | --- |
| `/internal/v1/observation-participation/candidates` | `{"actor_id":"actor:example","after":"","limit":20}` | `state/candidates/next`；按返回游标继续，包括过滤后空页 |
| `/internal/v1/observation-participation/decision` | 原 `semantic_participation_decision` DTO | abstain/defer、waiting/unavailable，或原 Bot event 受理状态 |
| `/internal/v1/observation-participation/status` | `{}` | 可用状态、尚未集成的消费者状态和固定错误码 |

候选最长保留 60 秒参与窗口。决定、领取、登记和绑定不延长上游期限；无决定不占宿主槽。临时 `processing` 可由调用方在原期限内重放同一决定；已记录 claim_request 一律重放原参数查询回执。结果未知不换事件、message、claim 或 Core 幂等键。

候选正文直接来自正在处理的真实归档批次，并保留完整事件用于核对原 source_digest。首次人物尚未映射时使用 observed 物理锚；不会为了生成候选创建来源受理、人物或轮次。配置或角色版本变化后旧候选不可重新启用。

## 宿主协议

沿原适配器地址、凭据及地址校验调用 `/tianshu/adapter/v1/participation/`，不在原 poll 或 send 中添加字段。

| 操作 | 请求与回应 |
| --- | --- |
| `capabilities` | `{account_id}` → available、framework、host_epoch、host_generation、manifest_sha256、claim_supported |
| `ownership` | `{native}` → `{found,ownership}`；ownership 是 host_ownership DTO |
| `candidate` | candidate DTO → `{candidate}` |
| `decision` | semantic_decision DTO → `{decision}` |
| `claim` | claim_request DTO → claim_result DTO；原回执优先，历史 grant 与当前有效性分开 |
| `register` | admission_registration DTO → `{registration}` |
| `bind` | delivery_binding DTO → `{binding}`；同一 binding 仅允许 source_confirmation 单调升级 |

`available` 不等于可以领取；只有宿主真实 free 与完成证明才允许新 claim。普通 AstrBot 或未受控宿主不能据“没看见回复”判空闲。适配器的具体完成证明与 SDK 最终检查由 TS320 拥有，平台不冒充宿主 CAS。

## 原受理及恢复

1. 当前候选、决定、群许可和宿主 claim 有效后，沿 `_ensure_reply_scope` 安装真实输入/actor entry；复用原 `_admit_event` 事件键和 message_id，仅选择已决定 actor。
2. `Sources.prepare_mapping` 在原权威事务中保存原 fanout 及参与来源标记。参与登记引用真实 input entry、两种 assertion、input digest、fanout ticket 和原 Core 幂等键。
3. 来源 owner、原 Bot 账本和宿主保存同一完整登记后才调用原 Core。三处不是分布式事务；中断后读取原准备与登记恢复，缺失、异参或过期停止派发。
4. 首次人物由原 Memory identity owner 在正常 Core 受理中解析，Platform 经原 `_confirm` 写入身份及 admission。既有身份不代表本次来源已经确认。
5. 原 BotDelivery 与旧 conversation/send 在各自插入 reply 的同一 Bot 队列事务中写 binding。任何绑定失败都回滚该 reply；不存在先发布 pending 再补关联的窗口。
6. 早到的合法回复可带 resolved 身份和 `source_confirmation=pending`。原本次 fanout 确认后只升级 confirmation，不更换 registration/reply/attempt/bound_at。
7. 原领取 SQL 排序和限量前排除失效/待确认关联。待核 pending 分页轮转，claimed 批次单独重核；仅已证明失效的 claimed 可不占连接，未检查或 RPC 不确定的在途批次仍保留原阻塞。宿主 bind 成功后才给原 claim 本次已核集合；SDK 调用前再次检查原 claim。发送、查询和 ACK 始终使用原队列。

原 Core 派发 deadline 只限制派发及尚未确认的准备。已经由真实回执确认的来源读取仍检查 source/origin TTL、撤销和当前作者/entry/input；不会因为派发 deadline 已过而提前拒绝仍获许可的回复，也不因此延长任一来源期限。

关联记录在已有 Bot 数据库的 `participation_candidates/events/registrations/bindings` 表；来源标记在现有权威库 `participation_sources`，实际来源和回执仍归 `source_inputs/actor_origins/fanout_exchanges/admission_history`。这些是候选及关联记录，不是第二套执行队列或用户记忆。

## 验证与当前限制

本地检查入口：设置 `TS012_CONTRACT_DIR` 后运行 `python -m unittest discover -s tests/backend -p test_participation.py -v`。测试使用真实 Platform、临时 SQLite 和实际 HTTP；Core/宿主响应是明确的合成 peer，不能代替宿主 CAS 或 SDK 测试。精确命令、分组结果和失败历史见 [TS-321 交接](../handoffs/TS-321.md)。

已完成与 TS320 固定提交实际受控 NoneBot 宿主的双进程联合，包含原宿主领取、ACK 后参与、同绑定确认、SDK sender 和未知回执恢复；合成的是 Core/归档服务与 QQ API 返回。可复现 CLI、精确提交和失败历史见交接。AL12 自动消费者、真实模型、QQ 账号、生产迁移与部署未验证。父级独立 PG 收据单列，SQLite 检查不替代 PostgreSQL 实测。未修改 Memory、Companion 源码或用户记忆。
