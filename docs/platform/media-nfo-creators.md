# NFO 实际媒体信息与 UP主导演头像

新版受管下载/元数据刷新保留简介原文和展示覆盖，把 UP主写为标准 `<director>`，协作者继续写 `<actor>`。导演展示名统一为 `昵称（B站 UID MID）`；原昵称、MID、角色和来源链接保存在天枢扩展和 source.json。这样同昵称的 UP主和其他影视人物不会仅按裸昵称归并。UP主不再重复写裸昵称 actor，空姓名不造人物。

纯 sidecars 仍不做文件、网络、数据库操作；`nfo_details` 只把应用已验证的逐 CID 下载/复用结果加入候选。Engine 保存一次真实 ffprobe 的实际观察，下载缓存复用该观察；没有观察的旧 cache/library 成品在已有 pin 后读取 ffprobe。结果绑定每个 CID 的实际 QN、SHA256 和 size，不用 job 最后一个 P 的 actual_quality、当前最大可选格式、分辨率或“最高”设置替代。

## NFO 与 source.json

电影和每个 `episodedetails` 的技术字段层级是 `fileinfo/streamdetails/{video,audio,subtitle}`。tvshow.nfo 不放某一个 P 的技术流。视频写实际 codec、DAR 十进制 aspect、width/height、整数秒 durationinseconds，保留帧率和色彩观察；aspect 优先有效 DAR，无 DAR 时用 width/height × 有效 SAR，再回退方形像素。有效 avg_frame_rate 或 r_frame_rate 转十进制；0/0 等未知不造 0/60。

HDR 优先真实 `DOVI configuration record` → `dolbyvision`，其次 color_transfer=smpte2084 → `hdr10`，arib-std-b67 → `hlg`；其他省略 hdrtype。不从 HEVC、10bit、BT2020 或请求 QN 猜 HDR/DV。所有实际音轨逐个写 codec、channels、实际 language、samplingrate；字幕保留 codec/language。EAC3/TrueHD 不自动写 Atmos，原 profile 和 disposition 等观察仍可查 source.json。

天枢扩展形状如下：provider/bvid/cid 是 source 属性，质量/摘要/大小是其子节点。未知 actual_quality 省略，不写假 QN：

```xml
<tianshu profile="bilibili-creators-v1">
  <creators>
    <creator mid="946974" role="uploader">
      <name>示例UP主</name>
      <url>https://space.bilibili.com/946974</url>
    </creator>
  </creators>
  <source provider="bilibili" bvid="BV1xx411c7mD" cid="123">
    <actual_quality>64</actual_quality>
    <sha256>已校验视频完整SHA256</sha256>
    <size_bytes>实际字节数</size_bytes>
  </source>
</tianshu>
```

source.json 顶层保留原 bvid、简介/标题原始与展示值，并加 `nfo_profile: "bilibili-creators-v1"`。`creators[]` 保留原 MID/name/role，UP主增加 director_name。每个 `parts[]` 保留原 cid/index，选中且已有结果的 P 增加：

```json
{
  "actual_media": {
    "actual_quality": "64",
    "sha256": "已校验视频完整SHA256",
    "size_bytes": 123456,
    "source": {"kind": "library", "operation_id": "真实成品操作ID"},
    "media": {
      "duration_seconds": 2.125,
      "video_streams": [],
      "audio_tracks": [],
      "subtitle_tracks": []
    }
  }
}
```

数组中是真实 ffprobe 编码/profile、分辨率、DAR/SAR、帧率、色彩、DOVI 等 side_data、全部音轨/字幕及其 tags/disposition。未选中 P 的 actual_media 为 null；未知 QN 在 JSON 为 null。来源只保留 kind 及真实 operation_id/import root_id/import_record_id/evidence_digest 等身份，不导出内部绝对文件路径、cookie 或签名下载 URL。当前 BV/CID 加 actual_quality/sha256/size_bytes 共同绑定质量证据，不能只拿脱离文件摘要的一个质量标签判断将来换片后的文件。

