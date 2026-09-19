# B 站内容身份、元数据与 NFO 候选（TS-090）

日期：2026-09-19（含首次验收 R1–R5 返修）。状态：本地隔离实现，纯函数。本页描述**已实现**的领域能力和明确边界；不是下载、发布或媒体服务器验收记录。

## 1. 范围与位置

| 文件 | 职责 |
| --- | --- |
| `services/platform/media/types.py` | 不可变公开类型、具名错误、issues 词表、固定常量 |
| `services/platform/media/identity.py` | BV/CID/MID 形状与稳定键、规范链接、包目录与分集文件名 |
| `services/platform/media/metadata.py` | snapshot 校验、归一化、人工展示覆盖、字段来源与缺项 |
| `services/platform/media/sidecars.py` | `RenderRequest` 校验、NFO 与 source.json 内存生成 |
| `services/platform/media/__init__.py` | 公开面：两个函数 + 冻结类型 + 两个错误 |

公开面只有：

```python
normalize_bilibili(snapshot, *, overrides=None) -> MediaMetadata
render_sidecars(metadata, request) -> SidecarBundle
```

本包**没有**网络、文件、进程、时钟、数据库、随机数或环境变量访问：`import` 不产生任何 IO 或目录，测试以静态 AST 白名单与全新解释器两次验证。

## 2. snapshot 是内部投影，不是 B 站接口

snapshot 是**未来连接器**产出的内部投影。本模块不猜任何站点接口、不解析用户链接、不保存 Cookie/签名 URL/任意 `raw`。全部键必填，未知键与类型错误一律 `MetadataValidationError(code, field)`，错误消息只含固定 code 与字段路径，**不回显原值**（凭据可能就在被拒的键里）。

| 字段 | 规则 |
| --- | --- |
| `schema_version` | 整数恰好 1；bool 不算整数 |
| `bvid` | 严格 ASCII 大写前缀 `BV` + 10 个 ASCII 字母数字；10 位正文原样保留；`bv`/`Bv`/`bV` 拒绝，本层**不**规范化为新 ID；格式合法不等于远端存在 |
| `title` | string 或 null；原文逐字保留（含首尾空白/换行）；全空白视缺项；≤512 字符 |
| `description` | string 或 null；原文逐字保留；全空白视缺项；≤65536 字符 |
| `published_at` | 带时区 ISO8601 秒精度或 null；归一到 UTC `...Z`；无时区本地时间拒绝；折算越界为具名错误 |
| `captured_at` | 必填同上；**不读系统时钟** |
| `creators` | 0–100 个 `{mid,name,role}`；mid 只接受无前导零正十进制**字符串** ≤20 位；name ≤128 原文保留；role 仅 `uploader`/`collaborator` |
| `parts` | 1–1000 个 `{cid,index,title,duration_seconds}`；cid 同正十进制字符串规则；index 1–1000 且不重复；cid 不重复；title ≤512 原文保留；duration 非负整数或 null |
| `tags` | ≤100 项、每项 ≤128；去空项；按首次出现稳定去重 |
| `cover_available` | bool；只表示连接器**看到**封面来源，不表示图片已落地或可解码 |

**内容身份严格**：BV 前缀只接受 ASCII 大写 `BV`（`bv1xx411c7mD`、`Bv1xx411c7mD`、`bV1xx411c7mD` 一律 `invalid_bvid`），CID/MID 只接受正十进制字符串，整数（含 >2^53）、bool、float 一律拒绝。理由：大小写是 BV 内容的一部分，规范化会凭空造出源站从未发布的 ID；整数无法在浏览器里无损携带 64 位标识。被拒值原样进入主键/路径的通道因此关闭。

NUL、XML 1.0 禁止控制字符、落单代理与非字符拒绝；普通换行、引号、emoji、中文与 XML 符号原样保留，由标准库转义。CID/MID 以字符串输出，避免浏览器整数精度损失。

