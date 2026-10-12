# 视频订阅运行与验证

媒体模块在现有 Platform HTTP 生命周期启动后台订阅扫描和队列 worker，关闭时取消进程树、释放租约并关闭 HTTP/正则子进程。另一个公开监听器不重复启动 worker。任务中心消费同一媒体台账；纯元数据/NFO 继续复用 TS090，订阅规则继续复用 TS098。缺少 media 配置时网页如实显示未配置。

部署示例由集成人维护在 `deploy/platform/media.settings.example.json`。该文件仅含 media 对象，需合并进现有平台配置；替换管理员已确定的目标地址、AssetLibrary library UUID、媒体服务器 library ID、服务器实际 media_root，以及专用 token 环境变量。示例不是可直接用于生产的账号配置。

## 配置字段

media.enabled 默认 true，显式 false 不启动。staging_directory 是绝对私有目录；package_contract_directory 指向工作区正式 `contracts/media-package/v1`，启动核验 manifest.json 所列文件的 LF 归一 SHA256 和 schema。workers 默认2，范围1..4；同目标、同子目录、同 BV 的作业串行持有租约。不同目录的整理可以同时推进，实际相同视频下载共用全局占用。

engine.python 默认为当前 Python；engine.ffmpeg/ffprobe 为可执行文件或 PATH 名称；timeout_seconds 默认7200（30..86400），max_bytes 默认20GiB（1MiB..200GiB）。固定依赖 yt-dlp==2026.8.19、Pillow==12.3.0，由平台依赖锁及镜像维护。启动检查 yt-dlp 精确版本与工具存在；实际每个视频使用 ffprobe 核对音视频流及来源时长，指定画质核对引擎实际 quality。不得用不同版本的全局下载器替代。

scan_page_limit 默认200（1..1000），scan_item_limit 默认5000（1..50000），scan_timeout_seconds 默认900（30..86400）。扫描必须完整取得来源成员并重核每个投稿 CID；任何分页、预算或访问失败都不推进成员/CID基线。列表变化导致不完整会重试，用户可根据订阅规模调整静态预算。订阅自身 interval_seconds 为60..604800。

targets 为目标列表（最多32），每项 target_id、label、library_id；publisher 为 `{base_url,token_env,ca_file?}`，servers 为 `{server_id,label?,kind:"emby"|"jellyfin",base_url,token_env,ca_file?,library_id,media_root}` 列表。地址与根路径只由静态管理员配置提供，浏览器不能提交它们。token_env 必须是环境变量标识符，缺少令牌会如实失败；AssetLibrary 使用独立 service-publish 凭据，与只读凭据分开。

静态连接允许 HTTPS 及管理员控制的 Compose 内网 HTTP（例如 http://assetlibrary、http://jellyfin:8096），禁止 URL userinfo/query/fragment 和自动重定向。HTTPS 默认系统信任，私有 CA 通过 ca_file 绝对现存 PEM 路径配置，每次请求保持证书与主机名验证。HTTP 的信任边界是管理员配置的服务网络；不能将公网未受保护服务作为该内网配置。没有全局关闭 TLS 验证。

## 数据、发布和恢复

平台数据库旁保存 `.media.sqlite` 及 `.media.sqlite.key`；Cookie 使用独立随机 AES-GCM 密钥加密，浏览器响应、事件和日志不含 Cookie/签名媒体 URL。保持数据库和 key 同步备份；遗失密钥不能猜测恢复账号。部署目录及挂载应仅让服务身份访问。

历史 AssetLibrary 目标的staging_directory需作为对端已信任inbound根只读可见（对端管理最终库写入）；该路径继续保留发布缓存。直接媒体服务器目标生成`pkg_<UUID去横线>`，核验入库后从持久成品复用并精确清理成功缓存，失败/未知及活跃引用保留。完整生命周期、恢复与外部媒体卷独立备份见[媒体缓存维护](media-retention.md)。不要只备主平台数据库。

每次发布 prepare→publish→GET 查询，同键恢复未知响应；成功 receipt 持久后才进入媒体服务器核验。新增 P 使用当前 owned base_operation_id/base_version，保留旧 CID/集号/路径/hash；显式质量变更 additionally replace_video_cids 允许对应视频字节更新。展示元数据修改同样发布新版本，原始快照及覆盖分离。AssetLibrary 负责完整目录备份和发布 journal，平台不直接写最终媒体根。

