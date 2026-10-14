# Bilibili 来源目录与稍后再看

订阅继续使用现有扫描、规则、队列、目录快照和发布链路。`destination_mode` 为 `source` 或 `custom`；新版网页的新订阅选择 `source`，字段缺省的旧 API 请求和旧订阅保持 `custom`。旧请求省略 `destination_subdirectory` 时，已有订阅保留原目录；新订阅使用目标根目录。单视频手动入队的现有协议不变。

## 来源目录

所有目录都相对目标的 `directory`，后端不固定 NAS 或容器路径。

| 来源 | 子目录 |
| --- | --- |
| UP 主 | `UP主/{订阅名称} [UID{mid}]` |
| 收藏夹 | `收藏夹/{订阅名称} [FID{fid}]` |
| 合集 | `合集/{订阅名称} [合集{mid}-{sid}]` |
| 系列 | `合集/{订阅名称} [系列{mid}-{sid}]` |
| 稍后再看 | `稍后再看` |

来源 ID 先经已有 `config.source` 规范化。订阅名称中的文件系统非法字符替换为 `_`，空白合并，清理前后空格和句点，设备名增加 `_` 前缀；空名使用“未命名”。名称按 UTF-8 字节截短，整个目录组件不超过 255 字节，来源 ID 后缀完整保留。同名但不同来源 ID，以及同名同 ID 的合集/系列，保持不同目录。

`source` 第一次保存时生成目录并持久化在 `source_subdirectory`；`destination_subdirectory` 保存本次实际使用的相对目录，`destination_directory` 保存目标完整路径。修改显示名称不改变已确定的来源目录，切换到 `custom` 再切回 `source` 也使用原来源目录。切换目标改变根路径。已经排队的任务继续使用入队时的目录快照。

后端目录规则唯一归属于 `media/destination.py`，生成结果继续经过现有子目录、安全路径和目标能力校验。`media_server` 且配置了本地目录的目标支持子目录；当前远端 AssetLibrary 目标支持 `custom` 空子目录，`source` 或非空自定义子目录返回 `409 destination_not_supported`。

## 同源网页协议

`POST /api/web/media/subscriptions/destination-preview` 使用现有网页登录、Cookie、CSRF 与 Origin 校验；返回前复核会话。预览不访问 Bilibili，不创建目录，不保存订阅。

```json
{
  "source": {"kind": "uploader", "id": "946974"},
  "label": "音乐收藏",
  "account_id": null,
  "target_id": "local-library",
  "subscription_id": null,
  "destination_mode": "source"
}
```

必填 `source/label/account_id/target_id`；`subscription_id` 可省略或为 `null`，编辑时为现有订阅 ID。`destination_mode` 可省略，预览缺省为 `source`。`custom` 可提供字符串 `destination_subdirectory`，省略时保留已有订阅路径，新建时为空。已有订阅的来源身份仍不可变。

```json
{
  "destination_mode": "source",
  "destination_subdirectory": "UP主/音乐收藏 [UID946974]",
  "destination_directory": "/media/Bilibili/UP主/音乐收藏 [UID946974]"
}
```

`subscriptions/save` 在原协议上新增可选 `destination_mode`，与预览使用同一解析函数。`source` 以服务器生成或持久化的路径为准，不接受浏览器替换生成路径。旧保存请求缺省模式为 `custom`。原修订号与幂等键语义保留。

`view.capabilities` 增加 `destination_modes: ["source", "custom"]`，`source_kinds` 包含 `watch_later`；每个目标投影增加 `destination_subdirectory_supported`。订阅投影补齐旧条目的 `destination_mode: "custom"` 和空子目录。任务投影继续使用已有 `destination_subdirectory/destination_directory` 字段。

## 稍后再看

来源固定为 `{"kind":"watch_later","id":"self"}`。保存和预览必须提供已登记且 `ready` 的账号，否则 `409 auth_required`。此类订阅的账号属于来源身份，已有订阅更换账号返回 `409 subscription_account_immutable`；另一账号创建独立订阅，从自己的基线开始。同账号更新凭据保留已有基线。

扫描读取 `/x/v2/history/toview` 的完整认证列表。`count` 必须为非负整数，`list` 必须明确存在，成员数量必须与 `count` 相等，BV 不能重复或缺失；条目超过扫描预算时失败。明确 `count=0` 且 `list=[]` 或 `null` 才表示完整空列表。认证失败、缺失列表、截断列表和成员不可读不会确认基线，也不会覆盖此前成员。

`history` 首次完整扫描选择已有成员；`future_only` 首次只建立基线。后续继续复用现有新增 BV/新增分 P、规则评估、累计成员和排队逻辑。列表移除或稍后重新添加成员不会删除已有文件或重复下载。

不同来源目录保留独立队列/发布归属；全局下载缓存继续按 BV/CID/实际画质复用，同卷视频可硬链接，NFO 等附属文件保持独立。历史文件重定位由协调者单独计划和执行，本功能不自动移动已有目录。