## 3. 身份、路径与集号

- 投稿键 `bilibili:video:<BV>`；分 P 媒体键 `bilibili:video:<BV>:cid:<CID>`；人物键 `bilibili:creator:<MID>`。
- 规范链接 `https://www.bilibili.com/video/<BV>/`，分 P 为 `?p=<index>`；不接受也不保存用户原始链接、短链、av 号或扩展 `raw`。
- 单 P 包目录 `bilibili-<BV>-cid-<CID>/`：`video.<ext>`、`movie.nfo`、`source.json`、有绑定时 `poster.<ext>`。
- 多 P 包目录 `bilibili-<BV>/`：`tvshow.nfo`、`source.json`、`Season 01/S01E<episode:02d>-cid-<CID>.<ext>` 与同名 `.nfo`、有绑定时 `<同视频stem>-thumb.<ext>`。
- `:02d` 是**最小两位**，不是上限：第 1234 集是 `S01E1234-...`。
- 集号由调用方通过 `RenderRequest.episode_numbers` 明确给出，渲染器**不自行分配、不因源列表重排而重编号**。快照 index 变化会改变 source.json 的来源记录，稳定键与已分配集号不变。
- 路径只由已验证身份与固定字面量拼成，比较用正斜杠，无 `..`、盘符、绝对路径或重名。标题/昵称**永不进入文件名**：`../../foo`、`CON`、`LPT1`、超长昵称、含 `/` 的标题都不改变任何路径。

## 4. 原文、展示值与人工覆盖

`overrides` 只接受 `title`、`description` 两个键，值为 string。

- **原文不可有损**：`original_title`/`original_description` 非 null 时逐字保留输入字符串，包括首尾换行与空格；null 以空字符串表示。本层不做 strip 后再写 original。
- 非空白的来源或覆盖字符串**原样显示**；全空白展示折成空字符串并标记缺失（来源为 `missing`，覆盖为 `user` + 对应 issue）。字段原始空白仍保留，人工覆盖不删除原文。
- 归一化同时保留 `original_*` 与 `display_*`，并给出 `field_sources`（`source`/`user`/`missing`）。
- 分 P 标题与作者昵称同样原样保留普通字符（含首尾空白），只有 tags 沿用既有的去空白/去重策略。
- 缺少某键表示保留来源；显式 `null` 拒绝（防止隐式删除）。
- 展示文本不做 HTML 执行，保留源文本；XML 由 `xml.etree.ElementTree` 转义。

## 5. NFO 与 source.json

