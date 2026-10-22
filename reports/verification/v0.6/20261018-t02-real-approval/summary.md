# V0.6-T02 真实审批卡片验证

日期：2026-10-18，Asia/Shanghai。**本次真实投递、PATCH、重启后签名/AES 批准回调、单次决定与终态显示通过。用户确认“已批准，按钮已消失”。** 真实飞书配合隔离的 Mock/Fixture 业务运行时及 SQLite；真实审批人经服务端目录映射，审批发起人为独立合成主体。不包含真实业务执行、模型调用、PostgreSQL API 或 Job/Graph 恢复。

## 真实结果

| 项目 | 结果与证据 |
| --- | --- |
| 应用/群/审批人 | 用户手动发布独立 OriaTest 0.1.0 并入群；四次只读 API 均 HTTP 200 / code 0，机器人已激活，唯一匹配测试群及 测试操作者。[发现记录](discovery-014624.json) |
| 平台地址校验 | 用户确认保存成功，本地收到真实 URL challenge 并返回 200；该校验请求没有签名头，按官方 challenge 流程核验 token。[记录](platform-url-validation.json) |
| 一张卡片投递与激活 | 受信 ApprovalCardDelivery 经当前 Policy 和两份目的地白名单，POST 无按钮共享卡片→持久绑定→PATCH 激活批准；两次均 HTTP 200 / code 0，消息回执只留摘要，不重投。[记录](real-delivery.json) |
| 进程重启 | 发卡后先重启接收进程保留绑定；诊断及格式修复也重启了服务。最终真实请求由加载修复的新进程受理，继续使用同一持久卡片绑定。[重启记录](receiver-restart-before-click.json) |
| 真实批准回调 | 原始签名验证、AES 解密、token、应用、用户和 chat/message 绑定均匹配；返回 200 与无按钮 raw 终态卡片。用户确认显示正常。[记录](real-callback.json) |
| 原子与单次副作用 | 最终 approvals=1（approved）、bindings=1、receipt=1、replay=1、audit=6、outbox=1；投递基线 audit=5/outbox=0，因此决定审计与 outbox 各只增加 1。[真实记录](real-callback.json) |
| 同一真实请求复验 | 已响应平台后，将捕获的同一请求仅在内存中经完整 ASGI 验签/身份/Policy/事务路径复验一次，200 与终态卡片、全部计数不变。没有把这次本地复验描述为飞书平台自身重投。[记录](captured-real-callback-duplicate.json) |

独占发送 reservation 阻止重复执行。只发送一张合成卡片；多次用户点击的失败请求在接收器前返回 401，没有创建 receipt/replay、决定或 outbox。失败及成功计数均保留：[全部请求摘要](callback-attempts.json)。未记录原始回调、action_ref、签名或凭证；数据库仅按既有契约保留受信绑定、规范化指纹与决定。

## 实测发现与修复

真实请求时间戳是 59 字符的 Go `Time.String()` 文本，包含数字时区偏移、纳秒和 `m=...` 单调时钟后缀。旧实现限制 20 字符并只解析 Unix 秒，因此在签名、解密、token、身份和卡片绑定均正确时仍返回 401。共保留 9 次被拒绝的真实卡片回调；它们没有提交审批。[首次失败](first-click-failed.json)、[结构诊断](real-callback-diagnostic.json)、[时间诊断](real-timestamp-diagnostic.json)、[脱敏格式](real-timestamp-format.json)。诊断中的旧整数解析探针失败不表示真实时钟有偏差。

