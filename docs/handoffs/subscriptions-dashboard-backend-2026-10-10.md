# 订阅仪表盘后端交接

基线 `123acfa302cfe27969746584c406b98ecda8d200`，独立 backend worktree；仅修改 media/repository.py、runtime.py、专项测试及媒体 API 文档。无前端、worker、共享入口、依赖或迁移变化。

既有 view 增加 overview：Repository 对全部 jobs 的 state 做 SQL 聚合，不读取作业 body、不受最新100条分页限制；状态分类见 media-api.md。运行层经既有 media.local 线程读取暂存目录所在磁盘，仅 stat 最近存在父目录及 shutil.disk_usage，不创建目录、扫描文件或返回路径。不可读三字节字段为null；未配置overview=null，原字段兼容。

验证使用既有 video-subscription integration 固定 Python：`ruff format --check`、`ruff check` 覆盖两个生产文件与新增测试；`python -m unittest discover -s tests/backend -p test_media_overview.py -v` 9项通过（308条各状态、第一页100/全量137、未创建目录、权限及磁盘错误、阻塞IO离开事件循环、未配置）；既有 `test_media_runtime.py` 14项通过，设置 TS012_CONTRACT_DIR 与合成FFmpeg/ffprobe路径。`git diff --check`通过。

磁盘容量是本服务下载暂存盘，非 Emby/Jellyfin 总库；这些验证使用隔离SQLite及文件系统/容量夹具，无NAS或真实媒体服务操作。由父级串行集成，再与前端联验。
