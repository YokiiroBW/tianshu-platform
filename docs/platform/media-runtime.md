# 视频订阅运行与验证

媒体模块在现有 Platform HTTP 生命周期启动后台订阅扫描和队列 worker，关闭时取消进程树、释放租约并关闭 HTTP/正则子进程。另一个公开监听器不重复启动 worker。任务中心消费同一媒体台账；纯元数据/NFO 继续复用 TS090，订阅规则继续复用 TS098。缺少 media 配置时网页如实显示未配置。

部署示例由集成人维护在 `deploy/platform/media.settings.example.json`。该文件仅含 media 对象，需合并进现有平台配置；替换管理员已确定的目标地址、AssetLibrary library UUID、媒体服务器 library ID、服务器实际 media_root，以及专用 token 环境变量。示例不是可直接用于生产的账号配置。

## 配置字段

media.enabled 默认 true，显式 false 不启动。staging_directory 是绝对私有目录；package_contract_directory 指向工作区正式 `contracts/media-package/v1`，启动核验 manifest.json 所列文件的 LF 归一 SHA256 和 schema。workers 默认2，范围1..4；同目标同 BV 的作业串行持有租约。

engine.python 默认为当前 Python；engine.ffmpeg/ffprobe 为可执行文件或 PATH 名称；timeout_seconds 默认7200（30..86400），max_bytes 默认20GiB（1MiB..200GiB）。固定依赖 yt-dlp==2026.8.19、Pillow==12.3.0，由平台依赖锁及镜像维护。启动检查 yt-dlp 精确版本与工具存在；实际每个视频使用 ffprobe 核对音视频流及来源时长，指定画质核对引擎实际 quality。不得用不同版本的全局下载器替代。

scan_page_limit 默认200（1..1000），scan_item_limit 默认5000（1..50000），scan_timeout_seconds 默认900（30..86400）。扫描必须完整取得来源成员并重核每个投稿 CID；任何分页、预算或访问失败都不推进成员/CID基线。列表变化导致不完整会重试，用户可根据订阅规模调整静态预算。订阅自身 interval_seconds 为60..604800。

targets 为目标列表（最多32），每项 target_id、label、library_id；publisher 为 `{base_url,token_env,ca_file?}`，servers 为 `{server_id,label?,kind:"emby"|"jellyfin",base_url,token_env,ca_file?,library_id,media_root}` 列表。地址与根路径只由静态管理员配置提供，浏览器不能提交它们。token_env 必须是环境变量标识符，缺少令牌会如实失败；AssetLibrary 使用独立 service-publish 凭据，与只读凭据分开。

静态连接允许 HTTPS 及管理员控制的 Compose 内网 HTTP（例如 http://assetlibrary、http://jellyfin:8096），禁止 URL userinfo/query/fragment 和自动重定向。HTTPS 默认系统信任，私有 CA 通过 ca_file 绝对现存 PEM 路径配置，每次请求保持证书与主机名验证。HTTP 的信任边界是管理员配置的服务网络；不能将公网未受保护服务作为该内网配置。没有全局关闭 TLS 验证。

## 数据、发布和恢复

平台数据库旁保存 `.media.sqlite` 及 `.media.sqlite.key`；Cookie 使用独立随机 AES-GCM 密钥加密，浏览器响应、事件和日志不含 Cookie/签名媒体 URL。保持数据库和 key 同步备份；遗失密钥不能猜测恢复账号。部署目录及挂载应仅让服务身份访问。

staging_directory 需要同时作为 AssetLibrary 已信任 inbound 根的只读可见目录（AssetLibrary 自己管理最终库写入）。平台生成 `pkg_<UUID去横线>` 成品目录后才提交 manifest 原始 UTF8 字节；不把绝对路径传给 Asset API。下载临时目录、完整已验证下载及已发布 staging 保留供重启和增量版本组包，旧视频硬链接或复制为完整不可变新树，不能在仍引用时清理。备份包括 SQLite/key/staging；不要只备主平台数据库。

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