普通失败最多5次自动重试，30秒开始指数退避最高1800秒，耗尽后failed且可手动恢复。发布仍排队属于正常观察，不消耗错误重试额度；媒体库核验最多20次（30..300秒），之后failed但保留已发布receipt，手动恢复仅继续核验。完整已下载CID校验哈希后复用，尚未完成的CID补下；过期lease旧worker不能写新generation。平台停止和用户取消均终止 yt-dlp/ffmpeg 进程树；已发布操作无法撤销时显示unknown，保留事实供恢复。

媒体服务器回读按原 NFO 的完整 bilibili provider key 核对 root 和全部所选 Episode、目标库相对路径、展示标题/简介与作者；封面有界下载32MiB后解码比较，允许同宽高比的转码/缩放，64×64 RGB平均误差≤12/255且超40误差的通道≤15%。配置多个服务器需全部通过才completed，未配置服务器时published。

## 本地运行验证

正常启动使用既有 `python -m services.platform --settings <现有完整配置> serve --host <绑定地址> --port <端口>`（详见部署 README 的TLS入口要求）。没有自动读取个人账号或生产配置。前端同源 API 详见 [media-api.md](media-api.md)。

订阅首个只有1P的投稿也固定multipart剧集布局，后续新P保留旧路径及集号。手动链接的原单P可用single布局；若同目标已有single成品后改为订阅，明确显示layout_change_requires_migration，不偷偷覆盖其路径。

专项：设置 TS012_CONTRACT_DIR 为工作区 `contracts/text-dialogue/v1`、MEDIA_TEST_FFMPEG/MEDIA_TEST_FFPROBE 为本地已安装工具，然后执行 `python -m unittest discover -s tests/backend -p test_media_runtime.py -v`。用真实 HTTP、合成 B站响应、合成发布替身和真实 FFmpeg/ffprobe，明确不证明真实 B站下载或 AssetLibrary 发布。

浏览器夹具：`python tests/backend/run_media_fixture.py --static <apps/web/dist> --port 18898 --ffmpeg <ffmpeg> --ffprobe <ffprobe>`，启动时打印仅本地合成 upstream control URL；网页登录使用 tests/backend/web_fixtures.py 的合成身份。控制请求可设 qr_state 为waiting/scanned/ready、members、parts、description；QR ready只生成合成Cookie。夹具封面也是本地生成PNG，不能称真实扫码或真实媒体发布。

真实本地媒体服务器验证脚本：`python tests/backend/run_media_servers_integration.py --fixtures <合成服务连接JSON> --documents <纯元数据合成文档JSON>`，凭据从本次生成文件读取到临时 env，不打印。已用真实 Jellyfin12.2/Emby4.10.1 对single/multipart四种组合通过核验，源文件来自独立TS090 NFO输出。固定yt-dlp metadata-only实际公开测试 BV13x41117TL 可得80/64/32/16，无账号或视频下载。

本地合成验证、联合真实本地Host验证和NAS部署/真实账号/用户使用必须分别记录。未执行NAS部署，未读取个人账号，未声称真实用户订阅已验收。

## 全局下载源与独立目录（TS-123）

`destination.py` 统一相对目录校验、任务路径快照及目录身份；旧空目录的队列摘要与 target_head 键保持原身份。发布清单仍用现行合同，子目录作为平台内部整理参数保存在 package 外层；file_transfer 的 relative 及 receipt.target_relative_path 带完整子目录，服务器在自身 media_root 下映射同一相对路径。修改订阅不会挪动或删除旧文件。

媒体 SQLite schema 为2，新增 `download_sources`，启动读取v0/v1旧库前生成 `.pre-media-<UUID>`备份；jobs、documents、receipts等原事实保持。探针需接受媒体schema2（由集成者维护共享探针入口）。`download_cache.py` 按 BV/CID/实际画质编号保存验证过的下载源；旧 jobs.downloads 的有效记录按需吸收，包含发布失败或中断任务已经完成的 CID。每次复用要求文件存在、路径无链接、扩展名一致、非空且大小和 SHA256 匹配，损坏或缺失源失效后重新取得，不将临时文件算成功。

先取得相同下载键的跨进程 OS 文件锁，再通过 Repository 领取 owner/generation/30秒租约；每5秒续全局与任务租约。OS锁阻止仍在下载但暂时失去心跳的进程被并发接管；进程退出自动释放锁，租约过期后接管使用新generation目录。用户取消终止引擎进程树并释放占用；等待者可取消，正常等待不增加失败重试次数。只有完整验证且成功提交的源才是ready，迟到旧generation不能写回。整理根锁争用在既有 Publications 中可取消地等待，仍由worker维护任务租约。

