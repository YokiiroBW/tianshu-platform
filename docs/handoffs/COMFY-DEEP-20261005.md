# ComfyUI 深度接入 · Platform 本地交接

## 基线与提交

- 工作树：`worktrees/comfy-deep-20261005/platform`，分支 `codex/comfy-deep-20261005`。
- 基线：`c87c53bcb7604d93f392cf1613049ac8567baa88`。
- 实现提交：`78f333a6e59e4921cf1290c9e9dcb48e86295ac1`。
- 状态：本地开发及相关验证完成；本交接随后单独提交。集成、远端推送、NAS 部署和真实使用由协调者另行编排。

## 实现与复用

生活页“衣柜与相册”增加 ComfyUI 管理入口：实际连接状态、工作流目录、模板选择、节点自动识别及候选下拉修正、模型辅助选图/适配、逐角色绑定、固定形象和拍摄默认值、横竖尺寸预设、结构化提示词及完整 API 工作流编译预览。形象字段留空继承所选工作流；没有衣柜记录不禁用拍照。供应商当前仅 ComfyUI，NAI/在线 API 没有虚假可用选项。

`WebImageBackend` 复用 `WebLifeManagement.scoped_actor/guard/_call`、既有 Cookie/CSRF、管理身份和 Companion Life 授权。新代理为 `/api/web/life/image-backend/{read,manage,compile}`，连接保存仍走既有 `save` 路由；浏览器不能直接提供 `credential_ref` 或调用 `connection.configure`。响应核验 request/actor，剔除浏览器不需要的凭据引用。原标准工作流保存接口保持兼容。

全局连接凭据集中到现有加密 `external_catalog` 的 `images:connection`，没有新目录、权限或图像任务账本。留空仅复用 Companion 当前已确认、同 origin 的凭据；可迁入原 actor 记录，旧引用仍保留。换地址留空时新连接不带旧地址的 token，原加密记录保留，显式清除才移除。旧版本表单在写凭据前与 owner 版本核对：版本不同只调用 owner 原回执/版本冲突路径，不修改 secret；同 client ID 可恢复已受理操作。

创作继续使用原 `image.request/image.cancel`、`image_jobs`、原图及相册链，新增活动、动作、镜头、补充提示词和方向预设。可先编译预览，再登记原图片任务。状态轮询沿原可见性暂停逻辑；取消、未知结果与原任务保持原语义。模型辅助分析/编译及图片请求采用单请求 120 秒总预算；普通读和配置保持既有预算。没有自动重试；图片传输中断明确返回结果未知，原幂等请求仍可查询。

## 合同闭包

运行合同来自协调工作树 `worktrees/quality-life-20261003/coordination/contracts`，本产品不保存第二份 schema 草稿。

- `image-backend/v1`：`62e71dd2f42fb5b1376c439c362a555619da41cb7f7ae26d07acc1182b80ffbd`。
- `life-runtime/v2`：`85aff438f91cb96876e259204b5a198e2fedaead56db64a2265aa520a59ea7e3`。
- `bot-delivery/v2`：`edec27d83b8b9427656096d45818d9db3665d8d7bd82cc796805e21ba35b757b`。
- `knowledge-content/v1`：`75d210454102af5af505ec1f72d69cc7dfd344550f7cf90e70125c082fdf4cd7`。

## 实际验证

使用已有解释器 `.runtime/nas-a1-r1-venv/Scripts/python.exe` 和相同锁文件的既有 Node 依赖（忽略的 `node_modules` junction）。没有新增依赖。

- `test_web_image_backend.py`：**11/11**。真实 loopback HTTP、真实加密目录与合成 owner，覆盖固定代理、身份复核、两角色留空保留、换 URL、旧 ref 迁入、显式清除、过期表单不改凭据、同 ID 重放及模型辅助单总预算。
- `test_web_console.py`：**8/8**。复用会话、CSRF/Origin、过期/撤销与真实本地登录相关检查。
- `test_c4_proofs_credentials.py`：**3/3**。原 narrow credential resolver/source 路径结果复用；这几段代码本次未改。
- 修改 Python 文件 `ruff check`、新增/修改叶子模块与测试 `ruff format --check`、`git diff --check`：通过。既有大入口未作无关整体重排。
- 所有改动前端文件 Prettier：通过；`tsc -p apps/web/tsconfig.json --noEmit`：通过；Vite 生产构建：通过。既有 renderer 大 chunk 警告仍在，本轮没有改变它。
- 浏览器最终定向：**10/10，0 skip**；桌面 1440×1000 和手机 390×844 各覆盖 3 个新 ComfyUI 流程及 2 个既有媒体/原图流程。新流程覆盖保存连接、自动节点与修正、角色默认值、模型分析、编译不生成、结构化图片提交、离线状态和角色切换清理；无 JS pageerror、无横向溢出。

浏览器命令：

```text
node node_modules/@playwright/test/cli.js test --config apps/web/playwright.config.ts comfy-backend.spec.ts life-runtime.spec.ts --grep "ComfyUI|model adaptation|changing actors|sampled media|image preview reads" --workers=2
```

初轮新增测试的 TypeScript `.find()` 判空与内嵌 select 的精确标签定位问题均已修复；最终检查全部通过，没有把中间失败计为通过。

## 截图与验证边界

截图为合成网页会话，保留在本工作树的忽略运行目录，已人工查看桌面连接页与手机节点编辑页：

- 桌面目录：`apps/web/test-results/comfy-backend-ComfyUI-work-91ab3-efaults-and-compile-preview-desktop/`。
- 手机目录：`apps/web/test-results/comfy-backend-ComfyUI-work-91ab3-efaults-and-compile-preview-mobile/`。
- 两目录均包含 `comfy-connection.png`、`comfy-bindings.png`、`comfy-preview.png`、完整 `comfy-management.png`。

Platform 测试使用真实代理和合成 Companion owner；浏览器使用受控路由夹具。**未在本子任务运行真实 Platform→Companion 工作流联合、真实 8188 GPU 生图、收费模型、真实 QQ 发送、NAS 配置或部署**。后续协调者按固定双方提交做联合，并复用本轮未改变的相关检查。
