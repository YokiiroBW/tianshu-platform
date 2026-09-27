# CONNECT-B 项目知识只读接入交接（2026-09-27）

## 固定基线与范围

Platform 产品分支 `codex/connect-backend-20260927` 上固定提交 `3b0dc2473d60b3b359c4f31f1e14d3bde59fb1a9`，父提交 `bc45342babac2c7597987d0edb4f8a9f27763023`。Memory 产品冻结交付 `02df5ba8c041a7a6eb8b705c5f264e10543032ec`（其中四项 HTTP 窄扩展由 `bd0123bc7e242dc5a767347602f23957af9b2c33` 引入）。

本提交给现有 `knowledge.read` 浏览器连接器增加 `/api/web/knowledge/{lessons,experiences,continuation,continuation-check}`。项目和 checkout 别名从 `web_knowledge.projects[{project_id,label,checkouts:[{id,label}]}]` 的部署闭集选择；用户请求不携带上游 URL、账号、Bearer、路径、原始封装包或任意 operation。`experiences` 固定传当前项目作为 Memory `experience_query.arguments.project_id`，仍由 Memory 独立验证完整证据项目和 `review` 授权。缺新 `http_read_operations` 时返回 `knowledge_operation_not_enabled`，不冒充网络断连。

交接恢复只在用户主动请求时读登记 checkout。Memory 完整包不出浏览器，Platform 每会话最多存 4 个、全进程最多 16 个且总计不超过 4 MiB，单包受 256 KiB 答案上限，15 分钟绝对失效。handle 绑定原会话对象和项目，登出立即移除；账号权限、知识连接配置或服务凭据更改由现有会话指纹使其失效，进程重启则全部消失。`continuation-check` 将当前会话原包交给 Memory 真实重新检查，返回有效性、差异、观察时间；页面不凭缓存判定现状。封装摘要只投影 checkout id/分支/HEAD/脏状态、索引计数、项目声明状态与预算/省略，不透传 seal、文件 locator、完整 Git/文件快照。Memory 的 checkout 别名和项目授权仍是第二道边界。

## 实际验证

在本 Platform worktree 设置以下三个环境变量后运行。`TS_CONNECT_M_PATH` 应指向上述固定 Memory 提交的独立检出；测试只从该目录加载代码与只读 fixture，合成数据库/Git 仓库/凭据/证书均在 Platform 测试临时目录，未读取生产数据。

```powershell
$env:TS012_CONTRACT_DIR='C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
$env:TS013_TLS_PYTHON='C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$env:TS_CONNECT_M_PATH='C:/YOKI/Codex/tianshu-peiban-bot/worktrees/CONNECT-M/tianshu-memory'
$env:PYTHONDONTWRITEBYTECODE='1'
& .runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_knowledge_joint.py -v
& .runtime/venv/Scripts/python.exe -m unittest discover -s tests/backend -p test_web_readers.py -v
& .runtime/venv/Scripts/python.exe -m ruff check services/platform/web_readers.py services/platform/web_console.py tests/backend/test_knowledge_joint.py tests/backend/test_web_readers.py
```

结果：真实跨产品 4 项、合成读者边界 5 项、ruff 均通过。真实跨产品用 Platform 实际网页登录/同源路由 → TLS → Memory 实际 `knowledge_cli serve` 子进程和私有合成 SQLite/Git。分别核到非空 `query`/`note_query`、非空 `document_list`/`document_read`、非空 `lesson_query`/已审 `experience_query`、`continuation_recover`/`continuation_check(valid=true)`。撤销 HTTP 读授权后页返回明确未启用错误；未登记 checkout 别名在 Platform 拒绝，不把原始包/路径回送网页。`git diff --cached --check` 在固定提交前通过。

## 后续

此为本地隔离联合证据，NAS 尚缺独立 Knowledge 服务进程、受控 TLS/Host、客户端身份与逐项目/逐操作权限、工作树登记及 schema/数据实际验收。浏览器真实 Chromium 链由 CONNECT-U 与总控联合；总控独占现场更新。前端字段契约在 `CONNECT-B-API.md`，总控审查时以固定代码及本交接为准。
