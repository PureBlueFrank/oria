# V0.6-T02 审批卡片投递与结果更新

日期：2026-10-18，Asia/Shanghai。基于 `c57d59e08c7a09969214a130aa67c0e191bab9ea` 的未提交工作区，保留前期 V0.6 改动。**受信内部投递/激活与提交后终态响应的本地增量通过；真实审批卡片投递、PATCH 与回调未运行，T02 仍进行中。**

## 交付与边界

新增 `ApprovalCardDelivery` 内部服务，复用 RuntimeServices、原有 ApprovalCardBindings/Policy/FeishuChannel。sender 和 approver 由当前服务端目录确认，发送者检查原通知权限及收件人白名单，审批人检查独立的目的地白名单、审批权限、有效期和职责分离，禁止跨租户或将发起人自动作为审批人。获取 token 后、实际写入前重查两人的映射，覆盖异步期间撤权。

投递固定为：授权公开摘要 → 发送无按钮共享卡片 → 获得非空消息回执 → 持久绑定完整审批版本与真实 chat/message → PATCH 同一消息激活批准按钮。`issue_view()` 以 expected revision 绑定其摘要版本；原始 action_ref 不落库。默认服务启动不发送卡片，没有新增客户端投递路由、自动发现审批人、角色放宽或个人凭证加载。

`FeishuChannel.update_view()` 只更新当前实例成功发送且确认回执的同 tenant/chat/主体共享卡片。普通卡片、未确认消息或其他主体/会话不能借用 PATCH；共享配置在发送与更新时都启用。目标守卫最多 1024 条，淘汰或重启后拒绝主动 PATCH，不将该内存守卫当成 durable ledger。发送和更新超时均如实返回不确定结果，不自动发送或重投；部分成功保留消息回执供对账。

启用的审批 receiver 在既有唯一事务提交后重新授权并读取当前终态，以新版 `card.action.trigger` 的 raw 卡片响应移除按钮。已提交重复及独立 API 进程重启后的重投返回当前结果，不新增成功审计、决定或 outbox。投影失败时可重试已提交结果；503 不表示决定未提交。相应错误提示已改为“暂时无法确认处理结果，请稍后重试或查询当前状态”。

## 验证结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 投递/签名受理/绑定定向 | 56 passed / 1 warning，27.57 秒 | [targeted-final.log](targeted-final.log) |
| 全量本地 F/C/SEC/Recovery | 1108 passed / 9 deselected / 5 warnings，344.75 秒 | [community-final.log](community-final.log) |
| 最终 503 提示补验 | 14 passed / 1 warning，11.18 秒 | [projection-message-final.log](projection-message-final.log) |
| 格式 / Lint / 类型 | 361 文件格式、Ruff、180 源文件 mypy 通过 | [lint-final.log](lint-final.log) |
| wheel / sdist | 离线构建通过；只使用项目既有缓存 | [build-final.log](build-final.log) |
| 包内容 | 修改源码与 wheel 一致；发行物没有包含 .artifacts 缓存 | [package-content.log](package-content.log) |
| CLI smoke | 通过，返回版本 0.1.0 | [smoke-final.log](smoke-final.log) |

新增 14 项测试。各轮不相加；全量包含相同用例。定向唯一 warning 为既有 Starlette/httpx 弃用提示；全量另有四条既有迁移外键反射提示。全量结束后仅完善上述 503 提示文字并格式化，再以 14 项受影响用例、静态检查和重新构建验证；没有在全量后改变业务行为。

新增用例使用真实 Runtime/Policy/SQLite、合成身份与审批、Mock Feishu HTTP transport，以及真实签名的 ASGI 回调。验证了先绑定再激活、真实消息 ID 限制、无按钮占位、审批终态投影、错误消息/目的地拒绝、跨租户/自批、发送前与激活前撤权、POST/PATCH 超时和平台拒绝不重发、结果投影失败后的单次决定恢复。既有独立进程测试新增终态响应断言，并在本次全量中重新通过：两个先后运行的真实 API 进程、回环 HTTP、重启后重复回调、当前撤权拒绝及不增加决定审计/outbox。此处不是双 worker/lease/fencing 验证。

## 执行与失败历史

Python 3.11、uv 0.12.6，依赖由现有锁定环境提供，无新依赖、环境变量或迁移。使用 `UV_OFFLINE=1 UV_NO_SYNC=1`；测试/静态检查缓存为临时目录，构建使用仓库已有 `.artifacts/uv-cache`。没有运行真实模型、PostgreSQL 或企业业务 Adapter，也没有发送新的真实飞书消息。

