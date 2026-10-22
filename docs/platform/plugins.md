# 通用插件宿主与 Python SDK

TS-237，2026-10-20。本地实现沿用 TS-167 候选规范；尚未部署 NAS，也没有向公共包仓库发布 SDK。

## 可以使用的功能

网页 `/#/plugins` 提供已安装、发现和开发入口。可以安装内置示例、ZIP、服务器开发目录或固定到完整提交 SHA 的 HTTPS Git 仓库；同一包可以创建多个实例，分别保存配置、加密密钥、状态与工作区绑定。

每个实例在独立 Python 进程运行，依赖位于包内容版本自己的虚拟环境。主站不导入插件业务代码。插件必须是安装者信任的代码：进程分离不是 Python 安全沙箱，也没有容器级 CPU、内存或文件权限隔离。

当前提供 `state.kv`、`http.client`、`secrets.read`、`jobs.manage` 和 `notifications.emit`，共十二个有机器输入/输出 Schema 的操作。配置、日志和受管后台任务由 SDK 提供。支持动作、自动参数表单、独立页面、持久回执、取消、检查点、显式恢复和结果核对、周期调度、版本切换及失败恢复。

角色技能继续由 Companion 管理。插件导出的命令和模型工具描述目前没有接入聊天路由；事件、完整消息桥、群管理、模型、文件和下载提供者，以及 MoviePilot 业务插件仍为后续功能。缺失能力会明确报不可用，不能借 Companion 身份执行。

## 插件目录

```text
my-plugin/
  plugin.json
  main.py
  config.schema.json       # 可选
  contributions.json      # 可选：动作描述必须与 SDK 导出一致
  requirements.lock       # 有第三方依赖时声明 dependency_lock
  panel.html              # 可选自定义页面
```

最小 manifest：

```json
{
  "schema_version": 1,
  "id": "example.hello",
  "name": "回声",
  "version": "1.0.0",
  "description": "返回输入文字",
  "author": "Your name",
  "entrypoint": "main:Hello",
  "sdk": { "min": "1.0.0", "max_exclusive": "2.0.0" },
  "capabilities": { "required": [], "optional": [] }
}
```

`main.py`：

```python
from tianshu_plugin import Plugin, action


class Hello(Plugin):
    @action(
        id="echo", title="回声",
        input_schema={
            "type": "object", "required": ["text"], "additionalProperties": False,
            "properties": {"text": {"type": "string", "minLength": 1}},
        },
        output_schema={"type": "string"},
    )
    async def echo(self, call, args):
        return args["text"]
```

普通插件不需要 Node、前端或数据库。完整示例位于 `services/platform/plugins/builtin/hello` 和 `http-query`。安装包字段的本地 Schema 位于同模块 `schemas/`；注册动作从 SDK 导出，不在 manifest 复制参数。

SDK 可单独构建为无第三方运行依赖的 wheel：

```text
python -m pip wheel --no-deps --no-build-isolation -w .runtime/wheels ./tianshu_plugin
```

开发环境从生成的本地 wheel 安装；没有假设 PyPI 上已经存在此包。Platform wheel 同时携带当前 SDK、工作进程与页面桥。

## 配置、凭据与范围

配置采用 JSON Schema 2020-12，普通字段自动生成表单。顶层 string 属性可标记 `x-secret: true`；密钥只写，页面只显示是否已配置。实例配置和密钥更新在同一事务提交，修改携带 `expected_revision`，旧表单不能覆盖新设置。

HTTP 认证通过 `auth={"secret_name": "api_key", "scheme": "bearer"}` 引用实例密钥。宿主向配置的 base_url 同源地址注入认证，不把明文返回给工作进程；也支持 `x-api-key`。请求不跟随重定向，HTTPS 使用正常证书校验，本地 HTTP 服务可显式填写。

`binding_overrides` 列出允许按工作区覆盖的普通配置字段；对象递归合并、数组整体替换。删除覆盖时从 overrides 删除对应键；`null` 按 Schema 的真实空值处理。网页动作和独立页面可选择工作区绑定。机器人/角色范围的合同描述可以保存，但其路由提供者尚未接入；绑定从不授予群权限。

每次动作固定包版本、有效配置、绑定和加密凭据快照。网页身份与权限在执行和能力调用时重新核对。任务和事件不借用已过期网页登录会话的权限；当前周期动作以实例自动化身份运行，只能使用该实例已声明的现有能力。

## 回执、恢复和周期任务

网页提交 `client_request_id`。同一意图重试保留此 ID；参数改变时拒绝冲突。明确再次操作使用新 ID。页面响应丢失后同内容重试会查询原意图，不能重新提交外部写入。

