# 已有 Bilibili 媒体原位登记

`Media.imports` 是 operator Python 维护入口，使用现有媒体 SQLite `documents` 和全局 `media_source` 索引，schema 仍为 2。登记不创建下载任务，不写完成任务、入库回执、集号或订阅成员；不复制、移动、删除旧媒体、NFO、字幕，也不在旧目录创建控制区。

先把已有只读目录加入当前媒体配置，保留其他配置：

```json
{"media":{"import_roots":[{"root_id":"legacy-bilibili","label":"已有B站视频","directory":"/explicit/read-only/existing-root"}]}}
```

`root_id` 是唯一的 1～64 位英数、下划线或短横线。目录必须绝对路径；不能与 staging 互相包含，运行时必须已有且路径各级没有符号链接或 junction。配置校验不创建目录，撤销或重映射根立即使旧复用指针失效。扫描只收普通 `.mp4` / `.mkv`，跳过 `@eaDir`、`#recycle` 和链接；以 `.mp4` 命名的目录本身不会登记成视频。

## 证据 manifest v1

顶层只有 `schema_version: 1` 和 `entries` 数组。每项字段固定：

```json
{
  "schema_version": 1,
  "entries": [{
    "relative_path": "BV1xx411c7mD/P1.mp4",
    "bvid": "BV1xx411c7mD",
    "cid": "123",
    "part_index": 1,
    "size_bytes": 123456,
    "mtime_ns": 1791820800000000000,
    "identity_evidence": {
      "kind": "bili_sync_page",
      "binding": "exact_path",
      "relative_path": "BV1xx411c7mD/P1.mp4",
      "bvid": "BV1xx411c7mD",
      "cid": "123",
      "part_index": 1,
      "source_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    },
    "quality_evidence": null
  }]
}
```

`relative_path` 用 `/` 分隔且不得绝对、反斜线、空组件、`.`、`..` 或盘符。BV、CID、分P可以为 null；CID 有值时是正整数字符串，分P有值时是 1～1000 的整数。`size_bytes` 必须为正整数，`mtime_ns` 为非负整数，并与当前文件一致。

`identity_evidence` 可以为 null；有值时，`kind` 只能是 `bili_sync_page` 或 `metadata_verified`，`binding` 为 `exact_path` 或 `unique_bv_part`，重复的路径、BV、CID、分P须完全匹配外层，且 BV/CID 有值。`source_digest` 为父级保留的原始来源导出/官方 metadata 证据的 SHA256，小写 64 位十六进制。可加 `expected_duration_seconds` 正数（小于 864000）；`unique_bv_part` 必须提供它，产品按已有 Engine 时长规则验证实际文件。

质量未知时始终传 `quality_evidence: null`。不能用分辨率、HDR 标记、NFO、`best`、请求画质上限或不属于该文件的 `format_id` 推定 QN。后续取得可靠实际下载证据时，可把它升级为：

```json
{
  "kind": "actual_download",
  "quality": 80,
  "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "size_bytes": 123456,
  "source_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
}
```

`quality` 为实际数值 QN（1～9999 的整数），`sha256` 绑定当前文件完整字节，`size_bytes` 匹配外层，`source_digest` 绑定实际下载证据。只有 BV、CID、身份和实际 QN 证据都完整，产品才完整哈希并核对，原子写入 `verified` 库存与 `kind=import` 精确源。证据由 operator 对原始来源负责，产品不读取旧下载器数据库或账号。

manifest 不接受调用方填入 ffprobe 数据。产品复用 Engine，一次 ffprobe 保存实际编码、分辨率、帧率、色彩/动态范围侧数据和全部音轨的原始 streams，以及便于查询的 `video_streams`、`audio_tracks`。未知 QN 的库存延迟完整哈希；音视频/时长校验失败保存 `invalid` 和具体原因，能够取得的实际 streams 仍保留。

## 维护调用与查询

调用无需 `Media.start()`，也不会启动下载 worker。下例只展示公共产品方法，配置和 manifest 路径由操作者显式传入，不含生产默认值：

