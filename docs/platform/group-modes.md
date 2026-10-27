# 逐群四态与观察群记忆

本功能沿原账号观察、Bot 连接、来源证明和浏览器入口演进。群/私停用在采集和分派入口结束，不先归档、准备来源或生成模型再丢弃；历史读取与新增处理分别判断。

## 配置入口

打开 `#/settings/3` 的账号级观察策略，逐群选择：

| 状态     | 新消息处理                                                   |
| -------- | ------------------------------------------------------------ |
| 停用     | 不新增采集、整理、回复或主动参与；重新开启不补采、补发       |
| 仅观察   | 归档和整理话题、画像及用户记忆；不回复                       |
| 启用回复 | 在观察上，由真实 @ 或原文字面关键词触发回复                  |
| 主动     | 再允许按角色兴趣判断普通消息参与；仍需真实领取能力和后台模型 |

每个显式启用的群必须绑定已登记角色。关键词每行一个、原文子串匹配，最多 32 个、每个 64 字符，不使用正则。未绑定的默认观察规则只归档，不赋予学习或回复能力。私聊保留原观察/名单策略；关闭观察也关闭回复，不能以白名单绕过。

界面显示主机四态版本、普通消息领取能力和实际参与消费者状态。缺失能力或后台配置显示待确认/不可用；保存意图不冒充主机已经应用。

## 原账号协议演进

既有 observation/v2 的 capability 增加 `group_policy_version:2` 和真实 `ordinary_claim_available`。不支持四态的主机返回 `adapter_incompatible`，意图保留 pending。AL11 的冻结命名空间和 pin 不变。

`group_policy` 为闭集：

```json
{
  "version": 2,
  "default": { "mode": "observe", "actor_id": null, "keywords": [] },
  "groups": {
    "123456": {
      "mode": "reply",
      "actor_id": "actor:example",
      "keywords": ["小天"]
    }
  }
}
```

旧持久策略确定迁移：observe=false→停用；observe_only→观察；whitelist→默认观察/名单回复；blacklist→默认回复/名单观察。绝不迁成主动。API 保存使用 v2；私聊 wire 不变。

同次 apply 的 `group_revisions` 和 `group_default_revision` 是原 owner 的单调代际元数据。仅有效规则变化的群推进；A 群变化不撤 B 群。off→on 不重新使用旧连接/claim。采集 event 的 `scope_revision` 仍是实际账号 host revision，另保存该版完整群策略来核采集时权限。disabled 提交空角色时保留 owner 已知旧绑定；停用不删除旧事实。明确换角色仍按原身份/作用域隔离。

原 `reply_connections` 移除三列 tuple UNIQUE，保留 connection_id 主键和所有历史行；旧 native→Core resolve 不丢失。SQLite 迁移前通过原数据库 backup API 保存 `.pre-group-modes-<uuid>.sqlite`；PostgreSQL 只删除精确三列唯一约束。后者本卡未在真实 PG 执行，发布仍需部署方原冷备/迁移边界。

## 当前处理状态

`POST /internal/v2/observation-source/processing` 复用 service 身份和 `observation.verify` action，仅允许实际 companion/memory。部署需显式登记该既有 action；不由请求自报身份。历史 verify 仍只允许 Memory，语义不变。

请求：`{schema_version:1,request_id,frames:[...]}`，1–100 个不重复项。每项严格二选一：

- `{instance_id,self_id,conversation_id,actor_id}`：实际物理 group:/private: frame。
- `{channel,actor_id}`：原完整 QQ channel；仅从真实 reply_connections、稳定观察 frame 或实际 slot/source entry 解析，不拆字符串猜 owner。

响应同序返回 `{frame,mode,actor_id,processing_enabled,policy_version,code}`。mode/version 不可知为 null；正常 disabled 的 code 为 null；pending、actor_unbound/changed/unavailable 等是逐项结果。`processing_enabled` 只许可开启自动学习阶段，不授发送权。未绑定的观察可继续归档。已证非受管原外部 channel 返回 not_managed、保留其原能力；受管 frame 不明则不可用。主校验/来源、历史删除和授权证明不被替代；已发网络操作仍按原回执恢复。

## 群画像目录

`#/memory/1` 枚举同一观察 owner 的已配置/已发现群与角色、模式、pending。动态条目先调用受信 Core learning-frame 得到真实 canonical conversation，再由原 WEB operator account、WEB channel 和 Origins.read_channels 委托读取；不伪装 QQ account，不猜人物或 Core CID。每次 origin 解析仍核当前 owner、角色和历史读取权。

Memory reader 必须显式启用 TS327 的 `allow_runtime_groups:true`，并满足原真实 WEB account、同 operator person、角色与授权检查；默认 false 仍拒绝。此为部署配置依赖，本卡未改生产配置。

Memory 话题/records 与 Chat 统计分别显示可用性。观察用的 canonical segment_channel 不是独立 Chat 的归档 binding；只有实际已登记静态 Chat 群、其当前映射证明同一 canonical CID 且角色一致，才读取该 Chat channel。缺少准确绑定返回 `chat_group_not_registered`，缺 peer 返回 `chat_not_configured`；不能以零统计冒充已接通。原全历史过滤、邻读、已知遗忘检查和 cursor 绑定保持。

主动判断使用新 function_id `participation`，继承原提供方默认。仅固定 companion_background、group 允许 person_id=null，turn 为真实 candidate ID、会话字段为明确 native routing context，不能声称这是已分配的 Core CID。真实 AL11 source/claim、当前角色和最终发送仍各自核验。
