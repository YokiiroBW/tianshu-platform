# TS-011 · 小屋环境样片

2026-09-14。本地可运行的环境预览，入口 `/#/room`。基于 TS-010 的 `654a031669e3f50704ad5fd2b49c2599e4115daf`；沿用 React/Vite 壳、懒加载入口与唯一 CSS 令牌。

## 行为与边界

固定正交相机呈现一间开放剖面小屋。窗扇向外开启，玻璃不遮断日光；纱帘半透明且柔化直射光，遮光帘以真实网格参与投影。方向日光随示意时间连续改变角度、亮度和颜色。书桌灯、床头灯分别使用带距离衰减与阴影的聚光灯，桌椅、窗框、家具及角色几何参与遮挡。未使用底图、整屏滤色、后处理光晕或第二套引擎。

窗口和两层帘从当前值趋近新目标，重复操作只更新目标。帘顶在轨道上，底部按局部网格形变摆动；外来风随开窗增大，关窗后指数衰减。两层帘有不同摆幅与相位，侧向摆幅限制在两层之间的空隙内；这是受限视觉模型，没有布料碰撞解算或真实气象模拟。

默认固定预览 14:00，支持分钟滑条与四个时段快捷键。跟随本机时间使用 Asia/Shanghai 时区；06:00–19:00 是虚构晴天曲线，未读取位置或真实天气。每个物件独立保持手动，恢复自动才重新消费本地示意规则；灯的亮度与光色属于同一恢复组。光色是艺术化冷暖插值，界面温度为近似值，未经色温校准。

没有核心房间合同、API、服务端时钟、版本、权限、持久状态或远端命令。离开页面重置预览，跟随本机时间也不代表接入生活服务。未验证关网页持续生活、跨浏览器一致性、角色真实活动或 HA 联动。

## 渲染路线与素材

当前主线仅 Three.js `0.186.0`（配套 `@types/three 0.186.0`）。已在本机 npm 注册表核定并精确锁定。固定视角的 2.5D 构图、低饱和木色/雾蓝/淡紫参考主工作区 `output/ui/product-pages-v1/01-character-home.png` 和 `output/ui/room-environment-v1/day-cycle-study.png`，没有将概念图当运行背景或运行证据。

`@types/three` 自带 Rapier、tween 等传递开发依赖，锁文件保留其上游声明；本样片未导入这些运行时，没有布料物理引擎或第二条渲染路线。

| 对照                     | 当前证据 / 成本                                                                                                                                     |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Three.js 正交 3D         | 本样片实测窗框、家具遮挡与三光源投影；需要几何、材质和阴影预算。场景代码约 30,802 个渲染三角形、423 次绘制调用（含当前阴影通道，见测量记录）。      |
| 既有 Phaser/Pixi 2D 候选 | 本次仅与已有分层/静态方案作需求对照：保持绘画风格有优势，但移动光源与遮挡需要额外法线/遮罩素材。未安装、运行或做性能 A/B，不声称已证实 3D 优于 2D。 |

选择 Three.js 作为此样片唯一主线。最终美术资产与实体手机性能仍需后续验收，不在此冻结完整生产资产管线。

全部房间、窗扇、窗帘、书架、桌椅、床、灯、植物、窗外剪影、角色和衣物均由本任务生成几何；无外部图片、纹理、模型或第三方角色资产。人物为明确标识的静态阅读姿态占位，未实现骨骼、表情、呼吸、行走、角色换装或衣物动画。已实现的动画只有环境开合、风摆与灯亮度/光色过渡。保留家具作为遮挡验证，不能按这些占位尺寸制造真实资产。