```python
import asyncio
import json
import os
import time
from pathlib import Path
from services.platform.local_work import LocalWork
from services.platform.media.runtime import Media

async def main():
    settings = json.loads(Path(os.environ["IMPORT_SETTINGS_FILE"]).read_text("utf-8"))
    evidence = json.loads(Path(os.environ["IMPORT_MANIFEST_FILE"]).read_text("utf-8"))
    local = LocalWork()
    media = Media(settings, clock=time.time, local=local.run)
    try:
        result = await media.imports.reconcile(
            os.environ["IMPORT_ROOT_ID"], evidence_manifest=evidence
        )
        print(json.dumps(result, ensure_ascii=False))
        inventory = await local.run(media.repo.list, "media_import")
        sources = await local.run(media.repo.list, "media_source")
        run = await local.run(media.repo.get, "media_import_run", os.environ["IMPORT_ROOT_ID"])
        # 按 root_id 筛选 inventory；sources 中 kind=import 是精确可复用来源。
    finally:
        await media.close()
        local.close()

asyncio.run(main())
```

返回字段为 `root_id`、`state`、`discovered`、`registered`、`cid_confirmed`、`verified`、`unknown`、`invalid`、`conflicts`、`cursor`。`completed` 仅表示本次库存扫描结束，绝非视频下载/入库完成回执；有失败项或证据/CAS 冲突时为 `partial`。异常/取消会保存 `interrupted`；进程硬退出可能留下 `running`，重复执行可幂等恢复已登记项。`registered` 是本次扫描成功核对/提交数，含无需改写的已有项，其他计数同样描述本次扫描。

每次扫描整个授权根，manifest 可以只补某一批证据。未变文件保留以前确认的 CID/QN；相同文件身份和证据不重复 probe、哈希或增加库存修订号。原来没有证据的文件可仅从同名 NFO/movie.nfo 获取唯一 BV 提示，CID、QN 仍未知。进度按文件持久化。并发同根维护返回 `busy`；锁只位于 staging 的 `.imports`，取消中的本地复制/哈希/数据库线程结束前锁仍持有。库存和精确源在同一 Repository 事务提交，事务没有文件或网络操作；旧修订不能覆盖新已验证记录。

单条库存可这样读取：

```python
from services.platform.media.imports import item_id
record = await local.run(media.repo.get, "media_import", item_id(root_id, relative_path))
source = await local.run(media.repo.get, "media_source", "import:" + item_id(root_id, relative_path))
exact = await local.run(media.repo.media_sources, bvid, cid, actual_quality_string)
```

库存保存 `root_id/directory/relative_path/bvid/cid/part_index`、`observation`（dev/ino/size_bytes/mtime_ns/ctime_ns）、`media`、`identity_evidence/quality_evidence/evidence_digest`、`identity_confirmed`、`actual_quality/sha256`、`state/code/missing_evidence/revision`。精确源关联库存修订及文件摘要；每次复用重新核对当前授权根、修订、普通文件身份、完整 hash 和前后观测，再进入原 DownloadCache/pin/组包链。pin 来源中保留 `kind=import/root_id/import_record_id/file/sha256/evidence_digest`。硬链接 pin 导致的 ctime 改变不会单独否定原文件，但每次仍校验字节。原 `kind=library` 分支继续核对真实入库 operation/marker。

固定原因包括 `import_invalid_manifest`（400）、`import_evidence_conflict`、`import_observation_changed`、`import_manifest_file_missing`、`import_scope_changed`、`import_source_changed`、`import_unsafe_path`（409），`import_root_not_configured`（409）与 `import_root_unavailable`（503）。库存无 QN 的正常原因是 `import_quality_unknown`；具体媒体失败沿用 `media_invalid/media_audio_missing/media_video_missing/media_duration_invalid/media_duration_mismatch/engine_unavailable`，本地文件失败为 `import_source_unavailable`。源失效时原下载链会尝试其他有效源或重新下载，不能继续盲读已撤销根。

库存会保留已登记历史路径；删除原文件不自动删除库存事实，源复核会拒绝缺失文件。应用数据库备份包含索引和库存，但不包含外部旧媒体卷；恢复后仍需当前配置和原文件验证。

## 本地验证

`test_media_imports` 使用合成 evidence/文件和真实 SQLite，含真实 FFmpeg→ffprobe 单次读取。共享验证选 `test_media_engine`、`test_media_download_cache`、`test_media_runtime`；原 library 来源专项为 `StandaloneMediaTests.test_success_cleans_cache_and_packages_then_reuses_persistent_video_after_restart`、`test_multiple_configured_roots_reuse_source_and_repointed_roots_are_not_read`、`test_corrupt_library_video_redownloads_and_removed_mapping_is_not_read`。

本次没有访问真实旧库、manifest、媒体或 NAS。未知 QN 不能宣称已完成同画质去重；实际登记结果由协调者在发布后独立验收。
