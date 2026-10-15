# 音乐订阅底座与网易云登录

网页入口为 `#/subscriptions/5`。本阶段连接网易云账号，将选择的歌单和歌曲资料同步到天枢数据库，提供定时同步、暂停恢复、曲目浏览及音乐目录设置。音频下载、内嵌标签写入、已有文件认领和 QQ 音乐适配尚未接通；网页及接口明确保留“未下载”状态。

## 使用

进入“订阅 → 音乐订阅”，点击“网易云扫码登录”，使用本人网易云音乐 App 扫描并确认。二维码在当前控制台登录中有效，过期可重新生成。登录成功后读取账号歌单，选择同步周期并订阅；首次完整读取成功后，歌单出现在“天枢中的歌单”。

本地歌单包含成员顺序、重复项和暂不可获取的曲目。详情每页 100 首，歌单顺序与专辑曲序分别保存。同步失败保留上次完整结果；远端删歌只更新成员关系，不删除已保存的歌曲资料。断开账号删除其加密会话，已存歌单仍可浏览；重新连接相同网易云主体后可继续同步。

音乐目录可填写当前天枢进程可访问的绝对路径，保存配置不移动或创建音乐文件。“查看目录结构”只读取目录项，最多展示一批艺术家、专辑和文件名样本，跳过 attachments、符号链接及目录联接。默认整理方式为“艺术家／专辑／艺术家 - 歌曲”，也可选择专辑艺术家分组；此处保存后续下载整理所需的设置。

本阶段复用已有启用的 Media 服务，不要求连接音乐媒体服务器，也不增加网易云 API 边车。现有 B 站页面与编号保持，音乐追加为第 5 节。

## 存储归属

继续使用 `<database_path>.media.sqlite` 的 `Repository` 和 schema 2 的 documents 表，没有另建音乐数据库或下载队列。

| 文档类型       | 身份                                     | 内容                                                   |
| -------------- | ---------------------------------------- | ------------------------------------------------------ |
| music_account  | `netease:<user_id>`                      | 平台主体、昵称、状态及加密会话                         |
| music_playlist | `netease:<user_id>:playlist:<source_id>` | 来源资料、有序成员、订阅周期、完整同步时间、状态与修订 |
| music_track    | `netease:<user_id>:track:<source_id>`    | 标题、艺术家、专辑、源曲序、时长及来源可用状态         |
| music_settings | `library`                                | 音乐目录与整理方式                                     |

账号作用域避免私密歌单或用户相关曲目互相污染；歌曲暂缺详情时保留旧的已知元数据并标明当前不可用。文件下载复用键及成品台账留待音频阶段接入现有下载机制。

账号会话复用 Accounts 的 AES-GCM，密钥仍为媒体库的 `.key`。音乐账号已有会话时，密钥缺失会明确失败，不能重新生成一个不可解密旧数据的密钥。公开投影不包含 Cookie、密文或密钥。应用备份应继续一起保存媒体数据库和原密钥。

## 同步与上游

`music/netease.py` 直接连接网易云 HTTPS；`protocol.py` 使用已安装 cryptography 实现接口所需的 weapi/eapi 请求封装。协议依据为 [NeteaseCloudMusicApiEnhanced v4.41.1](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/tree/v4.41.1)，没有引入额外依赖、公共代理或自动跨来源替换。

扫码使用 `/api/login/qrcode/unikey` 与 `/api/login/qrcode/client/login`；资料使用用户账户、用户歌单、歌单详情和歌曲详情接口。QR 服务端最短轮询间隔 3 秒，浏览器正常间隔 5 秒，隐藏页面暂停；180 秒过期。扫码 Cookie 不假定能自动续期，过期时重新扫码。

HTTP 客户端没有共享 Cookie jar，每次请求明确传入当前账号会话；响应 Cookie 不会流到下一账号或匿名请求。上游响应和异常只归一化为固定错误码，不回显凭据或原始错误体。

歌单目录按页完整读取；单歌单先取得全部 trackIds，再分批读取详情，最后复核成员顺序及源更新时间。单次目录最多 10,000 项、歌单最多 20,000 首，超限或不完整会报错，不能把截断结果写成完整快照。单请求 15 秒、完整同步 120 秒预算。

`scheduling.py` 从原订阅循环提取实际共用的扫描逻辑。视频与音乐各自运行同一个循环，由 Media 生命周期启动和取消，慢音乐请求不会阻塞视频扫描。扫描租约仍归 Repository；音乐使用独立身份前缀，入库同时验证租约、账号修订和歌单修订。暂停、撤销、重新登录或另一次修改会拒绝迟到快照。

## 同源接口

所有接口均为 `POST /api/web/media/music/<operation>`，沿用控制台会话、Origin、CSRF 和响应前会话复核。持久命令使用现有幂等回执，`client_id` 为 UUID；配置变更使用 `expected_revision`。

| operation           | 输入                                                                  | 返回                                                               |
| ------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------ |
| view                | `{}`                                                                  | accounts、playlists 摘要、settings、audio_download_available=false |
| accounts/qr/start   | `{}`                                                                  | qr_id、url、expires_at                                             |
| accounts/qr/poll    | qr_id                                                                 | waiting/scanned/expired/ready，成功时附公开账号                    |
| accounts/revoke     | account_id、expected_revision、client_id                              | 已撤销账号                                                         |
| playlists/list      | account_id                                                            | 当前账号完整可访问歌单目录                                         |
| playlists/subscribe | account_id、source_id、interval_seconds、expected_revision、client_id | 首次完整快照及订阅摘要                                             |
| playlists/control   | playlist_id、action=pause/resume、expected_revision、client_id        | 订阅摘要                                                           |
| playlists/scan      | playlist_id                                                           | 立即同步后的摘要                                                   |
| playlists/detail    | playlist_id、offset                                                   | 歌单摘要、有序曲目页、more                                         |
| settings/save       | directory、layout、expected_revision、client_id                       | settings                                                           |
| settings/inspect    | directory                                                             | readable/unavailable、sample_only、目录样本                        |

`layout` 为 artist_album 或 album_artist_album；`interval_seconds` 沿用订阅范围 60–604800。后端保存仍只有一处实现，网页不自行计算或保存第二份成员集合。

## 部署及验证边界

本阶段仅本地交付。用户指定的 Windows 路径为 `\\YokiiroNAS\Music\音乐存档`；已只读抽样确认其艺术家／专辑结构及 FLAC、MP3、LRC 文件。正式容器应填写部署时确实挂载的 Linux 路径。现有 `/media/observe/emby-music` 是观察映射，不代表已获得音频写入挂载；本阶段未修改 NAS、ACL、Compose 或原曲库。

真实网易云接口已验证二维码生成及未扫描状态，未使用本人扫码完成账号登录。完整登录、私密歌单及真实账号成员读取仍需联验。测试中的账号、歌单和登录完成回执均为明确合成；详细结果见 [TS-147 交接](../handoffs/TS-147.md)。
