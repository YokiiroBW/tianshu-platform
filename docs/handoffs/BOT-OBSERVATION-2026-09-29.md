# 机器人账号观察 · Platform/UI 交接

## 目标与基线

- 基线 `186da00f84eb37653247c2bda05407ff89558540`；实现分支 `codex/bot-observation-policy-20260929`，本交接随该分支实现提交固定。
- 管理员按真实在线机器人账号接入，群聊和私聊分别设置观察开关与回复名单；默认均观察、均仅观察。旧精确范围连接维持原样。

## 变更

- `bot_observation.py` 保存独立账号策略、来源账本和发现列表；观察事件经宿主持久队列、Platform 核准后送 Companion，不调用聊天生成。来源含实例、机器人账号、会话、作者、原生消息和 `archive_epoch`。
- 回复仅处理宿主已确认接管的事件，并在发送前复查当前策略、宿主版本和名单。历史读取关闭增加 `archive_epoch`；归档跨服务返回前再次复查读取权限和 epoch，防止在途撤销泄露旧档案。
- Web 主入口为账号观察，旧精确范围向导置于高级兼容区；显示宿主积压/丢弃、Companion 归档状态、发现对象和授权档案。
- 本地管理员 HTTPS `observation-admin/status`、`enroll-default` 与 `scripts/observation_admin.py`；客户端不创建 Platform 实例，查询状态不会禁用已有 v1 连接。

## 实际验证

- Ruff 检查通过；`test_bot_observation.py` 5/5，含在途撤销交错；`test_bot_adapters.py` 13 通过/1 既有跳过；`test_bots.py` 12/12。
- 三产品合成联合测试 1/1；真实 TLS/HTTP 联合测试 1/1，实际初始化 Companion `build_runtime`、Memory `configured_app` 与 Platform，并验证 Memory 暂停恢复、策略待定 503、历史撤销 409、已存档只按新 epoch 读取、没有发送或生成副作用。
- Prettier、TypeScript、Vite 构建通过；Playwright `bot-adapters.spec.ts` 桌面与移动共 44/44。
- 旧 `test_bot_adapters_host_joint.py` 写死原相邻工作区路径，在隔离 worktree 中不能导入；新 `test_observation_http_joint.py` 使用显式产品路径且通过。`pnpm run build` 的脚本内部调用本机不存在的 `npm`，等价的 `pnpm run typecheck` 与 `pnpm exec vite build --config apps/web/vite.config.ts` 均通过。

## 部署与验收接口

- Platform Memory 服务 principal 增加 `observation.verify` 动作；Core URL/服务凭据与现有 `core` 配置相同。管理员 Bearer 需 `bot.manage`；CLI `status` 只读，`enroll-default` 输入 `adapter,address,access_key,allow_private_http,ca_pem,account_id,name`，不得使用生产明文配置入库。
- 先完成 Memory 明确迁移、Companion 和宿主 0.3.0 升级，再按账号执行 `scripts/observation_admin.py` 的 `enroll-default`。状态回读需看到默认 `observe_only`，无 actor、无会话和作者预录入；旧 v1 连接仍启用。
- 候选合同在根仓库 `contracts/observation-source/candidate-v1`，仍未冻结；当前三产品运行时并不加载该候选 JSON Schema，由各端严校验和跨产品样例测试验收。

## 未完成与风险

- 未连接真实 QQ/NAS、未迁移生产数据库、未推送或发布。租约内宿主已持久认领的事件在 Platform 临时故障时可等待恢复；不能承诺其他宿主插件一定不回复。宿主 pending/dropped 必须运维监视。
- Linux 镜像启动前复核 TLS CA 路径、Platform `observation.verify` principal、Companion/Memory caller、Memory schema 迁移结果；在合成账号先只观察，再显式放开回复。

## 引用

- Companion、Memory 同名交接文件；根候选合同 README、schema、正反例与 manifest。