- XML 带 UTF-8 声明；根元素 single 为 `movie`，多 P 父为 `tvshow`，每集为 `episodedetails`。全部用标准库序列化，不手工拼接用户输入，不解析外部实体。
- `title` 用 display，`originaltitle` 用 original，`plot` 用 display_description；`year` 来自源发布时间的四位年份，未知则省略；**不从 captured_at 伪造首发时间**。
- **日期 profile**：`premiered`（movie/tvshow）与 `aired`（episode）只写 `YYYY-MM-DD`（取自归一后的 UTC 发布时间），未知则省略；`source.json` 仍保留完整 UTC 秒时间。依据是固定上游 XbmcMetadata 的默认 `ReleaseDateFormat` 为日期，解析器按此读取——完整 ISO datetime 不是本层能声称的兼容证据。
- **海报 profile**：movie/tvshow 的封面写 `<thumb aspect="poster">poster.jpg</thumb>`（png 同理），**不生成**顶层自造 `<poster>`；无绑定则不写任何图片元素。依据是固定上游 `BaseNfoParser` 的图像入口读取 thumb/fanart。
- **分集图引用基准**：`referenced_images`/`source.json` 保存包根相对路径 `Season 01/<stem>-thumb.<ext>`；分集 NFO 内的 `<thumb>` 只写**同目录叶文件名** `<stem>-thumb.<ext>`，用 NFO 所在目录解析后必须与清单中的同一声明文件一致。无绑定就不写 `<thumb>`。
- 每位**有姓名**作者输出 `<actor><name>昵称</name><role infoset="true" name="uploader" language="zh-CN">UP主</role><order>…</order></actor>`，`order` 从 0 起按具名作者顺序编号。缺姓名时**不输出 actor 节点**（避免伪造人物候选），MID 与 `author_name_missing` 仍留在内部记录与 source.json；已有具名作者照常全部输出。同名不同 MID 保留为两个人；NFO **不能**保证媒体服务器的人物归并，最终归并需服务器侧验收。
- 稳定 ID 只写 `<uniqueid type="bilibili">`（作品键或分 P 媒体键），不写假的 `imdbid`/`tmdbid`/`tvdbid`。
- 集标题优先分 P 标题，否则非空展示标题加 ` - P<index>`，两者都缺则省略 `<title>` 并记缺项；该回退只格式化已有字段。
- 多 P 集输出 `showtitle`、`season=1`、`episode=<显式映射>`、逐条 `<tag>`；`runtime` 仅在时长已知时输出向下取整分钟数（不足 60 秒为 0），source.json 保留源秒数；未知时长省略而不是写 0。父级不累加未知分 P 时长。
- 图片与视频只引用包内相对路径，且只在调用方声明绑定时出现；不输出上游 URL 或环境路径。本卡不生成人物头像，也不写 `people/` 或 `person.nfo`。
- `source.json` 是白名单可重建文档：`schema_version`、`generator`(`tianshu-media-metadata/1`)、`provider`、`bvid`、`item_key`、`media_key`、`canonical_url`、`layout`、`media_extension`、`original_/display_title/description`、`field_sources`、`published_at`、`captured_at`、`cover_available`、`creators`（键/MID/姓名/缺名/角色）、`tags`、`parts`（CID/index/标题/缺题/时长/是否选中/集号/视频路径）、`selected_cids`、`episode_numbers`、`expected_media`、`referenced_images`、`issues`。UTF-8、`ensure_ascii=False`、两空格缩进、**递归对象键词典序（`sort_keys=True`）**、单个结尾换行。数组仍保持已定义的业务顺序（`parts` 跟随快照来源顺序，`tags`/`creators` 首次出现顺序，`selected_cids`/`episode_numbers` 调用方顺序）。无 `raw`、无凭据、无任何环境绝对路径或短期下载地址。

### 5.1 媒体服务器兼容缺口（未通过，需后续卡验证）

便携相对图片引用是**候选包语义**：NFO 内的图片引用按同目录叶文件名解析，`source.json` 声明包根相对路径。Jellyfin 当前参考解析器要求绝对 URI，因此**本卡没有媒体服务器兼容通过结论**，也不以 XML 可解析冒充服务器已识别。后续卡（TS-095）须在锁定版本上实测同目录图片发现与发布目标 profile，必要时经 AssetLibrary 发布阶段生成目标 profile；平台不直写成品，也不写服务器内部 people/cache。本卡不添加环境路径、网络适配、图片 IO 或人物缓存写入。

## 6. `RenderRequest` 与候选状态

`RenderRequest` 是冻结类型：`layout`(`single`/`multipart`)、`selected_cids`（非空、去重、必须属于 metadata）、`episode_numbers`（multipart 必须为每个所选 CID 给出唯一正整数 ≤999999；single 必须为空）、`media_extension`(`mp4`/`mkv`)、`images`。

`ImageBinding(role, cid, extension)`：role 仅 `poster`/`episode_thumb`（非字符串或未知值 → `invalid_image_role`）；poster 的 cid 必须为 null；episode_thumb 的 cid 必须是已选中的字符串 CID；extension 仅 `jpg`/`png`；每个 role/cid 唯一。single 不允许携带 episode_thumb。手工构造的绑定同样被复核：cid 不是字符串也不是 null 时（list/dict/int 等）在进入任何集合 membership/hash **之前**就报 `invalid_poster_cid`/`invalid_thumb_cid`，不会漏出 `TypeError`。

