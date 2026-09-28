# BOT-P HTTP 边界草案（2026-09-28）

本草案用于 BOT-N/BOT-A 本地联合开发；正式跨产品合同由总控发布。既有 `text-dialogue/v1` 的 `qq/tg/web` 是渠道，不是插件名称。`nonebot/astrbot` 仅表示连接运行时类型，不新造 namespace。首次实际发送只允许已登记连接的精确会话。

## 部署登记和管理

平台设置 `bot_connections` 显式列出可选槽位：`adapter` (`nonebot`/`astrbot`)、SDK `platform_id`、机器人 `self_id`、外部会话键、多个 `input_entry_ids`（同渠道同会话、每个预登记作者一条）和允许角色。槽位引用已有 Sources input entry 与 actor entry；未知作者拒绝，不把群成员伪装成户主。`input_entry` 的 owner 是平台专用 service principal，且被选用的 Core `bindings[channel.binding_id].service=platform`。只有**显式选择的绑定**走平台 polling sender，旧 qq/tg 原发送链继续沿用原配置。跨产品登记须部署方一起准备；网页不能临时发明 Core 未登记角色、作者或会话。网页管理员可创建/停用槽位实例、选定角色和允许会话、轮换每实例令牌；实例只能收窄部署登记。没有槽位时页面展示具体登记步骤，两种机器人各有可复制的部署模板。实例默认禁用，凭据一次展示，库内只存哈希。网页调用同源 `POST /api/web/bots/{view,create,enable,disable,rotate,unlock}`，复用 Cookie/CSRF/Origin 与当前管理员会话；管理写操作要求再次密码解锁。列表仅含掩码、状态和最后有效事件时间，不含消息正文、令牌或来源 proof。

## 插件到平台

连接实例通过独立随机 Bearer 凭据访问下列 JSON 端点；凭据只对应一个 `connection_id`，从不获得平台 admin/operator token。平台验证凭据、启用状态、`platform_id/self_id`、精确会话白名单、作者、角色及 Sources 登记后，由服务端使用自己的受限 platform principal 执行 `Sources.register_input` → `Sources.dispatch`。输入去重键为 `(connection_id, SDK event_id, author account_id)`；`revision` 首版固定 `1`，编辑/撤回不支持。相同键和语义只返回首次结果，冲突 409。先持久落 unknown 意图再 dispatch；网络断开或进程中断不得猜测未执行而重发，应查询 status，查询不到也保持 unknown。

- `POST /internal/v1/bot/events`: `{schema_version:1,connection_id,platform_id,self_id,event_id,revision:1,namespace,conversation_id,thread_id,account_id,sent_at,text}`；QQ 外部 `conversation_id` 统一为 `group:<群号>` 或 `private:<用户号>`，与 Core 回执里的 opaque `conversation_id` 不同。文本限定 1–8000 字符。`event_id` 必须来自宿主 SDK 稳定事件标识。成功只表示 Core admission：`{event_id,message_id,state:accepted|not_started|unknown,outcomes:[{actor_id,state}]}`，不代表回复已发送。
- `POST /internal/v1/bot/events/status`: `{connection_id,event_id,account_id}` → `{found:boolean,state:accepted|not_started|unknown|null,message_id:string|null,outcomes:[]}`；仅查本连接，不触发重发。
- `POST /internal/v1/bot/heartbeat`: `{connection_id,instance_id}`；`{state:online,observed_at}`。状态只表示插件近期与平台认证成功，不能代替接入、模型或真实发送验收。

## Core 到平台，再由插件发出

Core 仅对显式选定的平台 polling 绑定使用现有 `POST /internal/v1/conversation/send`，其他旧 qq/tg 绑定继续原发送链。请求及回执仍符合已发布 `conversation#send_request/send_receipt`；身份是 Core 专用 `companion` service token。平台以现有 origin/scope 检查目标、actor、conversation 与当前启用连接，持久化唯一 `(reply_id, semantic_digest)` 和 `(conversation_id,actor_id,turn_id,segment_sequence)`。首次接收只可回 `state=unknown,retry_safe=false,channel_message_ids=[]`，因为尚未由真实 SDK 发送；不得虚构 `sent` 或消息 ID。重复相同请求返回当前持久回执，语义冲突 409。

- `POST /internal/v1/bot/replies/claim`: `{connection_id,instance_id,limit}` → `{deliveries:[{reply_id,attempt_id,namespace,conversation_id,thread_id,text,turn_id,segment_sequence}]}`。这里 `conversation_id` 是外部会话键。只发该连接与白名单内待领回复。领取前 intent 已持久；一个回复只可被一个实例领取。领取后 60 秒未获回执转 `unknown`，绝不自动再次下发。
- `POST /internal/v1/bot/replies/ack`: `{connection_id,reply_id,attempt_id,state:sent|failed|unknown,channel_message_ids:[]}` → `{reply_id,state}`。适配器必须先持久记录自己的发送意图，再调原 SDK；只有 SDK 确认且给出真实消息 ID 才可报 `sent`。同一 attempt 同一回执可重放；已确认终态不同回执冲突。平台因 60 秒租约推导的 unknown 可由同 attempt 晚到的 SDK `sent/failed` 补全；SDK 自报 unknown 则冻结。`unknown` 保守停发，不转可重试。
- `POST /internal/v1/bot/replies/status`: `{connection_id,reply_id,attempt_id}` → `{state:pending|claimed|sent|failed|unknown}`，仅供连接恢复核对，没有正文，不延长租约，也不重新领取。插件有真实 SDK 回执时可以重放同 attempt ACK；没有则保持 unknown，不再发送。
- `POST /internal/v1/conversation/reply-status`: Core 专用 companion Bearer，请求体为**原完整** `conversation#send_request`，响应 `{receipt:conversation#send_receipt|null}`。平台重新校验当前 origin/scope、目标及保存的语义；pending/claimed/不存在返回 null，已有 sent/failed/unknown 返回持久回执。Core 当前 `Sender.reconcile` 总是 `None`，BOT-N 需接入此只读查询；Core 的 30 秒 reconciliation 超时之前有 `sent` 才能确认 sent，之后平台台账仍保留晚到真实 ACK 事实。

## 约束与未决

同一 `(namespace, 实际 bot self_id, 外部会话键, thread_id, actor_id)` 只允许一个启用的 reply owner；Core `binding_id`、运行时类型和宿主 `platform_id` 不参与物理目标判重，防止同一机器人跨 NoneBot/AstrBot 双发。不同实际 `self_id` 的机器人可作为独立发送身份显式共存，部署方需确认同群同角色的多机器人回复确属预期。停用/撤权立即拒新输入与新领取；已领取且可能发出的回复保持 unknown/实际回执，不能承诺撤回 SDK 发送。日志只用连接 ID/固定状态码，不记录正文、账号号值、令牌。HTTP 只接受服务 TLS；本地合成测试可用 loopback。插件不能把 Core admission 当作回复，不可重发 unknown。`AstrBot` 不作为已发布 issuer；平台在受信适配器边界验证后以 `platform` 签发来源。Core 的 30 秒 reconcile 截止只表示 Core 不再等待，平台 60 秒 claim 租约与 SDK 晚到 ACK 是独立事实；网页台账继续显示最终 sent/failed，不自动重发。

联合测试需覆盖：两插件各一条 SDK 文本事件→HTTP admission→真实 Core 结果→平台 claim→原 SDK 发送器替身→ACK→Core reconcile；重复事件、重复回复、双实例竞争、claim 崩溃、ACK 丢失、撤权与重连。实机版本与账号/会话白名单由总控另行指定。