回执分别记录 `execution`、`effect`、`external`、`delivery` 与单调 `revision`。动作受理不等于外部下载完成。写入超时或进程中断后保留 unknown，不自动重放。`PluginError` 可明确报告 `not_applied`、`applied` 或 `unknown`；写动作成功须返回 `ActionResult`。

`call.checkpoint()` 持久化业务阶段，恢复和核对分别调用声明的 `resume_handler` / `reconcile_handler`，沿用原 invocation 和 intent。核对处理器应查询原外部影响，不重新创建业务资源。取消会停止本地进程工作，不承诺撤销已送达外部服务的请求。

周期任务支持 interval 和五字段 cron、IANA 时区、`misfire=skip/coalesce` 与 `overlap=skip/queue_one`。不补跑全部历史周期。Windows 的非 UTC 时区需要可用的 IANA 时区数据；Linux 通常由系统提供。

版本切换先保存实例配置和 KV 的持久快照，再调用配置与数据迁移；失败恢复旧实例和状态，宿主重启也恢复未提交的迁移。实例数据只通过受管 KV 写入；插件任意私有文件与外部服务的数据迁移不在该事务内。卸载停止实例，保留实例数据、历史记录、包缓存及所有外部资源。

## 独立页面

静态 HTML 可以包含打包完成的 JS/CSS 和 HTML 中引用的相对静态资源。JS 模块和 CSS 内部引用应在插件发行前打包，不依赖宿主构建器。

页面由专用 GET 响应提供，先通过现有会话与 CSRF 取得单次短期页面票据。iframe 与响应 CSP 都启用 `sandbox allow-scripts allow-forms`，不授予同源权限；CSP 禁止直接网络和表单导航。主站 CSP 不放宽。页面只能通过实例绑定的 MessageChannel 使用 `getContext`、`callAction`、`getInvocation`、`subscribeInvocation`；文件选择尚无提供者，会明确拒绝。

`window.tianshu.call(method, input)` 在 `tianshu:ready` 后可用。动作必须属于该 View 声明的 actions。主题令牌和 `tianshu:themeChanged`、`tianshu:invocation` 事件由宿主推送；iframe 不读取主站 Cookie 或插件密钥。

## HTTP 与运行文件

控制面使用项目现有同源 POST 约定：`/api/web/plugins/{view,catalog,install,installation,create,configure,enable,disable,bindings,invoke,invocations,invocation,cancel,reconcile,resume,upgrade,uninstall,logs,schedules,schema}`。沿用当前平台 operator 的 `config.publish` 权限，不增加密码解锁步骤。

`POST /api/web/plugins/schema {}` 返回与服务端校验相同注册表生成的 OpenAPI 3.1 和宿主操作 Schema。`view-content` 签发页面票据，`GET /api/web/plugins/page/{ticket}` 只返回对应页面。任务中心只读聚合 `platform.plugins` 台账，不另建执行路径；页面状态采用有 revision 的轮询，当前没有 SSE 端点。

默认在 database_path 附近保存 `.plugins.sqlite`、`.plugins.key`、`.plugins.owner` 和 `.plugins/` 包缓存。原生 PG 模式将插件表登记到既有存储运行时，仍保留本地密钥和文件锚点。备份必须同时保留业务库、密钥及需要恢复的包版本；不能只恢复数据库或生成新密钥冒充恢复。

可用 `plugins.package_directory` 指定包缓存绝对路径。实际部署时应沿现有分层存储把包缓存放机械盘，活动库和密钥留在配置/数据盘。本地开发没有改 NAS 的路径、挂载或备份工具。

同一个文件锚点只有一个插件监督者。另一个 Platform 进程可以继续服务原有 API 和只读记录；插件写操作不会抢走正在运行的监督者。当前不支持多主节点同时调度同一实例。

## 验证入口

```text
python -m unittest discover -s tests/backend -p "test_plugins*.py" -v
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.plugins.config.ts
```

HTTP 测试沿既有 `TS012_CONTRACT_DIR` 指向完整冻结合同快照；浏览器使用自己的临时本地服务与合成查询接口。浏览器环境的 `TS237_PYTHON` 指向已装项目依赖的解释器。真实 PG 测试须显式提供隔离 `TS237_TEST_POSTGRES`，否则记为跳过。

实际结果和未完成边界见 [TS-237 交接](../handoffs/TS-237.md)。本地结果不代表 Docker/NAS、真实外部 Git 依赖安装或 MoviePilot 的业务验收。