`RenderRequest.build` 对 `selected_cids`/`episode_numbers`/`images` 的非可迭代值（`None`、数字、对象等）与错误元素形状定点拒绝为 `invalid_selected_cids`/`invalid_episode_numbers`/`invalid_images`，不裸抛 `TypeError`。

绑定只是“本地文件已验证”的**声明**，本卡无法验证文件存在，因此：

- `SidecarBundle.status` 恒为 `rendered_unverified`；
- `files` 只含生成的 XML/JSON，**绝不**用空 bytes 冒充媒体或图片；
- 图片与视频只出现在 `expected_media` / `referenced_images`；
- `cover_available=true` 但没有 poster 绑定时附加 `cover_local_missing`，不伪造图片文件；没有分集图时不写 `<thumb>`，需要补图由调用方据 `expected_media`/`issues` 决定。

所有公开入口的失败都是具名错误：`MetadataValidationError(code, field)` / `RenderRequestError(code, field)`，消息只含固定 code 与字段路径，**不回显原值**。可预期的越界（时区折算超出可表示 UTC 范围）转为 `invalid_timestamp`，不用全函数 catch-all 掩盖真实缺陷。

## 7. issues 词表

`title_missing`、`description_missing`、`creator_missing`、`author_name_missing`、`cover_source_missing`、`cover_local_missing`、`published_at_missing`、`part_title_missing`。每条带 `field` 指出对象（如 `parts[2].title`、`creators[0].name`），不回显隐私内容。

字段齐全也仍是 `rendered_unverified`。默认严格发布需标题、简介、至少一位具名作者和本地封面；宽松批准逻辑**不属于**本卡。

## 8. 确定性

同一归一化输入与同一 `RenderRequest` 生成逐字节相同的输出；无随机 UUID、无当前时间、无环境依赖。只有 `captured_at` 来自快照本身。`source.json` 的递归键词典序排序保证输出与文档组装顺序无关。

## 9. 验证

```powershell
# 最窄套件
python -m unittest discover -s tests/backend/media -v

# 常规后端发现（媒体用例随之加载）
python -m unittest discover -s tests/backend -v

# 真实 ruff 0.15.7 门禁（wheel 在 .runtime/review-tools，隔离安装到 .runtime/review-tools/site）
python -m ruff format --check services/platform/media tests/backend/media
python -m ruff check services/platform/media tests/backend/media
```

测试为合成 fixture，无网络、账号、Cookie 或真实响应；NFO 用 `xml.etree.ElementTree` 独立解析读回（含按 NFO 所在目录解析分集图引用），source.json 用 `json` 独立解析。

**测试入口**：`tests/backend/media/` 现在是包（含 `__init__.py`），因此

- 常规 `discover -s tests/backend` 会把它作为包 `media` 递归发现并运行全部媒体用例；
- 原最窄命令 `discover -s tests/backend/media` 仍把它作为顶层目录运行同一批用例。

每个测试模块先尝试相对导入 `._fixtures`，失败时回退到顶层 `_fixtures`，两种入口都成立；未修改任何旧测试入口或断言，也未做全局 `sys.path` 改写或模块替换。测试包完全限定名 `media` 与生产包 `services.platform.media` 不同，二者不冲突。

## 10. 边界

纯函数与旁车候选通过，**不等于** B 站实际下载、图片落地、Emby/Jellyfin 实机入库。本卡不含下载引擎、账号会话、数据库、迁移、HTTP/CLI 路由、网页入口、发布流程或人物头像；这些分别属于后续卡片与真实服务器验收。

首期 NFO profile（日期词法、poster 标签、分集图引用基准）来自协调者补充规格与固定上游解析器行为；**相对图片引用被两服务器直接接受仍未验证**，见 §5.1。