参考机制：[正交相机](https://threejs.org/docs/pages/OrthographicCamera.html)、[聚光灯](https://threejs.org/docs/pages/SpotLight.html)、[WebGLRenderer](https://threejs.org/docs/pages/WebGLRenderer.html)。实际行为以锁定版本源码与本样片测试为准。

## 生命周期与画质

- 一个调度者、最多一个待执行 RAF；连续场景以 30 Hz 等待间隔调度，不能把该设置当实测帧率。帧间隔有上限，后台恢复不补播旧帧。
- 隐藏时取消 RAF/计时器，回到前台用当前本机时间/预览目标重新求值；页面的本机时钟更新也暂停。
- 系统减少动态或壳的外观偏好任一要求减少动态，立即停止摆动和开合插值，正确显示最终状态。固定时间下空闲不连续绘制；跟随时钟只低频更新。
- 卸载断开 ResizeObserver、偏好观察与事件，清理几何、材质、阴影及 renderer，主动释放 WebGL context。每次挂载拥有独立 canvas，兼容开发模式 StrictMode 的重复 setup/cleanup。
- WebGL 2 不可用显示明确降级说明，普通控件仍可使用；重试保留页面内设置。实际 context lost/restored 有中断与恢复显示。
- 默认 DPR 上限 1.5，日光阴影 1024、两灯各 512；简化画质 DPR 上限 1，阴影分别 512/256，保留局部照明与遮挡。

## 复现与证据

安装、开发、构建沿用根 README/AGENTS 的命令。新增 `npm run test:room`：在已构建产物上运行小屋浏览器测试，单工作者避免同机并发 WebGL 扭曲测量。受影响壳检查为 `npm run test:e2e -- room.spec.ts shell.spec.ts --workers=1`。

本机没有 PATH npm，实际经 README 的捆绑 pnpm 启动 npm 11.6.2；亦使用清单对应的本地 Node 程序：`node node_modules/typescript/bin/tsc -p apps/web/tsconfig.json --noEmit`、`node node_modules/vite/bin/vite.js build --config apps/web/vite.config.ts`。Node 24.19.0。

小屋 + 壳套件 **31 通过、1 跳过**；跳过是桌面项目中的手机导航专用项。之后增强的真实帘摆像素和壳减少动态偏好用例通过桌面、手机两项定向检查。帘摆检查比较实际 WebGL 截图，像素因果检查比较日照、两帘、单灯和双灯的真实截图，未使用 CSS 夹具或批准截图基线。初始化失败用 WebGL 获取失败注入；context lost/restored 使用浏览器真实扩展。隐藏采用明确的 visibility 事件夹具，不等于实体手机系统后台验收。

浅深主题、键盘滑条/开关/折叠、200% 文字、桌面和手机无横向溢出、axe WCAG A/AA 检查通过。已目视检查取景、家具与窗框投影、两灯区域、移动单列控件与焦点可见性。开发模式另实测 4 次进入与画质切换，始终一个 canvas，无页面异常，旧 context 已释放；测试与开发服务均已关闭。

实际 Windows Chromium Headless **153.0.8010.12**，ANGLE Vulkan **SwiftShader 软件渲染**，DPR 1，正常动态，热场景单工作者 2 秒样本：1440×1000 桌面视口约 **23.6 帧/秒**；390×844 手机模拟视口约 **20.3 帧/秒**。这些只是本次自动化环境的交付帧率，不代表显卡、实体手机、Safari 或线上 p75 性能。原始数值及像素记录见 [browser-measurements.json](evidence/browser-measurements.json)。

主 JS 229.05 kB / gzip 72.98 kB；小屋懒加载 JS 551.15 kB / gzip 140.77 kB、CSS 4.19 kB / gzip 1.10 kB。构建保留小屋 chunk 超过 500 kB 的提示；没有修改阈值掩盖它。非小屋入口未请求该 chunk 或创建 WebGL，壳回归已验证。

| 实际截图          | 桌面                                                                                               | 手机模拟视口                                                                                     |
| ----------------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 清晨 / 午后       | [清晨](evidence/desktop-morning.png) · [午后](evidence/desktop-afternoon.png)                      | [清晨](evidence/mobile-morning.png) · [午后](evidence/mobile-afternoon.png)                      |
| 黄昏 / 夜晚       | [黄昏](evidence/desktop-dusk.png) · [夜晚](evidence/desktop-night.png)                             | [黄昏](evidence/mobile-dusk.png) · [夜晚](evidence/mobile-night.png)                             |
| 关闭玻璃 / 遮光帘 | [关窗](evidence/desktop-day-glass-closed.png) · [拉帘](evidence/desktop-day-blackout-closed.png)   | [关窗](evidence/mobile-day-glass-closed.png) · [拉帘](evidence/mobile-day-blackout-closed.png)   |
| 单灯局部光        | [书桌灯](evidence/desktop-night-desk-only.png) · [床头灯](evidence/desktop-night-bedside-only.png) | [书桌灯](evidence/mobile-night-desk-only.png) · [床头灯](evidence/mobile-night-bedside-only.png) |
| 整页与控件        | [浅色](evidence/desktop-light-page.png) · [深色](evidence/desktop-dark-page.png)                   | [浅色](evidence/mobile-light-page.png) · [深色](evidence/mobile-dark-page.png)                   |

主协调者额外授权 `.gitattributes`（Git 自动识别文本并以 LF 入库，图像/GLB 明确二进制）和 `.prettierrc.json`（`endOfLine:auto` 兼容现有 CRLF 检出）。原有 21 个未改文件的默认格式检查曾只因行尾失败；新增规则后全仓现有格式检查通过，没有批量重写业务文件或改全局 Git 配置。

未验证：实体手机、浏览器后台节流/休眠的系统级差异、Firefox/Safari、读屏器、长期运行内存/驱动 VRAM、真实核心状态、跨浏览器一致性、完整美术角色资产与天气/天文准确性。
