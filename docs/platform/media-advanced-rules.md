# B 站订阅高级规则（TS-148）

在现有 `media/rules` 引擎上增加五类条件，继续使用原黑白名单、组内 AND、组间 OR、黑名单优先、正则子进程及有界试算。`schema_version` 仍为 `1`，不修改已发布的 metadata/NFO 合同。

## 字段和比较

| 字段 | 值 | 支持操作 |
| --- | --- | --- |
| `title` / `description` / `uploader_id` / `uploader_name` / `tags` | 非空文本 | `equals` / `not_equals` / `contains` / `prefix` / `suffix` / `regex` |
| `published_at` / `favorited_at` | 带时区的 ISO8601 时间或两个时间组成的数组 | `equals` / `not_equals` / `gt` / `gte` / `lt` / `lte` / `between` |
| `part_count` | 非负整数或两个整数组成的数组 | 同上 |
| `duration_seconds` | 非负有限数值或两个数值组成的数组 | 同上 |
| `is_collaboration` | `true` / `false` | `equals` / `not_equals` |

只有 `between` 接受数组，且必须恰有两个元素、下界不大于上界；范围包含两端。其余操作只接受对应类型的单值，不把文本数字、`0`、`false` 相互转换。

时间格式为 `YYYY-MM-DDTHH:mm:ss[.ffffff]Z` 或带 `±HH:mm` 偏移，最多六位小数，按实际时刻比较。没有时区的本地时间、无效日期和偏移都会拒绝；例如 `2026-10-15T08:00:00+08:00` 与 `2026-10-15T00:00:00Z` 相等。

所有规则继续要求 `case_sensitive: boolean`，仅文本匹配使用它。可选 `negate: boolean` 缺省为 `false`，在字段有实际值、基础比较完成后翻转结果。多值文本的 `equals` 仍为任意一项相等；`not_equals` 为没有任何一项相等，再应用 `negate`。

```json
{
  "schema_version": 1,
  "revision": 1,
  "blacklist": [],
  "whitelist": [{
    "id": "long_joint_uploads",
    "rules": [
      {"id": "length", "field": "duration_seconds", "op": "between", "value": [600, 3600], "case_sensitive": false},
      {"id": "joint", "field": "is_collaboration", "op": "equals", "value": true, "case_sensitive": false},
      {"id": "recent", "field": "favorited_at", "op": "gte", "value": "2026-10-01T00:00:00+08:00", "case_sensitive": false, "negate": false}
    ]
  }]
}
```

未知字段值返回 `result=not_matched`、`reason=field_missing`；`not_equals` 和 `negate` 都不能把未知变成命中。零秒时长和 `is_collaboration=false` 是有值，不是缺失。组内后续规则仍使用原来的短路解释。每条规则 trace 增加 `negate`，仍不包含规则值、标题或简介。

## 来源事实与摘要

`rule_context.py` 集中投影规则事实，网页试算和订阅扫描共同调用：

- 发布时间直接取 `normalize_bilibili` 的 `published_at`。
- 分 P 数量取该视频全部标准化 `parts` 的数量；总时长对全部分 P 求和。任何一个分 P 时长未知时，总时长就是未知，不使用不完整的和。
- 联合投稿优先取 Bilibili `view` 响应的 `staff` 事实：存在实际成员为 `true`，合法空列表或该接口的单人投稿缺省形状为 `false`。旧标准化 metadata 若已有 collaborator 可证明 `true`；没有原始 `staff` 事实时不凭缺少 collaborator 猜 `false`。
- 收藏时间仅取选定 FID 完整扫描结果里该 BV 条目的 `fav_time`。缺失、无效、零值时间均为未知，不使用发布时间、抓取时间、当前时间或浏览器给出的时间。稍后再看的加入时间也不当作收藏时间。

`Bilibili.scan()` 和 `scan_watch_later()` 返回 `dict[bvid, RuleSourceContext]`；每个不可变成员携带 `bvid/source_kind/source_id/favorited_at`。保留原来的分页、可见收藏集终止标记、总数、重复成员、预算和失败校验。订阅持久成员表仍只接收 BV 集合；完整扫描及全部决策成功后才提交基线。没有收藏移除删除视频行为。

旧纯文本规则的政策摘要和 `snapshot_revision` 保持原算法。显式 `negate=false` 与省略该字段具有同一个政策摘要；`true` 纳入摘要。新规则的范围在解析后保存为不可变 tuple，摘要中仍是 JSON 数组。

包含任何高级字段的规则使用以下证据摘要；任务的 `rule_evidence.context` 保存完全相同的扁平 `RuleContext` 字典，可连同已保存的 snapshot 复核：

```python
digest(canonical({"snapshot": snapshot, "rule_context": rule_evidence["context"]}))
```

其中 `digest` / `canonical` 是平台既有帮助函数。不同 FID、实际收藏时间、成员核实结果或联合投稿事实都影响摘要；不把不同来源事实标成同一次试算。旧纯文本沿用 `digest(canonical(snapshot))`。

## 网页试算接口

继续使用 `POST /api/web/media/rules/preview`，原请求 `{preview_id, rules}` 仍有效。可额外传二者之一：

- `source: {kind, id}`：服务器用 resolve 预览登记的账号完整扫描该来源。
- `subscription_id: string`：服务器读取现有订阅登记的来源和账号；它的账号必须与 resolve 预览账号相同，否则 `409 preview_account_conflict`。

只接受来源标识和订阅标识，不接受收藏时间、联合投稿标记或其他浏览器自报的来源事实。预览账号修订在扫描前后复核，既有同源会话在返回前复核。来源超时返回 `503 source_budget_exceeded`，未完成扫描不返回伪造的成员结论。

原 `results` 数组保持，每个 CID 的决策基于同一 BV 全部内容；响应顶层额外提供：

```json
{
  "context": {
    "source": {"kind": "favorite", "id": "123"},
    "member": true,
    "fields": {
      "published_at": {"available": true, "value": "2023-11-14T22:13:20Z"},
      "favorited_at": {"available": true, "value": "2026-10-15T00:00:00Z"},
      "part_count": {"available": true, "value": 2},
      "duration_seconds": {"available": false, "value": null},
      "is_collaboration": {"available": true, "value": false}
    }
  },
  "results": []
}
```

`source=null/member=null` 表示没有选择来源、未核实成员；`member=false` 表示完整扫描证实该 BV 不在选定来源。上例 `results` 省略内容只用于展示新增上下文结构。收藏时间总是属于整个视频在该来源的成员关系，响应不按 CID 重复，避免将总时长和收藏时间误读为单 P 的值。

## 验证入口

沿用后端 unittest 与 ruff：`tests/backend/media_rules`、`test_media_advanced_rules.py`、`test_media_bilibili_scan.py` 和受影响 `test_media_runtime.py`。HTTP 用例复用原登录、真实 loopback 服务、SQLite 和合成 B 站来源；不连接真实账号。

Windows 若使用的 venv `python.exe` 为另起真实 Python 的启动器，会导致原有严格正则握手的子 PID 与启动器 PID 不符。可用该 venv 的 `sys._base_executable` 配合相同 venv `Lib/site-packages` 运行测试，子进程仍执行隔离的标准库 worker。产品没有放宽 PID 校验或更改正则协议。

本轮只做隔离本地开发；真实 B 站、Linux候选、前后端联合浏览器和生产部署由协调者分别验收。
