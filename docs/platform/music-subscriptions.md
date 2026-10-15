# 音乐订阅、网易云登录与音频归档

网页入口为 `#/subscriptions/5`。连接网易云账号后，可将歌单和歌曲资料同步到天枢数据库，手动下载整张歌单，或开启同步后的自动下载。音频、封面、歌词和来源元数据按专辑整理；任务进度、取消、重试、删除记录使用现有下载中心 `#/subscriptions/2`。QQ 音乐、历史曲库原位认领和已归档文件标签重编辑未在本阶段实现。

## 使用

进入“订阅 → 音乐订阅”，点击“网易云扫码登录”，使用本人网易云音乐 App 扫描并确认。二维码在当前控制台登录中有效，过期可重新生成。登录成功后读取账号歌单，选择同步周期并订阅；首次完整读取成功后，歌单出现在“天枢中的歌单”。

本地歌单包含成员顺序、重复项和暂不可获取的曲目。详情每页 100 首，歌单顺序与专辑曲序分别保存。同步失败保留上次完整结果；远端删歌只更新成员关系，不删除已保存的歌曲资料。断开账号删除其加密会话，已存歌单仍可浏览；重新连接相同网易云主体后可继续同步。

音乐目录填写当前天枢进程可访问的绝对路径，保存配置不移动或创建音乐文件。“查看目录结构”只读取目录项，最多展示一批艺术家、专辑和文件名样本，跳过 attachments、符号链接及目录联接。默认整理方式为“艺术家／专辑／艺术家 - 歌曲”，也可选择专辑艺术家分组。下载时要求此目录已存在并可写，不能用临时创建的目录冒充丢失的挂载点。

歌单的“下载设置”可选择最佳可用、Hi-Res、无损、极高、较高或标准音质，以及是否接受较低音质。默认无损、允许降级；自动下载初始关闭，开启时会把已有成员加入队列。暂停同步不取消已经排队的任务，取消操作在下载中心完成。远端删歌或暂时不可访问都不会删除本地已归档音频。

“导出 M3U8 / JSON”在音乐根的 `歌单/<稳定歌单标识摘要>/` 下生成两份派生文件。M3U8 使用相对路径，仅包含当次哈希核验通过的音频；JSON 保留所有成员、顺序、重复项与缺失项。导出由用户点击更新，数据库仍是成员关系的权威，不回写远端歌单。

本阶段复用已有启用的 Media 服务，不要求连接音乐媒体服务器，也不增加网易云 API 边车。现有 B 站页面与编号保持，音乐追加为第 5 节。

## 存储归属

继续使用 `<database_path>.media.sqlite` 的 `Repository` 和 schema 2 的 documents 表，没有另建音乐数据库或下载队列。

| 文档类型       | 身份                                     | 内容                                                   |
| -------------- | ---------------------------------------- | ------------------------------------------------------ |
| music_account  | `netease:<user_id>`                      | 平台主体、昵称、状态及加密会话                         |
| music_playlist | `netease:<user_id>:playlist:<source_id>` | 来源资料、有序成员、订阅周期、完整同步时间、状态与修订 |
| music_track    | `netease:<user_id>:track:<source_id>`    | 标题、艺术家、专辑、源曲序、时长及来源可用状态         |
| music_settings | `library`                                | 音乐目录与整理方式                                     |

账号作用域避免私密歌单或用户相关曲目互相污染；歌曲暂缺详情时保留旧的已知元数据并标明当前不可用。新增音频事实仍在同一 documents 表：`music_playlist_jobs` 保存成员到共用任务的引用，`music_stage` 登记精确临时文件，`music_publication` / `music_publication_history` 保存发布意图，`music_artifact` 保存文件哈希与发布回执，`music_export` 记录受管歌单导出目录。

