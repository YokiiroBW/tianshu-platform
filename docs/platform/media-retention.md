# 入库后缓存清理与媒体成品复用（TS-127）

直接 Emby/Jellyfin/Plex 目标在任务 completed 且全部服务器 verified 后自动维护缓存。维护失败只记录 pending，不改变已成功入库事实。后台在启动后立即检查历史完成任务，每30秒重试；哈希和文件操作在既有 LocalWork 执行，不阻塞服务启动。无需新增开关、数据库迁移或审批。

## 持久性与并发

复用既有 schema2、documents/download_sources/FileOrganizer 回执及 OS 库锁。documents 新 kind 为 media_source（BV/CID/实际画质及当前目标根映射/operation/视频hash）、media_input（任务generation专属输入意图）、media_retention（维护结果）、deleted_job（列表信息隐藏）。完整备份媒体SQLite和key，索引不会依赖进程内存。

清理先核验 indexed 成品和当前 publication marker、视频长度/SHA256与原实际画质，再提交 media_source；只有该依据可重新读取时才删除 staging。后续任务在全局源锁内优先选择所有当前已配置媒体根的成品，核对directory/library/marker/hash，再在媒体根锁内固定独立输入。被移除/改映射的根不读取；成品不存在或坏hash时回退到有效缓存或重新下载。best/fallback继续使用原引擎确定实际编号，低清不会冒充高清。

新输入为 `.inputs/<job_id>/<generation>/<cid>.<extension>`，先持久 prepared 意图，再硬链接或跨卷复制，验证后记 ready。下载源锁保持到固定输入完成，调用方取消也不会提前释放仍在复制线程使用的源锁。完成代只能清理自己及更早的记录，新的generation和所有尚未核验任务的已记录引用受保护。记录中 `downloads[cid].source={kind:"library",target_id,directory,operation_id,file,sha256,size_bytes}` 可证明真实媒体复用；public_job仍只公开download_reused_cids。

清理精确涵盖已记录的 `.downloads/global/<BV/CID/画质摘要>/<generation>/download.*`、历史 `.downloads/<job_id>/<generation>/<cid>/download.*`、当前任务的已登记输入，以及manifest列出的pkg文件。绝不递归删除整个下载区或媒体根；未知文件保留，已失败/未确认入库的缓存和未合并片段保留。旧失败任务隐藏也只隐藏信息，不清这些片段。其他实际画质的未确认旧输入继续保留，不用当前画质的锁删除它们。

元数据更新与新增P仍经target_head保存的发布基线扩展；隐藏任务的领域记录留在同一jobs台账，内部显式读取。新的包从可验证的媒体视频组装，不需要已清理的旧pkg。直接媒体目标的封面比对读取入库封面；旧远程AssetLibrary目标仍沿用原路径且不会因为没有本地持久成品依据而清缓存。

## 维护检查

正常服务启动即可执行维护，无需手工SQL。协调者在已有Platform实例可调用 `await platform.media.retention.recover()` 立即触发一次；或 `platform.media.retention.finish(job)` 收口一个已完成任务（同步方法应通过media.local执行）。查询 `repo.get("media_retention", job_id)` 查看cleaned/pending与固定原因码，`repo.media_sources(bvid,cid,quality)` 查看已提交候选；`repo.job(job_id,include_deleted=True)`读取保留的领域事实。不能把pending解释成入库失败。

删除经正常已登录Cookie/Origin/CSRF调用 `POST /api/web/media/jobs/delete`，带最新expected_revision和UUID client_id。修改与回执在同一事务；重放不重复修改，同键不同语义冲突。活动/排队/重试等待拒绝删除。仅终止信息可隐藏，公开列表、计数、媒体详情和任务中心均不再展示；发布、成员、集号和成品保持。

应用冷备不包含外部媒体卷及其控制池；外部根必须独立备份/挂载。仅恢复SQLite不能证明成品仍在。恢复后仍逐次核对当前映射、marker与视频hash，缺卷/文件损坏会回退下载；概览缺失目录返回unavailable/null，不读取父盘来冒称挂载成功。

浏览器联合夹具可追加 `--local-server`：真实Platform HTTP、SQLite/FFmpeg文件和FileOrganizer目录，服务器核验使用明确的合成观测，不证明真实Emby/B站。默认参数仍保留旧远程发布夹具。运行命令：`python tests/backend/run_media_fixture.py --local-server --static <构建目录> --port <本地端口> --ffmpeg <工具> --ffprobe <工具>`。
