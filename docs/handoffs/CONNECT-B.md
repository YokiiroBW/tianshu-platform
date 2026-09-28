# CONNECT-B 平台后端交接（2026-09-27）

## 目标与范围

在 Platform 既有网页登录/CSRF/会话边界内，提供 Memory 本人记忆、项目知识、Companion 生活/已发布日记的固定只读入口，统一连接状态，并让管理员保存和检测 AssetLink 与 Home Assistant 两类固定外部连接。本任务只改 `tianshu-platform` 后端、测试与交接；不改前端、根合同、对端产品、NAS、真实设备或真实数据。基线 `1d51d888ff77594a1f6fbb1c7e0d4fdf92285ff8`。

## 固定提交

| 提交 | 内容 |
| --- | --- |
| `347a15d87189f0269861d1e0d209b4748380926b` | Knowledge/Life 固定 HTTPS 只读连接、本人 Memory 浏览入口、九项连接摘要、Persona published 加载门禁 |
| `bc45342babac2c7597987d0edb4f8a9f27763023` | 对齐 Memory overview/subjects/records 实际 wire，加入真实 Memory HTTPS 联测 |
| `3b0dc2473d60b3b359c4f31f1e14d3bde59fb1a9` | Knowledge lessons/experiences/continuation/continuation-check 四端点、会话内有界私包与真实 Knowledge HTTPS 联测 |
| `8699f1880f6e94920bb6a442f1c25bc20845fbd3` | 固定 Knowledge 交接与复跑命令 |
| `4d9214249467bb8c80d45d38d49eab96229e1b63` | 管理员固定 AssetLink/HA 连接、私有加密目录、URL pin、保存/检测/轮换联测与部署交接 |
| `5c55ef1b62d962ad42278132a29ef46aa4111832` | 真实 Companion HTTPS 四路由联合验收及说明 |

浏览器消费字段与错误码见 [CONNECT-B-API.md](CONNECT-B-API.md)；专门交接见 [CONNECT-B-KNOWLEDGE.md](CONNECT-B-KNOWLEDGE.md)、[CONNECT-B-EXTERNAL.md](CONNECT-B-EXTERNAL.md)、[CONNECT-B-LIFE.md](CONNECT-B-LIFE.md)。

## 部署前置配置

- 网页 operator 必须显式具备实际要用的 `knowledge.read`、`life.read`、`memory.read`、`external.manage`；Memory 还要求 `origin.issue`。页面不自动授予动作。
- `web_memory` 要求固定 HTTPS `base_url`、独立 browse `token_env`、CA 与 `entry_id`。该 entry 属于网页登录 operator 同一稳定 account，类型 `local_operator`、受众 `self_private`，带精确 `platform → memory / dialogue` route。Memory 端独立 `browser_readers` 仅授登记 account/actor/scope 的 `browse`；Memory 使用独立发行方凭据向 Platform 正式 resolve，Browser Bearer 与 issuer Bearer 分离。缺 person/conversation 身份映射失败关闭。
- `web_knowledge` 要求固定 HTTPS endpoint、独立 `token_env`、CA、显式 `projects` 白名单；continuation 另需每项目显式 `checkouts`。Memory 端运行独立受控 TLS Knowledge 服务，以该固定 client 分别授权实际要用的 `query/document_list/document_read/note_query/lesson_query/experience_query/continuation_recover/continuation_check`；`experience_query` 另需 `review` 权限。Knowledge 与本人 Memory 的凭据、身份和端口分离。续接私包只留本网页登录会话，登出/撤权失效。
- `web_life` 要求固定 Companion HTTPS endpoint、独立 reader `token_env` 与 CA；Companion 端同时配置 `callers` Bearer 身份、`life_readers` 的固定 `reader_id`/actor 白名单及剧情日记访问授权。Platform 只读四条 `/internal/v1/life-read/*`，不推进生活状态或写日记。
- 管理员外部配置要求 `web_external={directory,allowed_cidrs,assets_connection_id}`；后者在 `asset_connections` 为 `{managed_external:true}`，绑定启用的 `web_assets` 与具有 `asset.read` 的独立服务 principal。与旧静态 `home` 不并用。停服安装时在私有父目录下显式执行 `python -m services.platform.external_catalog init <绝对目录>` 并配置系统 ACL；启服前执行 `... check <绝对目录>`。目录、数据库和 key 一起备份/恢复，步骤见 [CONNECT-B-EXTERNAL.md](CONNECT-B-EXTERNAL.md)。网页保存后先是 `saved_unverified`，检测成功才是 connected；登记实体不创建 HA 控制模板。
- Persona 根 `persona-management/v1` 已由总控在 `f705475d9ee26df29ea634349d8c18398418b40f` 正式发布，manifest 为 `published`、`production_publish_authorized=true`、`joint_runtime_acceptance=passed`。B 固定 manifest SHA256 `72ae9ee2fd5e0140e122877c35d90eb747f1d56804c33d8f01c9eb38413c0e2d`；加载器还核每个文件 hash、生产者提交和操作闭集。实际部署仍须把同一发布目录及固定 Companion HTTPS 凭据交给 Platform，不能用隔离 fixture 冒充现场连接。