共用 jobs 表增加 `media_kind=audio` 的文档形状，沿用 Repository 的队列、租约代次、状态、事件、幂等回执和取消重试机制，不虚构 BVID/CID。相同账号、网易云歌曲 ID、根目录、整理方式和请求音质共用一个任务；多个歌单只引用它。删除任务记录后仍可根据成品回执校验并复用文件。不同账号或策略目前不跨键复用，已有曲库也不凭文件名猜歌曲身份。

账号会话复用 Accounts 的 AES-GCM，密钥仍为媒体库的 `.key`。音乐账号已有会话时，密钥缺失会明确失败，不能重新生成一个不可解密旧数据的密钥。公开投影不包含 Cookie、密文或密钥。应用备份应继续一起保存媒体数据库和原密钥。

## 同步与上游

`music/netease.py` 直接连接网易云 HTTPS；`protocol.py` 使用 cryptography 实现 weapi/eapi 请求封装。协议依据为 [NeteaseCloudMusicApiEnhanced v4.41.1](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/tree/v4.41.1)，不需要 API 边车、公共代理或跨来源替换。

扫码使用 `/api/login/qrcode/unikey` 与 `/api/login/qrcode/client/login`；资料使用用户账户、用户歌单、歌单详情和歌曲详情接口。QR 服务端最短轮询间隔 3 秒，浏览器正常间隔 5 秒，隐藏页面暂停；180 秒过期。扫码 Cookie 不假定能自动续期，过期时重新扫码。

HTTP 客户端没有共享 Cookie jar，每次请求明确传入当前账号会话；响应 Cookie 不会流到下一账号或匿名请求。上游响应和异常只归一化为固定错误码，不回显凭据或原始错误体。

歌单目录按页完整读取；单歌单先取得全部 trackIds，再分批读取详情，最后复核成员顺序及源更新时间。单次目录最多 10,000 项、歌单最多 20,000 首，超限或不完整会报错，不能把截断结果写成完整快照。单请求 15 秒、完整同步 120 秒预算。

`scheduling.py` 从原订阅循环提取实际共用的扫描逻辑。视频与音乐各自运行同一个循环，由 Media 生命周期启动和取消，慢音乐请求不会阻塞视频扫描。扫描租约仍归 Repository；音乐使用独立身份前缀，入库同时验证租约、账号修订和歌单修订。暂停、撤销、重新登录或另一次修改会拒绝迟到快照。

## 下载、标签与发布

下载使用 `/api/song/enhance/download/url/v1`，歌词使用 `/api/song/lyric`，专辑补充使用 `/api/v1/album/{id}`。只消费当前账号接口返回的完整下载音源；允许降级时依次尝试较低等级。试听标记、时长不符、未知音质、错误大小或 MD5 都不能发布成完整歌曲。CDN 单独使用空 Cookie jar，逐跳验证网易云 CDN 地址，下载 URL 不持久化。单文件最多 1 GiB，并受现有 engine.max_bytes 更小值约束；中断传输从头重新请求，未实现 HTTP Range 续传。

通过现有 ffprobe/ffmpeg 子进程工具确认实际编码、采样率、位深、声道、时长并完整解码。原音源保留编码，不能把有损格式转成 FLAC 冒充无损。支持 FLAC、MP3、M4A（AAC/ALAC），Hi-Res 需有位深或采样率证据。实际等级、编码和降级状态展示于任务和歌单歌曲页。

