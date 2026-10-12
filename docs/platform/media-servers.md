# 媒体服务器与订阅流程

天枢负责网络视频订阅、下载与文件整理，Emby、Jellyfin、Plex 负责媒体库索引、展示和播放。没有内置的天枢媒体库，也不要求 AssetLibrary 中转。未添加媒体服务器时目标列表为空，其余天枢功能不依赖媒体服务器在线。

## 网页配置

在“订阅 → 账号与媒体库 → 添加媒体服务器”选择 Emby、Jellyfin 或 Plex，填写地址及 API Key / Plex Token。可测试连接并读取服务器已有的媒体库，也可以直接填写库 ID；测试不是保存配置的强制前置步骤。

选择目标库并填写两端目录：

- **整理目录（天枢可见）**：例如 `/media/bilibili`，必须是已经挂载到天枢容器的可写目录。
- **对应目录（媒体服务器可见）**：例如 `/library/bilibili`，应当是该媒体库扫描范围内的同一份文件。容器路径可以不同，但宿主机文件必须相同。

新连接立即供链接下载和订阅选择。配置存入既有媒体台账，重启恢复；密钥复用既有 AES-GCM 密钥文件加密，不回传网页、不进入请求回执。编辑时密钥留空保留原值，更换服务器地址须填写对应密钥。已有任务的目录/库映射不能改指其他文件，以便旧任务继续修订或恢复；可新增目标用于新任务。未使用连接可以移除，移除不会删除媒体文件。

Plex 使用 X-Plex-Token 请求头，通过 `/library/sections` 读取媒体库，刷新目录并分页核对实际媒体文件路径。Plex 完成表示**文件已被收录**，不宣称其原生解析了全部 NFO；标题/简介/封面展示取决于该服务器元数据代理。Emby/Jellyfin 沿用原刷新、来源 ID、文件路径、标题、简介、作者及封面核对流程。服务器离线/扫描尚未完成仍显示真实待核对或失败，可从已有文件继续重试，不重复下载视频。

## 订阅闭环与参考

参考 [MoviePilot](https://github.com/jxxghp/MoviePilot) 的订阅→下载→整理→媒体服务器刷新分工，以及 [MoviePilot 环境说明](https://wiki.movie-pilot.org/ready) 的外部媒体服务器和共享目录模式；Plex 协议参照 [官方 URL 命令](https://support.plex.tv/articles/201638786-plex-media-server-url-commands/)。

Easy-vdl 参考主工作区已有 `media-subscription-feasibility-2026-09-19.md` 调研，以及本机恢复版本的来源类型、增量扫描、队列与元数据设计。本轮公开仓库页面读取返回404，不冒称核验了最新版本；不引入或运行其恢复源码。

当前 B 站流程复用现有模块：

1. 收藏夹、合集、系列或 UP 主订阅；选择仅追新或补历史、扫描间隔、画质和过滤规则。
2. 完整扫描后提交来源成员及分 P 游标，不把中断分页当作扫描完成。
3. 按 BV/CID、目标和画质去重；跨订阅复用已排队内容。新增分 P 保留稳定集数，画质调整按既有修订流程处理。
4. 复用下载引擎、部分成功文件和 NFO/封面生成；只整理经大小及摘要核验的成品。
5. 直接整理到所选媒体服务器的共享目录，再触发扫描并核对结果；浏览器关闭不影响后台任务。
6. 持久任务和文件转移回执支撑中断重启、幂等重试和元数据修订，页面保留分阶段状态与原因。

本次没有增加 MoviePilot 服务依赖，也没有宣称已实现其 PT 搜索、影视缺集、站点维护或 Easy-vdl 的全部多平台功能。

## 部署配置与既有目标

媒体服务仍由原 `media.enabled` 配置启用，省略 targets 或 `targets: []` 不再自动生成内置库。也可通过部署配置管理直接整理目标：

```json
{
  "media": {
    "enabled": true,
    "targets": [{
      "target_id": "network-videos",
      "label": "Jellyfin 网络视频",
      "kind": "media_server",
      "directory": "/media/bilibili",
      "servers": [{
        "server_id": "jellyfin",
        "kind": "jellyfin",
        "base_url": "https://jellyfin.example.invalid",
        "token_env": "TIANSHU_JELLYFIN_TOKEN",
        "library_id": "EXISTING_LIBRARY_ID",
        "media_root": "/library/bilibili"
      }]
    }]
  }
}
```

示例为占位配置。部署管理的目标由配置文件维护；网页管理的目标单独保存到同一媒体台账。原有带 publisher 的 AssetLibrary 目标只为既有任务兼容保留，不影响新直接整理目标，不自动搬迁旧文件。

## 文件整理与恢复

`media/organizer.py` 只负责文件转移，不提供内部媒体目录、播放或资产索引服务。转移使用现有中立 media-package/v1 清单；`file_transfer` 文档复用既有 SQLite documents 表，无新 schema 迁移。历史字段 asset_operation/asset_receipt 保留便于读取原任务；新回执 storage=server_directory。

先写同一目标卷的临时副本，校验、落盘后切换目录；文件标记和持久转移意图支持安装后回执丢失与目录切换中断恢复。不跟随目录链接，不覆盖未知同名目录；历史修订和未变视频沿用现有身份/版本规则。跨进程文件锁不占用 SQLite 长事务。

`.tianshu-transfer/previous-*` 保留更新前的旧版，缓存和历史副本目前不自动清理。首次和修订都需要副本空间；备份应包含媒体台账及密钥、暂存区和目标文件。NAS 大文件吞吐和真实服务器版本仍需实机验证。
