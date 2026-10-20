# 可选 PostgreSQL 与 Redis

默认继续使用 SQLite。`database_path` 保留为 SQLite 文件和模块文件目录的锚点，不能填数据库 URL；原有 `storage: "sqlite_local"` 设置也保留。启动配置另加：

```json
{
  "storage_backends": {
    "postgresql_url": "postgresql://platform:password@postgres:5432/tianshu",
    "redis_url": "redis://redis:6379/0",
    "namespace": "tianshu",
    "connect_timeout": 3,
    "pool_max_size": 8
  }
}
```

也可使用容器环境变量 `TIANSHU_POSTGRES_URL`、`TIANSHU_REDIS_URL`、`TIANSHU_STORAGE_NAMESPACE`；非空环境变量优先于对应 JSON 字段。URL 含秘密时使用现有部署秘密注入方式，避免写进镜像或提交配置。PostgreSQL 和 Redis 独立可选。

## 启动、预检与引导

正常服务启动先只读校验现有配置，再实际检查 PostgreSQL 的连接、建 schema 权限和事务读写。首次检查失败且尚未选择 PG 时继续 SQLite；迁移开始后即写入持久绑定，后续故障或移除 URL 都会拒绝启动，避免产生第二套业务事实。

平台注册主库及其全部功能分库，用各自的 PostgreSQL schema 保存真正的表、索引、唯一约束和业务行。历史 HA 连接修订的账本也迁移。SQLite 的 JSON 查询由产品模块转换为 PostgreSQL JSONB 运算，权威变更触发器使用 PL/pgSQL；交易认领继续使用 SQL 事务、唯一约束和数据库事务级锁。Redis 不参与权限判断、任务认领或发送对账。

SQLite 数据迁移时保留源文件，逐表核对类型化行、数量和内容，再完成后端激活；中断可以重启继续。产品元数据同步登记历史业务表清单，已激活库缺少历史表时拒绝启动/就绪，不能通过建空表继续运行。同一安装不能另改 namespace、丢弃 `.tianshu-storage-platform.json` 或把旧 SQLite 当作当前库重新启动。

`preflight` 只读检查当前绑定和当前 PG 表，不建 schema、不迁移、不创建 Redis 键。尚未迁移的 PG 配置显示 `requires_initialization`；需要正常启动完成初始化。

已有 `providers-init` 继续负责首次创建私有目录、密钥和本地引导库，然后由服务启动统一导入 PG。它在 pending/active PG 绑定存在时拒绝重新初始化，防止创建旧 SQLite 或替换密钥。外部目录等已有首次引导步骤同样先于整个平台启动。

## 数据和文件归属

| 位置 | 保存内容 |
| --- | --- |
| SQL 主库 | 来源与映射、权威版本和撤销、模型配置、权限、人物/渠道映射、审计与幂等记录 |
| SQL 功能分库 | 提供商/外部连接/适配器的既有加密目录，网页输入/发送/发布，HA 当前及历史控制，角色运行、观察、QQ 管理，基础设施观察与控制，媒体订阅/任务/阅读进度/文件索引 |
| 原有媒体、音乐、书籍和漫画成品目录 | 视频、音频、EPUB、CBZ、图片、NFO、交付歌词；保留原路径与硬链接 |
| 原有下载、封面及扩展目录 | 下载暂存与分片、封面缓存、漫画扩展包及现有文件恢复材料 |
| `<media-db>.with_suffix(".book-content")/chapters` | 下载章节完整正文及听书任务的不可变输入文本。例如 `platform.sqlite.media.sqlite` 对应 `platform.sqlite.media.book-content/chapters` |
| `<media-db>.with_suffix(".lyric-content")` | 歌词修复待写内容，例如 `platform.sqlite.media.lyric-content` |
| `<bots-db>.payloads` | 待发送/结果未知消息的不可变媒体载荷，例如 `platform.sqlite.bots.sqlite.payloads` |
| 原有私有配置目录和状态卷 | 启动配置、TLS、目录加密密钥、管理员 `.sealed` 封印、后端绑定文件 |

文件引用包含相对内容地址、SHA256、字节数和媒体类型。先写同目录临时文件并 fsync，再原子发布；成功后 SQL 才提交引用。读文件会校验长度和内容哈希，发送时临时重建原 base64 请求，对外协议与请求摘要保持。

旧内联正文迁移逐项验证读取等价，并保留业务修订、租约、语义摘要和未知发送状态。事务失败可能留下尚未引用的不可变文件，不会提交指向尚未持久化文件的记录。本批不自动清理这些可靠恢复材料，也不把它们加入普通缓存 TTL。

## Redis 缓存

| 缓存 | TTL 与失效 |
| --- | --- |
| 音乐元数据 | 复用现有每操作 TTL（通常 1 小时、较稳定详情 24 小时）。键包含供应商、账号/凭据摘要、账号修订及参数；刷新回源，账号修订改变即换键 |
| 天气 | 成功 300 秒；错误/旧结果重试 60 秒。键包含配置修订、服务、地点、语言和单位；每次仍执行现有角色授权与读后修订检查 |
| 媒体容量分类统计 | 300 秒；键包含节点、数据库文件锚点、目标/映射及卷范围；配置/卷改变换键，强制刷新删除当前键。实时卷可用空间继续现场读取 |

共享值带采样和过期时间，命中不会延长原有效期。服务/安装代次/namespace 隔离键空间。Redis 不可用或数据丢失时复用现有本地缓存并正常回源；统计缓存不替代容量守护。权限、凭据正文、唯一任务账本及会话授权不写入 Redis。

## 备份与恢复

备份必须同时保存 PostgreSQL 数据、上表中新增内容目录、既有成品/暂存/私有密钥目录、管理员封印及后端绑定文件。SQL 引用与文件库组成一个恢复集合；单独恢复 SQL 不能重建已下载章节或未知发送载荷。停写后完成 PG 一致快照与文件清单，恢复到相同文件锚点和挂载后再启动。Redis 可丢弃并回源。

留存的 SQLite 是迁移前恢复材料，激活后不再接收新业务。回退需要恢复同一时点的整套数据库与文件备份；删除 PG 配置不是回退操作。独立 AssetLibrary、Chat Audit 及其数据库不属于此迁移。

## 安装与验证

产品携带 `vendor/tianshu_storage-0.1.0-py3-none-any.whl`，SHA256 为 `e7c8ef910a43a5e2d1f426948fe19e4ea3942e1ff0618a35ecfae03bfedc1482`。开发依赖和容器锁先安装该相对 wheel，再安装产品；不依赖未发布的 PyPI 包或其他工作树路径。

沿用后端完整回归命令。设置 `TS231_TEST_POSTGRES` 为可清空、独立的 PG 测试库 URL 可运行 `test_storage_backends.py` 的真实 PostgreSQL 组，每例使用独立 namespace。设置 `TS231_TEST_REDIS` 可启用真实 Redis 组。所有测试仅使用合成数据；缺失外部测试服务时明确跳过，不能称真实接入通过。