所有服务地址、私有目录、账号/actor/project/checkout、CA 和令牌都需总控按现场审阅后配置。凭据、真实聊天/健康/资产数据、数据库、模型权重与运行目录不入库。上述配置不代表 NAS 已部署。

## 实际验证

本 worktree 使用忽略入库的 `.runtime/venv`。设 `TS012_CONTRACT_DIR`、`TS013_TLS_PYTHON`；真实对端联测另设 `TS_CONNECT_M_PATH`、`TS_COMPANION_PATH` 指向固定只读产品检出。各命令与前置项在专门交接中。已执行并通过：

| 范围 | 结果 | 对端 |
| --- | --- | --- |
| 首版 `test_web_readers`、`test_web_memory`、`test_persona_published`、`test_web_console` | 新 9 项和旧控制台 8 项通过 | Knowledge/Life 合成 HTTPS、Memory 合成 HTTPS、Persona 合成 manifest |
| `test_memory_joint` | 1/1 | 真 Platform 发行方与真 Memory HTTPS；合成本人资料 |
| `test_knowledge_joint` | 4/4 | 真 Memory Knowledge CLI HTTPS；合成项目/checkout |
| `test_life_joint` | 1/1 | 真 Companion uvicorn HTTPS；合成小说 actor/日记；读前后库行相同 |
| `test_web_external`、`test_web_assets`、`test_home` | 4/4、31/31、45/45 | 真同源 HTTP 与合成 AssetLink TLS/HA HTTP |
| 正式 Persona 发布包 `load_published`、`test_persona_published` | 实际根目录加载成功；3/3 | 总控根提交 `f705475`；未发布、未 pin 或文件篡改拒绝 |

受影响 Python 范围 `ruff` 通过，各固定提交的 `git diff --check` 通过。Memory 真进程版本 `02df5ba8c041a7a6eb8b705c5f264e10543032ec`；Companion 联测源版本 `31677983798ba27b24d57925feab4774c2eec30f`。上述均为本地隔离夹具；没有 NAS、真实 Memory/Companion/AssetLink/HA 业务读取或生产凭据验收。

## 未完成与下一步

1. 总控审阅 B/U/A 固定提交与跨产品契约，按依赖顺序集成。前端真实 Chromium 对接由 U/A 报告；B 等待具体反馈后仅修消费端问题。
2. 总控集成固定 Persona 发布包与 Platform 的 `published_directory`，在受控运行环境核对证书、凭据、固定对端提交与真实角色读取。B 已 pin SHA 并验证实际根发布包的静态加载；这不等于 NAS 的业务链通过。
3. NAS 配置、私有库 ACL/备份、证书与固定 reader 授权、真实业务读取和回退由总控统一执行。本任务没有推送、部署或生产数据迁移。