技术字段供 NFO/来源事实保存；Emby 的播放格式展示主要来自它自己的媒体探测，不能把“导出有字段”当成 Emby 读取或播放保真的证明。[Kodi NFO 字段](https://kodi.wiki/view/NFO_files/Movies#fileinfo)与[HDR实际检测](https://github.com/xbmc/xbmc/blob/master/xbmc/cores/VideoPlayer/DVDDemuxers/DVDDemuxFFmpeg.cpp)说明这一区别。没有新增高级音轨选择或 FLAC 封装修复。

## 持久头像与 Emby

Bilibili.resolve 从实际 owner.face 取得并沿用已约束的 HTTPS 图片 URL；它放在运行 job 的 creator_images，严格纯 snapshot 的 creator 字段不扩展。缓存按 MID 位于 `Path(database_path).with_suffix(".media-avatars")/<MID>/`，与媒体暂存分离。图像只接受已校验 JPEG/PNG，32 MiB 字节上限及既有尺寸预算，按内容 SHA256 存不可变文件，fsync 后原子落盘。相同 MID/URL 命中时核验缓存字节，复用 fetched_at/revision；URL 变化或 24 小时到期重新获取。旧内容文件保留，避免并发旧回执读者被删文件。成功入库的 staging 回收不触碰这个目录。

现有 `documents` 的 `creator_avatar`，id=MID，ready 记录为：

```json
{
  "mid": "946974",
  "url": "约束后的公共头像URL",
  "state": "ready",
  "code": "creator_avatar_ready",
  "fetched_at": 1791820800.0,
  "file": "Platform持久缓存绝对路径",
  "sha256": "头像SHA256",
  "size_bytes": 264000,
  "extension": "jpg",
  "revision": 2
}
```

失败记录 state=retry，code 为 creator_avatar_source_missing/creator_avatar_invalid/creator_avatar_unavailable/creator_avatar_pending，有旧成功事实时 previous 保留它。锁位于同 MID 的持久控制区，网络和文件均不进入 Repository 事务。取消时尚在执行的本地文件/数据库操作结束前不释放锁；下一次核验/任务重试恢复，不伪称图片 ready。

新版 package 外层记录 nfo_profile（不加入已冻结 media-package manifest）。MediaServers 仍按库、视频 ProviderId 和真实路径匹配 item；新版进一步要求 People.Type=Director、Name=统一 MID 名，且只有一个有效人物 Id。只针对该人物读取/上传图片；不做全局裸昵称搜索，不写 Emby people 目录/内部数据库。已有匹配画像不上传，缺失或变化使用原连接认证 `POST /Items/{personId}/Images/Primary`，body 是 base64 ASCII，Content-Type 为 image/jpeg/png；再 GET Primary，用现有 matching_image 核验。[官方 Emby 上传协议](https://dev.emby.media/reference/RestAPI/ImageService/postItemsByIdImagesByType.html)。

library_results 新增 director_images，其中 mid/name/person_id/state/code/sha256 可查询。图片缺失、身份歧义、失败或读回不同保留 pending/unavailable/mismatch，整体库核验不标 verified，原 worker 的有界重试继续处理。相同导演在同次 show/episode 核验中合并图片观测。Jellyfin 只核新版导演类型/姓名，人物上传明确 not_supported；Plex 仍只核文件路径，不冒称两者导演图片实测。

旧 package 没有 nfo_profile，继续原 People 姓名规则；升级服务不会把旧 actor 成品直接判失败。需通过正常元数据刷新重新生成、发布、核验新版 NFO，不手改 marker、回执或只读历史库。应用备份需同时保留媒体 SQLite 和上述持久头像目录；外部媒体仍需另备份。

## 元数据刷新维护入口

网页 jobs/refresh_metadata 与受信维护使用同一个 MetadataRefresh/Repository 业务。网页保留原网络后 live(session) 复核和 receipt/CAS 顺序，不新增 HTTP/权限。受信维护调用：

```python
job = await media.local(media.repo.job, job_id)
result = await media.metadata_refresh.refresh(
    job_id=job_id,
    expected_revision=job["revision"],
    client_id=fresh_uuid_string,
)
# result={job:公共任务投影,replayed:false}；相同请求重放为replayed:true。
# 此方法仅排队，现有正式 worker 完成生成/发布/核验。
```

原任务若选择 best，单纯元数据刷新沿 base 已验证每个 CID 的 actual_quality 获取原成品，不按目前最大可选格式选择新下载。质量要求明确改变的其他操作继续原画质选择逻辑。缺失/损坏的已验证文件才进入原重下载回退；正常刷新保持视频 inode/hash 与 download_sources generation。

可读维护事实：`await media.local(media.repo.get, "creator_avatar", mid)`；`repo.job(job_id)` 的 creator_images/creator_avatars、downloads[cid].media/source/actual_quality；library_results[].director_images。不启动 worker 的本地维护可只运行 Media+LocalWork 和上述 refresh 方法；正式部署已有 worker 将消费排队结果，不能把返回 queued 当 Emby 已显示完成。

## 精确验证选择

设置既有 TS012_CONTRACT_DIR、MEDIA_TEST_FFMPEG、MEDIA_TEST_FFPROBE；Linux PYTHONPATH=`.:tests/backend`，Windows=`.;tests/backend`：

```text
python -m unittest test_media_nfo_creators media.test_sidecars media.test_purity test_media_engine test_media_imports test_media_download_cache test_media_runtime test_media_servers test_media_standalone -v
```

共 141 项，模块计数 10/37/14/12/16/11/15/4/22。Windows 最终相关行为 140 通过、1 目录 symlink 能力条件跳过。纯测试 test_purity 需要同一提交的 media 源码供 AST 检查，仍检查所有原纯模块及新 creators/nfo_details/probe_details；没有放宽 IO 禁止。真实 FFmpeg、SQLite、回环 Emby HTTP/base64/图片读回和既有完整媒体包 schema 都在选择范围内，真实 Bilibili/Emby/NAS 由协调者另验。
