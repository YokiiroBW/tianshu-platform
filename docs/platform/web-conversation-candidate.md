# TS-014 最小接点候选（未发布，不得按此调用）

**本文件旧示例已被同目录 `web-conversation-candidate/*.json` 候选替代，不可按下方旧示例实现。JSON 仍待双方审查与根协调发布。**

基线：Platform f4dac45 / Core 4f22c03 / Memory 575941e。根协调选择 Platform 持久 web sender、Core 有界快照；本文件供根发布独立 web-conversation 合同。

## Core 快照

候选 `POST /internal/v1/conversation/web-snapshot`，仅认证的 platform 服务，使用现行 common query envelope；origin 必须是当前用户对应 actor 的 actor origin，不是 source_input。Core 解析 origin，比较 allowed_scope、web namespace、self_private、actor/conversation/person，并重新核验当前来源权限。不存在与越权不泄漏其他会话。

最小请求：

```json
{"schema_version":1,"query":{"schema_version":1,"request_id":"query:web-1","origin":{"assertion_ref":"origin:actor-a"},"deadline_at":"2026-09-14T06:00:30Z"},"conversation_id":"conversation:web-1","actor_id":"actor:a","after_turn_sequence":0,"limit":20}
```

最小响应（示例是合成数据）：

```json
{"schema_version":1,"request_id":"query:web-1","conversation_id":"conversation:web-1","actor_id":"actor:a","observed_at":"2026-09-14T06:00:01Z","version":4,"next_after_turn_sequence":null,"collector":{"state":"collecting","deadline_at":"2026-09-14T06:00:05Z","revision":2,"messages":[{"message_id":"message:1","text":"你好","sent_at":"2026-09-14T06:00:00Z"}]},"turns":[{"turn_id":"turn:1","turn_sequence":1,"version":3,"phase":"closed","delivery_state":"sent","result_version":1,"messages":[{"message_id":"message:0","text":"早上好","sent_at":"2026-09-14T05:59:00Z"}],"replies":[{"reply_id":"reply:1","segment_sequence":1,"segment_count":1,"text":"早上好。","state":"sent"}]}]}
```

建议调整以直接复用 Core 的现存 phase/collector 字段为准，不为本候选重造状态枚举。collector 可能多个时用 collectors 数组；分页应避免漏掉较早正在更新的轮次：活跃轮次始终返回，历史以 sequence 分页，刷新重取当前页。无完整游标能力先做有界快照，不声称无损事件订阅。

边界例：同 web 账号但 actor/conversation 不匹配当前 scope → common forbidden；已过期或被撤销 origin → forbidden/unauthorized（沿现有解析语义）；请求超期 → deadline_exceeded（按冻结枚举修正）。错误必须沿 common#error 真实 code/current_version（若已有）透传；不把任何失败转换成空历史。

unknown 例：turn phase=closed_unknown / delivery_state=unknown，reply state=unknown，正文不得列为已确认回复，可显示“发送结果未知”；不得自动重发。取消使用现行 conversation#cancel_request/receipt，Platform 从登录映射签 actor origin、从已查询 turn 取 turn_id/expected_version，浏览器不提交 scope。版本冲突保留 current_version 并要求刷新。

## Platform 持久 sender

候选沿用 `POST /internal/v1/conversation/send` 和现行 conversation#send_request / send_receipt，不新增文本wire。仅认证 companion 服务可调用，平台配置显式授予 dialogue send，不复用 asset 权限。Core 根据 destination.namespace=web 选 platform sender；其他仍为 NoneBot。

平台检查 actor origin 对应 actor/conversation、destination 与已登记 web channel 完全一致，且 scope 是 self_private；持久提交 reply_id、turn/segment 元数据、正文和稳定receipt后才回 state=sent、channel_message_ids=[web持久消息ID]。重复完全相同语义复用receipt（request_id按现有命令语义校验）；同 reply_id 不同正文/目标/segment/turn → idempotency_conflict；权限过期/撤销先拒绝，即使存在旧receipt。不接受浏览器Cookie/Origin认证。无外部网络发送。

正常：Core reply:1 → Platform 提交 → sent；丢响应：重启后同请求返回同持久receipt，Core现有unknown策略不擅自改写成sent；跨用户：origin account A 与 destination B → forbidden 且无落库；过期：origin 到期 → forbidden且无落库。Platform公开网页读只按登录映射筛选，不能靠reply_id猜取。

## 网页消费

浏览器只提交 conversation选择ID、actor选择ID、用户原文和去重键；平台检查登记 allowlist，自行生成 physical_input.author/channel、message_key/sent_at，复用 source register/dispatch+inline确认。receipt只显示入站已接收。快照通过当前 actor origin 读取，经白名单脱敏后显示分条消息、5秒合并截止、后台处理、分段回复和版本/unknown。断线只重读；发送超时不生成新键重发。模型未配置沿服务状态显示不可用。登录完成不等于真实模型贯通。
