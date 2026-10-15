# 音视频流策略

`config.quality()` 是链接下载与订阅共用的质量输入。原有 `mode`、`quality_id`、`allow_fallback` 三键仍必填；没有有效高级限制时，归一化结果仍是原三键字典。

可选 `stream_options` 接受部分字段，缺省值如下。未知字段、未知档位、倒置上下限、重复编码和非布尔开关返回 `invalid_input`（400）。全默认选项被省略；有有效限制时，保存补齐默认值的完整对象。

```json
{
  "mode": "best",
  "quality_id": null,
  "allow_fallback": false,
  "stream_options": {
    "video_min_quality": null,
    "video_max_quality": null,
    "audio_min_quality": null,
    "audio_max_quality": null,
    "codecs": [],
    "allow_hdr": true,
    "allow_dolby_vision": true,
    "allow_dolby_audio": true,
    "allow_hires": true
  }
}
```

## 档位与选择

上下限均包含端点。视频档位按下表顺序；HDR、Dolby Vision 为独立档位，受上下限和各自开关共同限制。`allow_hdr` 控制普通 HDR；Dolby Vision（包括使用 PQ 的 DV）由 `allow_dolby_vision` 独立控制。

| ID | 视频档位 |
| --- | --- |
| 16 | 360P |
| 32 | 480P |
| 64 | 720P |
| 74 | 720P60 |
| 80 | 1080P |
| 112 | 1080P 高码率 |
| 116 | 1080P60 |
| 120 | 4K |
| 125 | HDR |
| 126 | Dolby Vision |
| 127 | 8K |

音频顺序为 `30216 < 30232 < 30280 < 30250 < 30251`，分别为 64k、132k、192k、Dolby Audio、Hi-Res。不能用 ID 的数值大小排序。固定 yt-dlp extractor 把音频 ID 放在 `format_id`，视频档位放在 `quality`；Dolby/FLAC 音轨也包含在其实际 `formats` 中。

`codecs` 允许 `av1`、`hevc`、`avc` 的无重复有序子集。空数组沿用自动排序；非空数组既限定允许集合，也指定同视频档位的编码优先顺序。需要下载时先取允许范围内最高视频档位，再按编码偏好选同档视频，音频取允许范围内最高逻辑档位。

`exact` 先在过滤后的真实可用流中寻找指定档位。找不到时，只有 `allow_fallback=true` 才能选过滤结果中的最高档位；回退不跨越上下限、允许编码或特殊流开关。无可用流返回 `quality_unavailable`（503）。下载输出继续核验实际 BV/P、选中档位、音视频、时长和大小；高级策略另核验 extractor 与 ffprobe 的真实编码一致，缺少必要输出证据为 `quality_unverified`，不符合为 `quality_mismatch`（均 503）。

`stream_policy.py` 同时用于格式探针过滤、新下载选择、输出证据和所有复用路径。没有高级限制时保留原 yt-dlp 选择表达式；高级选择使用实际提取的 format ID，避免通过拼装请求数字猜测音轨。

## 复用与持久证据

仍按 `(BV, CID, 实际 QN)` 使用原跨进程文件锁和数据库 CAS；没有以策略摘要另建并行下载锁。已经存在且满足允许集合、上下限和开关的文件优先复用。调整同一允许集合的编码顺序不会触发重编码；仅允许 HEVC 时 AVC 文件不符合。

复用需要完整路径/大小/SHA 校验，策略事实来自已保存的 `stream_evidence` 与 `media.rawffprobe_streams`。前者仅保存所选真实流的档位、编码和特殊流事实，不保存 URL、Header、Cookie 或请求策略。ffprobe 发现的实际特殊流不能被一个旧的 false 值掩盖；多视频/多音轨文件的每条流都遵守排除条件。

只要求当前约束需要的证据。例如只设视频范围时，原本已核验的 `actual_quality` 足够；不要求无关的音频 ID。指定音频范围必须知道真实音频 ID，仅凭 AAC 编码不能猜 30216/30232/30280。缺少所需证据的旧来源不会被当作符合，继续尝试其他有效来源或受控重新下载。未带选项的旧文件保留原复用行为。

有效但不兼容的同 QN 缓存保存为原 schema 2 `documents` 中的 `download_variant`，然后移动当前缓存指针；旧文件不被覆盖。候选搜索涵盖已入库来源、当前缓存、历史有效变体与历史任务输入。`media_source` 保存与实际文件 SHA 对应的证据，入库回收后仍能验证后续策略。导入文件使用原导入台账的 ffprobe 证据。没有数据库迁移。

回收只删除已有对应 SHA 的核验成品所承接的缓存，保留其他编码/封装的有效变体；活动任务继续拥有私有硬链接输入。缓存指针、代际 fencing、取消时文件操作完成后释放锁等原语义保留。内容存储仍按实际 SHA 共用视频硬链接。

## 容器与多分 P

yt-dlp 使用 `mp4/mkv` 容器偏好：兼容时使用 MP4，FLAC 等流使用 Matroska；合并使用流复制，不有损转码。实际 ffprobe 结果用于 NFO/来源信息。

已发布 media-package/v1 明确要求整包单一 `media_extension`。多 P 同时出现 MP4/MKV 时，仅把该任务私有 `.inputs` 中必要的 MP4 用 `ffmpeg -map 0 -c copy` 封装为 MKV；原缓存和已入库文件的 inode 与字节保持不变。目标私有输入先持久登记 `prepared`，完成后记录新 SHA/大小/探针并置 `ready`；所有音轨保留。最终清理包含原输入和新私有输入，后续复用指向成品实际 MKV。没有扩展或违背发布合同。

## 验证与引用

`test_media_stream_policy` 验证归一化、档位、范围、特殊流、实际 extractor 输出及未知证据；`test_media_engine` 包含真实固定 yt-dlp 合并 H.264+FLAC→MKV；`test_media_download_cache` 包含进程/并发去重、不同策略变体、真实 HTTP/SQLite/FFmpeg 入库后复用与多 P 无损统一。既有 imports、hardlinks、import-relocation、standalone 回归继续覆盖原调用方。

固定实现参考：[yt-dlp 2026.08.19 Bilibili extractor](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/extractor/bilibili.py)，以及主工作区只读 `bili-sync-research-20261015/crates/bili_sync/src/bilibili/analyzer.rs` 的档位/音频逻辑排序。这里的合成流与本地 FFmpeg 验证不代表真实 B 站高级会员流或 Emby 已验收；真实账号、部署和实际媒体服务验收由协调者独立记录。