```bash
uv run pytest -q tests/integration/test_v06_approval_delivery.py tests/integration/test_v06_transactional_approval_acceptance.py tests/integration/test_v06_approval_cards.py tests/contract/test_v06_feishu.py
make test
uv run pytest -q tests/integration/test_v06_approval_delivery.py
make lint
uv build --out-dir .artifacts/approval-delivery-20261018
make smoke
```

- 首轮：15 passed / 11 errors，async fixture 误用普通 pytest 装饰器；更正为 pytest_asyncio fixture。[日志](targeted-fixture-error.log)
- 第二轮：25 passed / 1 failed，自批 fixture 误以为 Repository.replace 能修改不可变 requester；改为插入明确 requester 的独立审批。[日志](targeted-sod-fixture-failed.log)
- 首次修正通过：26 passed / 1 warning；随后为明确遗漏的撤权时机和提交后投影故障补用例，最终定向 56 passed。[初次通过日志](targeted-initial-pass.log)
- 首次离线构建缺临时缓存中的 hatchling；改用项目已有构建缓存，没有绕过锁文件安装依赖。[失败日志](build-initial-cache-missing.log)
- 新提示文字的格式检查曾失败，定向格式化后完整静态检查通过。[格式失败记录](lint-message-format-failed.log)

本地网关首轮九项 HTTP 检查已通过，但最终计数检查误读临时空数据库路径；修正为 RuntimeDataPaths 后复验通过，保留 [失败记录](callback-gateway-first-failed.json)。

未通过轮次均保留，没有无改动重复运行挑选结果。日志只替换用户目录和移除行尾空白，失败计数保留。源码/锁文件指纹及环境见 [source-snapshot.json](source-snapshot.json)，最终检查见 [checks.json](checks.json)。未提交、推送或触发远端 CI，不声明 GitHub Actions 通过。

## 真实环境准备与未完成项

用户确认 ClawBot 被其他程序共用，并补充可能为 Hermes；浏览器显示该应用使用长连接且已有卡片回调订阅。未修改其配置，也未读取或复用 Hermes 的登录、连接或本地凭证。

经用户明确批准，已创建独立 **OriaTest** 并启用机器人能力，界面已核实有机器人配置及“待上线”状态。用户已填好独立的 0600 私有凭证文件；不覆盖 ClawBot 配置。经另行明确批准，权限已收敛为 `im:chat:read`、`im:chat.members:read`、`im:message:send_as_bot`、`im:message:update`，其余九项默认权限关闭。只读鉴权通过，但机器人尚未发布而处于未激活状态，保留 [首次真实预检](real-preflight.json)。已在最多 30 分钟的用户批准范围内建立仅允许指定 POST 路径的 Cloudflare 临时 HTTPS 隧道，通过 [本地网关检查](callback-gateway-local.json) 和 [公网合成 challenge 检查](callback-gateway-public.json)。飞书后台已接受 HTTP 地址并订阅新版 `card.action.trigger`；新增只保存请求摘要、状态码和响应形态的私有观察器后，[公网补验](callback-gateway-observer-public.json) 再次通过，没有保存原始回调、签名或令牌。地址验证不能替代审批按钮验收。发布确认仍待回复，因此已主动关闭本次临时入口；[关闭证据](temporary-tunnel.json) 确认进程和回环监听均停止，原私有配置中的临时地址现已失效，下一轮须更新并重新校验。版本 0.1.0 资料已准备，可用范围仅所有者，发布、测试审批人映射与一张具体合成卡片的发送正在等待用户明确批准。真实准备状态见 [real-readiness.json](real-readiness.json) 和 [联调指南](../../../../docs/guides/feishu-approval-validation.md)。

真实审批回调尚缺发布/入群、已确认的真实测试审批人映射及具体测试卡片发送授权。未配置/未运行不能记为通过。此前 ClawBot 的两条真实出站及人工显示确认仍独立引用 [出站卡](../20261018-feishu-channel/summary.md)，不当作本增量 PATCH 或审批回调证据。

带理由拒绝、飞书详情/翻页、默认入口自动投递触发、可靠投递账本与跨进程主动 PATCH 恢复、Job/wait/Graph 仍待后续；T02 未收口，T03 依赖未满足，V0.5-T07 Live 29/80 暂停状态不变。
