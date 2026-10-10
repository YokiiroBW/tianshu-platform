# 视频订阅后端本地交接

基线6a2ed3d，隔离backend worktree；用户已授权gpt-6.1-sol/xhigh并行实施。此次只写平台后端及对应API/运行文档/测试，前端和依赖锁由各自owner维护，无推送或部署。

已实现B站账号Cookie加密导入/真实二维码协议、四来源规范化及完整成员/CID扫描、TS098规则复用/试算、持久CAS队列和取消恢复、固定yt-dlp独立下载/质量metadata-only探测、ffprobe及图片核验、TS090成品组包、正式AssetLibrary两阶段发布/增量base/质量替换、Emby/Jellyfin全部CID回读、同源网页接口、任务中心投影及健康侧库检查。共享入口薄装配，SQLite仅Repository拥有，网络与引擎进程各有owner。

API与运行字段以docs/platform/media-api.md、media-runtime.md为准；运行示例由集成人维护deploy/platform/media.settings.example.json。订阅仅1P也固定multipart，旧手动single明确拒绝偷偷迁移；纯TS090 renderer新增支持1P剧集，纯IO测试继续精确覆盖原5个纯模块，新IO owner不属于纯renderer的范围。

验证使用integration固定venv Python，TS012_CONTRACT_DIR为根text-dialogue/v1，FFmpeg/ffprobe来自父级核验Gyan9.0.2；命令见运行文档。14项test_media_runtime在本轮先12项通过，1P剧集修复后剩余专项单独通过，另补真实HTTP任务中心投影通过；覆盖大Unicode/JSON转义策略、跨来源/跨订阅计数、失败基线、205任务分页、分P重排/删除/下载期间变化、P2失败补缺、三次增量、质量升级、503有界退避、租约旧代写保护、账号状态事实及加密重启、元数据CAS/原始快照。纯metadata/render 164项通过；既有WebConsole 8项此前通过。既有tasks19项中17项首轮通过，新增源枚举对应两项旧期望已更新且专项复测通过。runtime_health共87项：81项因未设置TS013_TLS_PYTHON跳过，新增sidecar枚举旧期望已更新，全部6项unit复测通过；TLS真实探针未在该命令核验。Ruff及diff空白检查通过。

已用独立TS090生成文件在真实本地Jellyfin12.2/Emby4.10.1对single与multipart四种组合验证，全部视频CID/路径/文本/作者/实际封面取图通过。固定yt-dlp metadata-only实际公开BV13x41117TL得80/64/32/16；自有签名API匿名nav特例已验证。未进行真实账号扫码、个人Cookie读取、实际B站视频下载。浏览器夹具明确用合成上游与发布替身、真实FFmpeg合成/ffprobe；真实AssetLibrary Host/PG联合链由父级接续，不能在此宣称通过。

已完成的下载分P校验哈希后复用；未完成分P在新generation安全目录重新下载，未宣称跨进程复用未验证partial bytes。完整staging/download/key/SQLite需备份并保留增量引用；没有自动清理历史成品。源成员/CID重核受配置总预算约束，预算失败不会推进水位。账号恢复后由用户恢复订阅/重试故障任务。无NAS/生产迁移/远程push/真实用户订阅验收。
