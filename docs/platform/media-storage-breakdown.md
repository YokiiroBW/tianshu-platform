# 媒体磁盘四类占用（TS-129）

统计服务只读取目录和文件元数据，类别为“订阅占用、Emby占用、其他占用、未使用”。容量继续从直接媒体目标的真实directory读取；容量归属与文件身份分别处理。Linux Btrfs通过当前容器mountinfo的major:minor和source识别共享容量池，其他文件系统/平台按st_dev；不因同server_id或容量数值相同合盘。

## 扫描范围与口径

`storage_scan.py`先遍历所有配置订阅根内的普通文件，含NFO、封面和共享视频控制池；再遍历现有Emby连接通过`MediaServers.libraries()`取得的所有媒体库Locations。跨目录硬链接、重叠媒体库和重复目录按`(st_dev,st_ino)`全局排重；订阅已计的inode从Emby排除。只读stat/lstat/scandir，不读文件内容，不跟随symlink/junction，也不递归跨入另一设备的子挂载。独立已配置卷可从其明确观察根统计。Windows DirEntry缓存的空设备/inode用原生lstat补齐，不虚构零占用。

`storage_filesystem.py`为容量展示和扫描根归属共用的唯一规则：只在Linux读取`/proc/self/mountinfo`，解码八进制路径转义，以最长完整挂载点边界定位根。明确btrfs及绝对mount source时，容量键为`(btrfs,mountinfo major:minor,source)`；不能要求mountinfo设备号等于stat设备号。无法核验或其他FS退回原stat设备身份。文件/目录排重仍是原`(st_dev,st_ino)`，每个明确扫描根仍以自己的原st_dev限定子树。这样可统计同池不同子卷的明确Emby根，不会把不同子卷的同号inode去重，也不会因为同池就跨入意外子挂载。Btrfs子卷共享存储池且各有inode命名空间，见[官方子卷文档](https://btrfs.readthedocs.io/en/latest/Subvolumes.html)。

2026-10-13实机修复依据：三条media bind的mountinfo均为`0:39`、btrfs、`/dev/mapper/cachedev_1`；Video subvol260的stat dev6291518，Music subvol264的stat dev7340078，两者fsid也不同。不能用fsid或相同total/free替代文件系统身份。此组合有固定回归，正式NAS再次统计由协调者执行。

有st_blocks时每文件计`st_blocks*512`；没有该字段时整个卷统一退为logical文件长度近似，响应basis=logical。目录、特殊文件和链接本身的分配量归其他。其他=`total-free-subscription-emby`，包括其余文件、文件系统开销和预留；未使用沿用实际可用free（Linux为bavail口径）。已计字节大于实际total-free时保留观察数，other=null并标partial；reflink/压缩或扫描期间内容变化不能用clamp伪装总量守恒。

订阅根不可读时订阅为null；Emby网络失败、Locations无映射、映射根缺失/拒绝读取时Emby和other为null，已完整计出的订阅数保留。没有Emby连接或全部Locations为空时明确Emby=0。无法判断未知路径在哪个卷时保守把该分类标未知，不猜归属。缺失目标卷容量为unavailable/null，不回退读父盘。

## 目录映射

媒体服务端路径只经已配置Emby server的映射读取。直接映射来自该target的`server.media_root → target.directory`；可新增静态 `media.storage_mappings=[{server_id,server_prefix,local_prefix}]`，最长完整路径边界优先；同前缀显式映射覆盖直接映射。不会用任意远端路径作为本机目录，也不因为mapping里写了server_id就新建连接。拒绝相对本机路径、路径中的`.`/`..`、重复同server前缀及非路径URL；配置只校验结构，不创建或改权限。最多128条。

本轮计划中的配置形状（实际挂载与权限由协调者维护）：

```json
{
  "storage_mappings": [
    {
      "server_id": "f95ba599-3b54-42b2-9cd0-b33d6dc5f0fb",
      "server_prefix": "/video/存储库",
      "local_prefix": "/media/observe/emby-video"
    },
    {
      "server_id": "f95ba599-3b54-42b2-9cd0-b33d6dc5f0fb",
      "server_prefix": "/music/音乐存档",
      "local_prefix": "/media/observe/emby-music"
    }
  ]
}
```

这段是media对象里的新增字段，不能覆盖其他settings。观察bind应只读，原Bilibili读写映射保留。音乐根ACL若拒绝运行UID，挂载存在仍应partial；本功能不自动改NAS权限。

## 缓存、取消与维护

`StorageBreakdown`复用Media的local线程和生命周期，不新增数据库/schema、HTTP写入口或第二套任务系统。view只做现有容量查询并调度扫描；首次响应scanning，不阻塞页面或Media.start。缓存TTL为300秒，多个view合并同一次扫描；到期view保留同范围旧快照并refreshing=true。目标、连接、目录映射或设备分组变化使旧范围立即失效，旧扫描协作取消且不能发布迟到结果。旧文件线程尚在收口时新扫描等同一gate，避免并行递归两次。关闭设置线程Event并等待已调度的本地扫描按目录项协作结束。

在已有Platform实例内只读触发并等待一次统计，不需要启动订阅/下载worker：

```python
storage = await platform.media.storage_breakdown.refresh(force=True)
```

返回storage完整投影，partial也是有效的真实结果；force绕过已完成缓存，已有in-flight统计仍合并。也可以 `await media.view()` → `await media.storage_breakdown.task` → `await media.view()`。`media.storage_breakdown.invalidate()`只清缓存/取消旧工作，下一view再扫描。不调用Media.start即可做容器维护读取；结束后`await media.close()`清理HTTP和扫描。冷备无需保存此缓存，重启重新测量。

固定breakdown.code：

| code | 含义 |
| --- | --- |
| null | 完整allocated读数 |
| storage_scanning | 首次后台测量尚未完成 |
| storage_subscription_unavailable | 订阅范围未能完整读取 |
| storage_emby_unavailable | Emby库列表无法取得或返回无效 |
| storage_path_unmapped | Emby库路径没有明确本机映射 |
| storage_path_unavailable | 已映射观察路径缺失、拒绝读取或本地扫描不可用 |
| storage_measurement_inconsistent | 分类分配量与真实容量矛盾，未裁剪 |
| storage_capacity_unavailable | 实际媒体卷容量不可读 |
| storage_logical_estimate | 完整但使用logical近似口径 |

浏览器联合夹具追加`--local-server --storage-emby`，使用真实Platform HTTP、实际文件元数据/硬链接与后台统计，但Emby库Locations仍是显式合成响应。默认既有夹具参数保留。此证据不能代替NAS分配量和真实Emby连接核验。
