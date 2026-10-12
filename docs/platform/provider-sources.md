# 供应商与多个模型

入口：任务与设置 → 模型供应商（`#/settings/2`）。一个供应商保存一次名称、HTTPS 基础地址和 API Key；其下可添加多个模型。模型分别设置名称、准确上游 ID、对话/嵌入/重排序类型、启停与测试。目录支持搜索、多选添加和手动填写；添加、获取目录不会自动发送收费测试。

## 使用

1. 添加供应商并保存地址、Key。
2. 获取模型列表，选择类型并勾选多个模型，点击“添加所选模型”；目录不支持时手工添加模型 ID。
3. 对要使用的模型单独测试。对话模型通过既有短回复测试后，可设默认或在“模型功能分工”中分别选择。角色管理继续支持独立聊天模型。
4. 嵌入与重排序在“记忆检索”选择相应类型的模型，直接共用此供应商的连接。选择不自动开启检索模式，也不改变自动记忆开关。

不同供应商下的同名模型分别保存。同一供应商、模型 ID 和类型重复添加时返回已有模型。类型是用户的接口选择，目录发现不等于能力验证。当前仍沿用已适配的 Chat Completions、`/embeddings`、`/rerank` 协议；未新增 Responses、语音、图片、代理或自定义请求头适配。

修改模型 ID、类型或启停只使本模型的旧测试及绑定失效；修改供应商地址、Key 或启停使其全部模型失效。仅改供应商名称或模型显示名称不打断既有选择。删除一个模型保留供应商与其他模型；删除供应商会删除其所有模型。失效的独立功能/角色绑定不会自动换成别的供应商。

## 数据归属与升级

复用 `ProviderCatalog` 的加密目录、SQLite 事务、CAS、测试回执、默认指针与功能绑定。`provider_sources.py` 管理连接与模型归属，`provider_values.py` 集中连接值校验，`ProviderManagement` 沿用原同源管理员授权。没有第二份凭据库或新增网关协议。

`provider_sources` 保存共享连接和唯一密文；原 `providers` 表现在保存模型条目。历史 `provider_id` 仍是稳定的模型条目引用，默认、角色、功能绑定、已签发版本及测试记录保持原编号与修订。首次打开旧库在事务内创建 SQLite 备份 `providers.pre-sources-<随机值>.sqlite`，随后拆分原单模型条目；不会按相同 URL 猜测合并不同连接。新建目录直接使用新结构；再次启动不重复迁移。

备份继续包含整个私有目录及加密 Key。旧程序不能直接读新结构；回退须停止写入并恢复升级前数据库与对应 Key，不能只恢复其中一个文件。本批只在临时合成库验证，没有迁移线上数据。

旧单模型 save/clear-key API 仍服务既有调用者，内部写同一份共享结构。新网页只用以下增量：

| `POST /api/web/providers/` 后缀      | 内容                                                                                       |
| ------------------------------------ | ------------------------------------------------------------------------------------------ |
| `view`                               | 新增 `sources`；原 `providers` 仍是模型投影，增加归属、类型与供应商启停信息                |
| `source-save`                        | client_id、name、base_url、enabled；编辑加 source_id/expected_revision，api_key 留空保留   |
| `source-models`                      | source_id、expected_revision，返回上游模型目录；迟到的旧连接结果拒绝                       |
| `source-clear-key` / `source-delete` | client_id、source_id、expected_revision                                                    |
| `model-add`                          | client_id、source_id、expected_revision、models（模型 ID 数组）、kind（text/embed/rerank） |
| `model-save`                         | client_id、provider_id、expected_revision、name、model_id、kind、enabled                   |

原 `test/default/function/delete` 继续以模型条目编号操作。嵌入/重排测试沿用既有网关适配器并提交固定短样本，密钥不进入网页投影。客户端错误及重试规则、Cookie/CSRF/Origin、模型管理权限保持原边界。

检索配置以 `{provider_id}` 引用模型，不复制供应商密钥；替换旧独立连接时清除旧密文。运行快照包含实际模型修订，连接改变使配置指纹和嵌入空间指纹变化，防止复用旧模型向量。旧独立检索连接继续可读和使用，页面保留其原设置，便于逐项切换。

## 验证入口

沿用现有 Python unittest/pytest、TypeScript、Vite 与 Playwright。专项为 `test_provider_sources.py`、`test_provider_catalog.py`、`test_model_functions.py`、`test_memory_retrieval.py`、`test_role_runtime.py` 及 `test_retrieval_integration.py`。浏览器用 `playwright.providers.config.ts`，现在是真实临时平台后端与合成网关，覆盖桌面/手机多选、保存、刷新、测试、功能分配、独立启停、别名和删除、密钥不回显及检索共享连接；角色路由及真实 Gateway/Memory/HTTPS 录制模型由后端用例覆盖。准确结果见 [TS-125 交接](../handoffs/TS-125.md)。
