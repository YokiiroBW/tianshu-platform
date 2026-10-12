# 同来源双目录 Emby/Jellyfin 条目选择修复

基线为当前已部署 Platform `a339df6a44a16ea86999b1ff6dbdc0fee99bff01`，隔离检出 `worktrees/media-live-emby-20261012/platform`，分支 `codex/media-live-emby-path-20261012`。本交接随修复固定提交；精确交付SHA由父级收到的提交回执记录。本次仅本地修复/验证/提交，未操作NAS或远端，真实双来源验收由父级继续。

## 根因与改动

同一BV/CID在两个目录整理后具有相同ProviderIds.bilibili。原MediaServers.verify选择第一条来源ID匹配项，再核对目录，第二目录因此误选第一目录而返回library_metadata_mismatch。

沿用既有manifest与receipt先算每个Movie/Series/Episode的期望完整服务器路径，再按ProviderIds.bilibili及归一后的Path联合选择正确项。只找到其它目录时目标尚未收录，返回pending/library_item_pending，item_id不指向其它目录。命中正确路径后仍执行原标题/简介/作者/图片核验，实际元数据不符仍是mismatch。下载、发布、缓存及Plex行为未改。

## 证据与验证

新增test_media_servers.py使用真实Remote与loopback HTTP响应。覆盖Emby/Jellyfin、single/multipart：两个目录分别选自己条目；首条错误但次条正确；只有其它目录时pending；目标路径正确但元数据不符仍mismatch。旧代码下4测试有12个子断言失败，精确复现首项误选；修复后通过。

解释器为 `C:/YOKI/Codex/tianshu-peiban-bot/worktrees/video-subscription-20261009/integration/.runtime/venv/Scripts/python.exe`；环境与前一TS-123相同：TS012_CONTRACT_DIR指根`.runtime/features-release-20261010/contracts-input/contracts/text-dialogue/v1`，MEDIA_TEST_FFMPEG/FFPROBE指根`.runtime/video-subscription-20261009/ffmpeg/ffmpeg-9.0.2-essentials_build/bin`工具。

- `python -m unittest discover -s tests/backend -p 'test_media_s*.py' -v`：新增4项加原standalone8项，共12通过，零失败/跳过。standalone包含真实FFmpeg视频、文件整理、Emby/Jellyfin HTTP元数据/图片协议与Plex路径回归。
- `python -m ruff format --check services/platform/media/servers.py tests/backend/test_media_servers.py`、相同范围ruff check与git diff --check通过。

生产Emby实例与真实B站视频链路由父级另行验收，不把本地HTTP夹具结果称实机完成。
