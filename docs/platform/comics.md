# 漫画订阅

入口：订阅 → 漫画订阅，`/#/subscriptions/9`。使用现有控制台登录、媒体任务库、加密连接、下载中心与后台租约。视频引擎未配置时也可使用漫画功能。

## 首版流程

1. 连接现有 Suwayomi，读取已安装源；安装/更新源插件和源账号登录仍由 Suwayomi 管理。普通漫画与私有收藏分别浏览。关键词结果按作品/作者/画师核对，来源只返回热门列表时不会直接当作命中；明确不提供搜索的源允许读取当前目录页后筛选，并标注来源模式。
2. 进入来源版本详情，读取目录、预览前3页，可修改作品名/作者/画师/语言/方向/简介。不同来源、译者版本独立保存，不将同名作品的页面混合。人工编辑字段保留。
3. 选择章节下载，或订阅全部/最近若干章/仅今后新章。支持暂停、恢复、调整周期和立即检查。源移除旧章时保留本地目录身份及归档阅读入口，不删除已有文件。
4. NAS导入支持一组自然排序图片，或每章一个图片文件夹/CBZ/图片ZIP。用户明确选择一部作品目录；混合布局、重复编号、损坏图片或不安全ZIP会给出具体原因。原输入只读。
5. 原图按字节封装为ZIP_STORED的CBZ，附ComicInfo.xml，不重新编码或同时生成PDF。写前/写后校验图像、页数、CRC与逐页SHA256。成品按作品/语言来源版本/章节及内容版本路径不可变发布；同路径不同字节拒绝覆盖。
6. AssetLibrary完整读取NAS文件后，提交真正目录/entry/native/content证据和持久回执。随后扫描Komga，按精确库/物理路径/大小/页数核对，返回其原生阅读入口。下载、资产纳管、阅读可用分阶段展示。

首版不新生成漫画EPUB，也不实施阅读3图片规则。已有文件和阅读进度不迁移、不删除。Suwayomi使用GraphQL控制与同源图片代理；不加入它的收藏库、不改它已有的全局追更/下载设置。

## 恢复与边界

- 来源ID作为字符串保存；内部实例ID仅用于寻址。章节身份由来源作品、章节地址和版本组成，章节号不作唯一标识。
- 已验证页面持久保存在天枢私有暂存区；恢复只获取缺失页面。取消和失败保留可恢复资料。归档与Komga双回执确认后回收该任务的私有暂存，原输入和最终CBZ保留。
- AssetLibrary调用使用稳定操作键。响应丢失先查询原键；尚未完成的观察按同键同意图恢复。HTTP200、上传完成、文件存在或PendingReview都不算正式资产归档。
- 普通HTTP读取有界；完整AS观察POST与服务端30分钟预算对齐。单页最多64MiB，单章最多4GiB/10000页，一次本地导入最多2000章。目录完整只能证明已提供页面完整，不证明出版物没有缺页。
- 源插件错误、网站验证、代理断连会逐源报告。源可用性不能由离线夹具或Suwayomi服务探测成功代替。
- 连接秘密由现有Accounts密钥加密，前端只得已保存状态；AS支持指定CA证书并保持HTTPS验证。预览图片必须通过当前控制台会话。

## 部署接点与当前状态

NAS已只读核实：`/volume1/Documents/Cartoon`（用户共享 `\\YokiiroNAS\Documents\Cartoon`），原Komga库为漫画与本子，保留4704个既有CBZ及原阅读记录。拟新增输出 `漫画/天枢`、`本子/天枢`；Komga对应路径 `/Cartoon/漫画/天枢`、`/Cartoon/本子/天枢`。

AssetLibrary只读纳管协议是TS198候选 `/assetlink/v1/service-imports/{key}/observe`（GET原键和targets）；生产尚未接主Host/迁移/角色。真实客户端与隔离C#/PostgreSQL/NTFS/HTTPS联验已通过，不能称NAS已纳管。NAS Linux原生lease要求文件owner：Platform 10001与AS 1654不同，因此使用下面的专用文件写入进程，保留旧目录权限和原生lease要求。

### NAS 专用写入进程

同一 Platform 镜像使用独立入口 `python -m services.platform.media.comics.publisher_service --settings /etc/comics-writer.json`。部署配置只含 `targets`（comic-main/comic-adult 对应容器内两个输出根绝对路径）、`socket`（独立共享卷内路径）、`client_uid`（10001）。进程以1654:10001运行，仅两处新输出根可写，不挂旧Documents、平台数据库或服务凭据。平台仍以10001运行，原Cartoon作为导入源只读挂载；共享socket卷在平台侧只读，socket权限0660，服务端用Linux SO_PEERCRED核对连接进程UID。没有网络监听或新访问令牌。

平台漫画设置的 `publisher_socket` 指向该socket，缺省为空时沿用同进程本地发布（用于同UID部署及开发）。部署设置会随目标快照保存在任务中，网页保存其他设置不会清除它。连接探测同时返回写入进程状态。

专用进程只处理已生成CBZ的传输和不可变发布，不承担订阅、搜索或资产索引。稳定意图键绑定目标/路径/长度/SHA；4MiB分块校验并fsync后记录进度。进程中断后截去未确认尾部，从确认偏移续传；同键不同意图拒绝。完整SHA通过后复用现有volume lock和原子create-if-absent，删除暂存硬链接、同步目录，再给完成回执。已存在的不同字节永不覆盖；发布后回执丢失会核对同一文件并恢复，AS观察前最终文件只剩一个硬链接。AS仍从原Documents只读挂载读取，不跨挂载别名。

此角色的Windows协议与实际平台工作队列测试已通过；跨UID、只读socket卷、Btrfs及AS原生lease仍需NAS隔离实测。未把该进程接入生产Compose。

Suwayomi实机为2.3.2243、Komga为1.24.4-master；未升级。Suwayomi代理指向已失效的旧mihomo地址，代理方案待用户确认。Komga浏览器已登录，新增专用API key的操作确认待回复。

## 验证

后端沿用unittest：`python -m unittest discover -s tests/backend -p test_comics.py -v`。浏览器沿用Playwright：设置已发布合同目录 `TS012_CONTRACT_DIR` 和工具解释器 `COMIC_TEST_PYTHON`，构建后运行 `node node_modules/@playwright/test/cli.js test --config apps/web/playwright.comics.config.ts`。夹具只使用合成图像和本地HTTP同行，真实平台认证、工作队列、CBZ文件和读取接口执行。

跨产品真实协议验证入口在AS TS198交接 `platform-joint-2026-10-18.md`；生产发布必须在听书和AS当前升级释放窗口、对齐最终历史与线上快照后串行进行。