新增 [Mutagen 1.48.1](https://mutagen.readthedocs.io/en/latest/) 用于在私有暂存文件中写标题、艺术家、专辑、专辑艺术家、日期、曲序、碟号、网易云 ID、歌词和封面；Pillow 把封面归一化为 JPEG。日期及专辑艺术家缺失时不猜值，封面或歌词请求失败记录为警告，不丢失已验证音频。附加文件为同名 `.lrc`、`.zh.lrc`、`.rom.lrc`、`.jpg`、`.source.json`，只生成实际取得的项；JSON 不保存 Cookie 和临时音频 URL。依赖同步固定于 pyproject、requirements-dev 和镜像 runtime.lock（含 wheel SHA256）。

发布复用现有根目录 OS 锁、safe_path、文件复制/fsync。先持久化意图，再在目标卷控制目录准备文件，以原子且不覆盖的硬链接安装最终文件；最终文件与暂存之间无硬链接，写标签不会污染历史文件。遇到同名原文件增加网易云 ID/摘要后缀，绝不覆盖。文件系统不支持硬链接时保留暂存并显示可重试错误，部署时需确认实际挂载能力。

发布中断、回执丢失可核验已有文件继续；取消须等待正在运行的文件操作结算，已落盘内容保留。完成后登记成品，再由同一 Retention 循环回收精确登记的暂存文件。外部修改过的成品不再被当作可复用来源；重试会保留它并生成新的文件。暂存丢失时保留旧意图历史和已有部分文件，重新下载到新路径。失败/取消产生的暂存与部分成品不自动清理，完成后的暂存才清理。音乐发布回执只证明文件归档，不声称 Emby 已扫描收录。

## 同源接口

所有接口均为 `POST /api/web/media/music/<operation>`，沿用控制台会话、Origin、CSRF 和响应前会话复核。持久命令使用现有幂等回执，`client_id` 为 UUID；配置变更使用 `expected_revision`。

| operation           | 输入                                                                                     | 返回                                                              |
| ------------------- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| view                | `{}`                                                                                     | accounts、playlists 摘要、settings、audio_download_available=true |
| accounts/qr/start   | `{}`                                                                                     | qr_id、url、expires_at                                            |
| accounts/qr/poll    | qr_id                                                                                    | waiting/scanned/expired/ready，成功时附公开账号                   |
| accounts/revoke     | account_id、expected_revision、client_id                                                 | 已撤销账号                                                        |
| playlists/list      | account_id                                                                               | 当前账号完整可访问歌单目录                                        |
| playlists/subscribe | account_id、source_id、interval_seconds、expected_revision、client_id                    | 首次完整快照及订阅摘要                                            |
| playlists/control   | playlist_id、action=pause/resume、expected_revision、client_id                           | 订阅摘要                                                          |
| playlists/scan      | playlist_id                                                                              | 立即同步后的摘要                                                  |
| playlists/detail    | playlist_id、offset                                                                      | 歌单摘要、有序曲目页、more                                        |
| playlists/downloads | playlist_id、quality={level,allow_fallback}、auto_download、expected_revision、client_id | 保存下载策略，开启时加入现有成员                                  |
| playlists/download  | playlist_id、expected_revision、client_id                                                | 当前歌单入队或重试；已有成品先核验复用                            |
| playlists/export    | playlist_id                                                                              | 导出目录、文件名、完整成员数和实际音频数                          |
| settings/save       | directory、layout、expected_revision、client_id                                          | settings                                                          |
| settings/inspect    | directory                                                                                | readable/unavailable、sample_only、目录样本                       |

`layout` 为 artist_album 或 album_artist_album；`interval_seconds` 沿用订阅范围 60–604800。后端保存仍只有一处实现，网页不自行计算或保存第二份成员集合。

## 部署及验证边界

本阶段仅本地交付。用户指定的 Windows 路径为 `\\YokiiroNAS\Music\音乐存档`；已只读抽样确认其艺术家／专辑结构及 FLAC、MP3、LRC 文件。正式容器应填写部署时确实挂载的 Linux 路径。现有 `/media/observe/emby-music` 是观察映射，不代表已获得音频写入挂载；本阶段未修改 NAS、ACL、Compose 或原曲库。

底座已对真实网易云验证二维码生成及未扫描状态，尚未使用本人扫码完成账号登录。完整登录、私密歌单、真实账号下载权限、NAS 挂载写入与媒体服务器收录仍需联验。测试账号及远端接口均为合成，音频用 ffmpeg 实际生成并通过 HTTP 传输、解码、写标签和发布；详细结果见 [TS-151 交接](../handoffs/TS-151.md)及前阶段 [TS-147 交接](../handoffs/TS-147.md)。本阶段未部署。
