# 嵌入与重排序设置 HTTP 兼容修复

状态：本地验证完成，未合产品 main、未推送、未部署。
基线：Platform `c591a5ddda34b31cd0e9e55f46cd7d50fae36dae`。
分支：`codex/retrieval-http-fix-20261029`。

保存嵌入、重排序和检索模式时，`RetrievalSettings` 直接调用
`crypto.randomUUID()`，在不提供该接口的局域网 HTTP 页面上请求尚未发出就报错。
改为复用 `app/requestId.ts` 的现有 Web Crypto UUID v4 生成函数。
未改 API、模型配置、密钥、权限、数据库或模型执行规则。

TypeScript、Vite 生产构建、改动文件格式与 diff 检查通过。
定向浏览器最终 4/4 通过：桌面与手机分别验证独立连接及六种检索模式、
供应商嵌入/重排序选择保存和刷新保留；所有用例显式禁用 `randomUUID`。
新增用例完成后恢复合成独立连接，避免共享测试库影响下一设备项目。
原先环境启动失败和测试清理失败不作为产品通过证据；修正运行路径和合成清理后取得上述结果。

运行使用现有 TS311 `.runtime/q21-venv`，合同在本任务外层 `test-inputs/contracts`
内按当前源码固定 hash 对齐；未改共享环境或已发布合同。
未执行真实模型调用、真实 QQ 操作或生产验收。下一步由发布协调者集成本提交，
按届时实际线上版本串行发布前端，并核对受影响静态资源。
