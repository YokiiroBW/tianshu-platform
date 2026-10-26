# 内容发现（TS-318 独立实现）

此模块为 AL08 的本地候选实现，已装配到 Platform 服务。它提供内置 `tianshu.content-discovery` 插件和只读 `content.discovery/discover` Host 端口，沿用插件实例、Vault、原 HTTP 客户端和原调用账本。没有新的媒体库、正文库、下载器或阅读历史账本。

## 服务装配边界

`service.py` 中的 `PluginManager(..., content_discovery=Discovery(MediaCatalog(media), local=local_work.run, clock=clock))` 是唯一装配点。独立宿主默认不注入时仍只有原五类能力，内容发现插件显示 blocked。manager 的实例、动作和全局投影、execution 的受理和 worker context、原角色桥的 ready 判断均读取 `host.available_capabilities`。

TS316 交付并释放共享入口后，本卡只做上述薄装配和角色桥集合替换，没有新增 HTTP 转发入口。已有实际 Platform、原 AL07 HTTP、真实插件子进程及本地媒体目录的联合测试；外部 SearXNG、账号和 Companion 消费链仍不能据此宣称接通。

调用角色能力端口仍使用 Companion 内部服务身份。模型的 `companion_background` 凭据不属于这一权限，不应转交或扩大 actor_capabilities 的 service 允许集合。

## 搜索服务与可信配置

搜索适配 SearXNG 的 `GET /search?q=...&format=json&pageno=...`。服务端必须在 `search.formats` 中允许 JSON，否则真实协议返回 403；公共实例可能禁用，不能凭 URL 宣称接通。[SearXNG 官方 Search API](https://docs.searxng.org/dev/search_api.html)

实例配置 `base_url` 指向可信的 SearXNG 根地址（可含部署前缀），可选 `api_key` 由原 Vault 注入 Bearer。模型只能提供查询、来源类别、数量和游标，不能选择 URL、账号、私有范围或原始路径。无地址、403、超时或无效结果只影响 web 来源，本地目录仍可返回候选；不自动访问候选页面或执行外站互动。

目录配置由管理员在原插件配置界面维护：

| 字段                                 | 实际授权范围                                                                                        |
| ------------------------------------ | --------------------------------------------------------------------------------------------------- |
| `subscription_ids`                   | 已有 B站订阅的累计成员及已记录 part；不扫描远端                                                     |
| `include_video_library`              | 原视频任务目录的持久 snapshot，不按下载任务状态冒充已经观看                                         |
| `include_books`                      | 现有书籍目录                                                                                        |
| `include_music` + `music_scopes`     | 原 music_source 中明确允许的 scope。空 scope 不默认公开；只有列表明确含空字符串时才允许对应历史导入 |
| `include_comics` + `comic_libraries` | 原漫画目录的 general/private 范围，默认只选 general                                                 |

所有目录默认关闭，来源范围是实例配置的一部分。它们用于授权一个可浏览目录，不要求模型预先知道每个内容 ID。音乐使用获准 source 的元数据，recording 只负责沿原合并关系解析稳定身份；不会借另一私有 source 的元数据补标题。

## 输入和候选语义

动作 `discover` 的输入：`query`（可为空）、`sources`（web/bilibili/music/books/comics）、`limit`（每个来源 1–20，默认 8）、`page`（SearXNG 页，默认 1）、`cursors`（按来源保存上一响应游标）、`exclude_keys`（最多 256 个已有 content_key/candidate_key）。最多五个来源并行，每源有独立期限，整个输出不超过 512 KiB。

每条候选包含真实 owner 身份、`content_key`、版本化 `candidate_key`、来源/类型、标题、作者、取得时间、来源观察时间、发布时间、摘要及摘要类型、owner_version、metadata_digest、read_locator、readability 和 `seen_state=unknown`。未知标题、时间或版本保留 null。过长标题/摘要有明确 `text_truncated`，响应超预算拒绝。

`search_snippet` 是检索片段；`catalog_description` 是原目录描述。metadata_digest 仅为这份描述的版本标识，**不是 Knowledge 原件 hash，也不证明读过正文**。网页 read_locator 保留原 URL，实际 Knowledge 取得和来源校验在其 owner 完成。媒体 locator 保留原 owner 的 ID（音乐另带 source_key），`adapter_required` 表示本卡没有实现跨服务正文传输；不返回本机文件路径、账号范围或凭据。

内容去重只依据原 owner 的稳定身份或安全规范化 URL（scheme/host 大小写、默认端口、空根路径）。保留 query 的顺序、参数和 fragment，不猜它们无意义；同标题不同 URL 不自动认作同一转载。跨网页转载若没有正文 hash 等证据仍无法可靠识别，此缺口属于真实读取后的 owner 证据，不用标题规则填补。

`exclude_keys` 可由后续 AL04 唯一阅读历史权威或调用方已有真实记录提供；content_key 排除同内容全部版本，candidate_key 仅排除该候选版本。本模块不读写 AL04、不保存“已看”、不把搜索命中或选择当亲历。选择/跳过沿调用方既有活动流程处理，没有额外写动作。

## 有界分页与当前性

媒体 Repository 新增 `document_page` / `member_page`，仅使用原表、PK 和 payload reader，没有 DDL。每次只读取一页；removed 订阅过滤保持原规则。游标按最后扫描原行推进，因此过滤后空页仍可继续，查询不触发同请求遍历全库。游标绑定来源、查询和可信配置指纹，只作位置提示，不是授权凭据；每次仍核当前执行授权。

B站订阅一次读一页成员，并最多用一个 128 行原 jobs 页补已有同订阅元数据；成员结束后进入有界的 job 元数据续取阶段，严格保留该订阅范围，因此较旧 snapshot 也能经分页查询找到，无需开启整个视频库。没有 part 的历史成员保持 cid 未知，不造标题/分P。视频目录每次读一个原 job，multipart 在同 job 内续取，不因 action limit 跳过剩余 part。members/member_parts 是累计扫描历史，不证明当前远端可见、已下载或已看。观测时间只取 snapshot 的 captured_at；job 的进度更新时间及订阅最新扫描时间不能证明该项重新出现，缺逐项证据时保持 null。

SearXNG 单页结果多于 limit 时提供同页续取 cursor，并校验该页结果描述的指纹。上游页变化时来源不可用，调用者应刷新并继续使用排除键，不能静默跳项。读完该页后 `has_more=null` 表示不声称知道远端下一页是否存在；调用者可显式增加 page。

读取前后沿原 authorizer 和实例配置 revision 复核。读取途中角色/会话撤销或配置变化时，候选不交给 worker。新端口返回前复用原 redactor，再由原执行器检查插件输出 schema 并保存窄动作结果。同 principal/实例/action/intent 的重试和重启继续原回执，不另发请求。

## 验证与未覆盖

测试为 `test_content_discovery.py`（实际临时 Media Repository、Books/Comics/Music catalog）、`test_content_discovery_plugins.py`（真实插件子进程、Vault、loopback SearXNG）及 `test_content_discovery_platform.py`（真实 Platform 与原 AL07 HTTP）。旧插件及媒体 owner 相关回归记录在本卡 handoff。仅本任务临时 SQLite 与本地 HTTP，没有使用共享 PG 或修改只读解释器。

实际部署搜索实例、真实外站账号、原生 PostgreSQL、新插件配置的浏览器操作与 AL04/AL09/AL10 消费联合均未验。实现可供协调者集中验收，不能把本卡称为自主阅读、长期已看记忆或自主生活已上线。