先用完全合成的同格式签名/AES 用例复现失败，再添加有边界的解析：接受 Unix 秒或明确的 Go 格式（字段最多 96 字符）；数字 UTC 偏移决定墙上时间，纳秒归一化到秒，单调后缀不用于跨进程时效判断；完整原文仍参与签名，Go 文本也原样参与 replay 键。原有 1–300 秒配置、审批有效期、身份、职责分离和持久去重均保留。未知格式、非法日期/偏移、过期/未来与签名篡改仍拒绝，没有增加跳过校验的配置。格式依据：[Go Time.String 文档](https://pkg.go.dev/time#Time.String)。

| 本地检查 | 结果 |
| --- | --- |
| 修复前回归 | 1 failed / 15 deselected / 1 warning，缺陷成功复现。[日志](timestamp-regression-before.log) |
| 初步契约与投递定向 | 30 passed / 1 warning，9.69 秒。[日志](timestamp-targeted.log) |
| 补充安全契约 | 24 passed / 1 warning，2.46 秒；本次新增 9 项测试，含纳秒/时区/单调后缀、完整原文签名篡改、过期/未来、非法日期/偏移/精度/格式以及 5 秒较短配置。[日志](timestamp-contract-final.log) |
| 格式/Lint/类型 | 361 文件格式、Ruff、180 源文件 mypy 通过。[日志](lint-final.log) |
| 全量本地回归 | 1117 passed / 9 deselected / 5 既有 warnings，330.89 秒。[日志](community-final.log) |
| 发行包与 smoke | 离线 wheel/sdist 构建、8 个相关模块包内容一致性、包内私有配置值扫描及 CLI 0.1.0 smoke 通过。[构建](build-final.log)、[包内容](package-content.json)、[smoke](smoke-final.log) |

本地命令为锁定环境下的 `make test`、`make lint`、`uv build --out-dir .artifacts/real-approval-20261018` 与 `make smoke`，设置 `UV_OFFLINE=1 UV_NO_SYNC=1`，复用项目既有 uv 缓存；版本与依赖见源码指纹。真实通道脚本显式使用 `ORIA_RUN_ENTERPRISE=1 ORIA_ENTERPRISE_TARGETS=feishu` 及局部已知目标校验，不修改正式 pytest Enterprise target 注册表。每次只读发现最多四次 API 调用，发送仅一次 POST 与同消息 PATCH，无自动重投。

各轮数量不累加；完整测试覆盖上述相同用例。定向 warning 为既有 Starlette/httpx 弃用提示。没有新增依赖、环境变量或迁移，没有修改系统时钟。

## 环境、入口和失败历史

- 初次真实预检鉴权成功但机器人未激活，保留 [两次 API 结果](preflight-010150.json)；发布及入群由用户完成，旧共享 ClawBot/Hermes 的长连接与配置保持现状。
- Chrome 自动控制多次超时，在用户恢复连接后仍无法操作，改为用户手动完成后台步骤，没有改用其他控制方式绕过浏览器。
- 官方固定版本 Cloudflare 工具首次直连 ConnectTimeout，改用现有系统代理后下载/摘要校验通过；HTTPS 证书验证保留，无系统安装。[失败](cloudflared-source-connect-timeout.json)、[通过](cloudflared-source.json)。
- 恢复接收端的九项本地检查通过：六项非回调路径/方法 404，合成签名/AES challenge 200，未映射身份 401，过大请求 413；审批/receipt/replay 为 0，独立本地检查进程停止。[本地记录](callback-gateway-restored.json)。
- 首轮临时入口于北京时间 09:39:22 按时关闭，共 1740.899 秒。旧地址随后返回 HTTP 530 / text/html，导致用户截图中的“不是合法 JSON”；没有对应请求到达本地。[关闭证据](temporary-tunnel.json)、[不含凭证的诊断](expired-url-diagnostic.json)。
- 用户另行明确批准重新开启最多 30 分钟；新地址公网检查通过并由飞书保存成功。真实验证结束后主动关闭，共 1035.422 秒，进程与监听已核实停止，临时目录映射及单个服务端凭证副本已移除。用户原始私有凭证文件保留 0600。[续开检查](callback-gateway-public-renewed.json)、[关闭/清理](renewed-temporary-tunnel.json)。后台和用户私有资料保留的最后临时地址现已失效，下次须新建受限入口并重新校验。
- 私有包装器的内存复验先用合成 ASGI/SQLite/Mock Feishu 验证：1 passed / 1 warning / 3.77 秒；首次临时测试文件导入路径错误，修正 PYTHONPATH 后通过。[失败](harness-test-import-failed.log)、[通过](harness-test-final.log)、[用例](harness-test.py.txt)。该用例验证诊断加入前的包装器；诊断仅添加结构/布尔元数据，最终真实回调及复验也已通过。

失败历史全部保留，没有无改动重复运行挑选最好结果。日志只脱敏用户目录和行尾空白；公开证据无真实应用/用户/会话/消息 ID。状态见 [checks.json](checks.json)，修复前后源码分别见 [原指纹](source-snapshot-before-timestamp-fix.json) 与 [最终指纹](source-snapshot.json)。包装器脱敏源码：[原入口](harness-gateway-before-diagnostics.py.txt)、[诊断后入口](harness-gateway.py.txt)、[结构诊断](harness-diagnostics.py.txt)、[时限守卫](harness-watchdog.py.txt)、[管理进程](harness-manager.py.txt)。

## 阶段边界

本次真实审批切片通过，T02 尚未整体收口。带理由拒绝、飞书详情/翻页、默认会话入口自动投递与完整消息→Job→Graph 仍待后续；跨进程主动 PATCH 恢复与可靠投递账本仍属 T05。不是双 worker/lease/fencing、PostgreSQL API、生产企业部署或 Performance 验收。V0.5-T07 Live 29/80 暂停状态不变。未提交、推送或触发远端 CI。