成品包视频优先复用已验证媒体成品，再查全局下载源；直接整理目标在自己的 `.tianshu-transfer/videos` 中保留按SHA256和长度验证的无扩展名共享文件。staging与媒体目标跨卷时首次复制一份到该目标卷，后续目录链接这一份；文件系统不支持链接时安全复制并报告实际模式。NFO/source/封面分别输出。服务从不原地改写已经验证的源视频，修订使用新文件和新目录，incoming恢复仍通过共享源安装。外部原地修改硬链接视频会影响所有引用，后续复用和整理核验会拒绝已变内容。TS-127按[缓存维护](media-retention.md)清理成功任务的staging；未确认下载和previous目录保留。

相关验证沿用 unittest：`python -m unittest discover -s tests/backend -p 'test_media*.py' -v`。新增全局源用例计数真正的 download 调用，覆盖两个独立进程和实例、租约/取消、旧记录/重启、损坏/缺失、best/exact/fallback；standalone夹具用真实FFmpeg视频完成两个中文目录、第二目标、独立NFO和订阅改目录/新增P。它们不证明真实B站账号下载或NAS大文件性能。

## 媒体卷内硬链接与既有成品转换（TS-126）

共享视频职责在 `video_storage.py`，不新增下载账本、数据库schema或公共合同。FileOrganizer在既有库锁内写控制区临时文件，复验长度/SHA256、fsync后原子安装；命中共享源仍复验。控制文件及临时文件没有媒体扩展名，不生成额外NFO或媒体条目。不同字节内容的画质拥有不同摘要，不串用文件；相同字节内容可共用同一inode。损坏的池文件不作为命中，可从已验证源原子换成新inode，不就地修补已发布视频。`previous-*`不自动清理。

池写入临时路径按该视频SHA256/长度确定，成品安装临时路径再绑定精确目标身份；进程强制终止后重试只处理当前条目的保留路径。已完整复制的临时池文件经核验直接恢复安装，不再跨卷复制；不完整残留清理后重建，命中既有共享源时也清理该条目残留。安装临时链接留在控制区，目标已安装的重试仍处理自己的残留，不扫描其它条目或执行全库GC。

直接整理回执新增结构化 `video_storage={mode:"hardlink"|"copy"|"mixed",files:[{path,mode:"hardlink"|"copy"}]}`，由实际文件inode关联观测，复制降级不声称存储去重。网页现有字段保持；目录输入首段 `.tianshu-transfer`（不区分大小写）属于内部控制区，报 `invalid_destination_subdirectory`。

上线后的明确维护入口：

```python
target = media.config["targets"][target_id]
result = await media.local(media.publisher.organizer.deduplicate, target, operation_id)
```

同步脚本也可直接 `media.publisher.organizer.deduplicate(target, operation_id)`。每次只处理一个明确operation，不扫描整个库。操作必须indexed、有回执，且当前目录的marker、全部manifest文件哈希和自有文件集合一致；旧版本marker或外来文件拒绝转换。仅视频以控制区临时硬链接+原子replace转换，元数据/marker及job、operation/version、package、清单身份保持。转换成功后才更新file_transfer回执的video_storage观测；失败不记假成功，可重复调用恢复，已链接时不重写。文件系统不支持硬链接时保留现有有效复制，返回mode=copy。返回 `{operation_id,video_storage,receipt}` 不含私有绝对池路径。

该维护方法沿用非阻塞库锁，实际被占用时报busy；协调脚本在现有媒体worker暂不写该库时逐一调用，不在锁内嵌套另一个Organizer方法。媒体文件删除一处不会破坏其它硬链接；外部媒体目录继续按既有挂载和备份规则管理。

## 独立资产服务整链验证

`tests/backend/run_media_asset_integration.py --asset <隔离Asset连接JSON> --servers <隔离媒体服务器JSON> --work-directory <本次私有输出目录> --ffmpeg <工具> --ffprobe <工具>` 用实际平台HTTP/SQLite、AssetLibrary HTTPS/PostgreSQL/文件系统，以及真实Emby/Jellyfin完成两P任务。B站响应和视频内容明确使用合成夹具。它会向所指定的隔离库写测试成品，并注册测试媒体库，不得传生产配置。每次使用空的隔离资产库或通过`--bvid`传入未使用的合成BV。

Asset JSON包含`base_url/token/library_id/staging_directory/library_directory/ca_file?`；servers JSON按`emby`和`jellyfin`分别包含`base_url/token/version`。只使用测试过程中生成的凭据，脚本不打印令牌。输出`joint-*/evidence.json`记录最终任务、资产回执与逐服务器结果。2026-10-09集成人实际完成一次两P整链，全部条目有entry_id，两台服务器均verified，任务completed；原始证据位置见工作区本批交付记录。
