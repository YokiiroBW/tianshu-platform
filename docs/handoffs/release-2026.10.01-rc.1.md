# 2026.10.01-rc.1 正式关系合同绑定

固定交付基线 e3257a2cafab24c473502ecb2c55f302deffb86d，正式根合同提交 37b086f66ca8521ec065578b9126111d97d0f112。schema LF SHA256 e96397bac2b6ad8ff9d23c023d7d3c5ba0701734b27053a05b9d0f65a7ff8ee6。配置 candidate_schema_path 保留为既有兼容键，正式值为 contracts/role-relationship/v1/schema.json。服务端 DTO 验证与关系测试绑定正式 schema；没有改网页、权限、评分或迁移逻辑。

2026-10-01 本轮实际验证：test_relationships.py + test_relationship_tls.py 共 18 passed / 3 subtests；固定三产品 test_relationship_joint.py 5 passed / 2 warnings，58.90s。三产品测试导出固定 Memory 1f3121c9758faeb31fc9d0fe2a54974c72505d55 和 Companion 21e4ff37f1d4f4e3e9db94f8c965031a8dcb9cd6 的 Git 归档，运行真实产品 HTTPS、来源/当前授权/关系账本和 SQLite；账号、模型与外部渠道是合成组件。首次三产品运行因缺少 TIANSHU_CONTRACTS 环境变量 setup 失败，修正后 5 项完整重跑通过；此前通过的 18 项未重跑，未删例/跳过失败。

前端代码输入未改，沿用 TS-116 的 12 项桌面/手机浏览器实际验收及静态构建证据，不声称本轮重跑浏览器。此前完整后端 889 passed / 53 baseline failed / 14 skipped / 2 deselected / 742 subtests 属此前记录，本轮没有重跑或宣称全绿。具体基线归因保留在 TS-116 交接与协调发布清单。

当前发布分支不等于生产版本；既有 main 含未上线 QQ 身份，不能盲替线上。GitHub 远端核验、一致备份与真实恢复、显式关系/alias 迁移、实际权限与 NAS 镜像验收仍待执行。不生成新权限或猜用户账号绑定。
