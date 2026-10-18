# V0.6-T02 真实飞书出站验证

日期：2026-10-18，Asia/Shanghai；基于 `c57d59e08c7a09969214a130aa67c0e191bab9ea` 的未提交工作区，保留前期 V0.6 改动。**真实文本/卡片出站及人工显示确认通过；T02 仍进行中。** 本卡属于真实外部通道的有限验证，不是完整 Enterprise 业务或双场景 E2E 验收。

## 结果与范围

使用用户已有 **ClawBot** 应用和明确确认的 **oriaTest** 测试群。只读预检通过，随后重新核验凭证、机器人启用状态及群名称唯一匹配，再由现有 `FeishuChannel` 实际调用飞书发送接口。文本标明“合成测试数据”；卡片标题为“Oria 真实飞书通道测试”，无操作按钮。两条内容和验证编号见 [投递预览](planned-delivery.json)。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 只读预检 | 3 次请求；鉴权、机器人身份、群匹配均成功；没有发送消息 | [preflight-first.json](preflight-first.json) |
| 实际出站 | 5 次请求；含再次鉴权、身份及群校验，文本和卡片发送各一次；全部 HTTP 200 / API code 0，两份非空回执 | [outbound-smoke.json](outbound-smoke.json) |
| 人工显示确认 | 用户回复“两条都可见，卡片显示正常” | [human-review.json](human-review.json) |
| 本地受影响契约 | 21 passed / 1 既有 warning，3.92 秒 | [local-targeted.log](local-targeted.log) |
| 格式 / Lint / 类型 | 359 文件格式、Ruff、179 源文件 mypy 均通过 | [lint.log](lint.log) |

验证编号：`feishu-20261018-93b13b259b62`。全程共 8 次真实 API 请求，其中 2 次发送消息；未执行审批、Job、业务操作或模型调用。实际发送程序上限为 8 次请求、2 条消息，无自动重试。两条发送结果均明确成功，不存在待判定投递。未修改既有飞书应用的权限、事件订阅或回调配置。

## 授权、失败与修正

1. 自动审批审核最初因缺少对具体外部写入的明确批准而拒绝执行。该次命令未执行，没有网络请求或投递预约记录。未绕过拒绝；用户审阅两条内容后明确回复“允许发送这两条测试消息”。
2. 获得批准后的首次执行在本地配置校验停止：文件中的群名标签为 ClawBot，与用户已确认的 oriaTest 不一致。记录为 `blocked`，0 次 API 请求、0 次发送；见 [首次失败记录](outbound-smoke-pre-send-blocked.json)。该轮程序误以退出码 0 返回，不能据此认定通过。
3. 仅修正验证程序：目标固定采用用户确认的 oriaTest，配置文件仅提供应用凭证，不再以可编辑群名标签决定收件人；仍须从飞书列表唯一匹配目标群。未再次修改用户凭证文件。补齐非通过状态退出码 1；保留失败记录，确认尚无投递记录后执行。
4. 实际投递前以独占创建方式写入 [预约记录](send-reservation.json)，避免意外重新运行。最终两条发送成功，程序退出码 0，用户另行确认显示正常。之后未重发。

## 契约、环境与证据

调用复用实际 `LocalPolicyEngine` 的 `notification:send` 授权、精确 tenant/chat/subject 收件人白名单、`IMViewModel`/`render_card` 和 `FeishuChannel`。actor 与 executor 为不同的受信合成测试 Principal；这不证明真实飞书用户到 Principal 的映射。token 通过按测试 tenant 限定的内存函数提供；密钥、token、真实 chat/message ID 与原始 API 响应未写入证据。仅保存回执及群 ID 的 SHA-256。

Python 3.11、项目现有锁定环境，无依赖安装或锁文件改动。各包版本、`uv.lock`、适配器、Policy、展示模型和测试指纹见 [source-snapshot.json](source-snapshot.json)。两份执行程序按用户目录脱敏留档：[只读预检程序](probe.py.txt)、[投递程序](send_smoke.py.txt)；它们是执行证据，不是可直接复跑的产品入口。投递预约文件应保留，不得为重新发送而删除。

本轮以 `ORIA_RUN_ENTERPRISE=1` 和单独的 `ORIA_ENTERPRISE_TARGETS=feishu` 显式选择受限验证程序。执行时临时注册的 feishu target 已在完成后撤回，避免尚无对应 pytest Enterprise fixture 时产生全部跳过的假通过；产品 target 注册表与原版本一致。本卡不宣称 `pytest -m enterprise` 已支持飞书，也未改变现有门禁或默认 CI。

本地验证命令（关闭依赖下载/同步）：

```bash
uv run pytest -q tests/unit/test_target_selection.py tests/contract/test_v06_feishu.py
make lint
```

warning 为既有 Starlette/httpx 弃用提示。本次没有新增业务源码行为；不重复运行全量回归或构建。10 月 16 日全量 1094 passed 与构建/CLI smoke、10 月 17 日真实 PostgreSQL 8 passed 的证据继续分别引用 [受理启动装配卡](../20261016-t02-receiver-lifecycle/summary.md) 和 [PostgreSQL 卡](../20261017-postgres-local/summary.md)，不计为本次重新验证。日志仅脱敏用户目录和清理行尾空白，失败内容与计数保留。

## 未完成与后续

未验证真实入站事件、真实用户身份映射、审批按钮回调、正式卡片投递/更新装配、带理由拒绝、飞书详情/翻页、Job/wait/Graph 恢复、多 worker、其他企业 Adapter 或 Performance。当前配置只有应用鉴权信息；真实审批回调还需在既有应用配置及接收链路明确后单独准备和验证。

下一优先级仍为 T02 正式卡片投递/更新、带理由拒绝、详情/翻页及真实回调；T02 未关闭，T03 依赖未满足。V0.5-T07 Live 29/80 暂停状态未改变。未提交、推送或触发 GitHub Actions，不声明远端 CI 通过。
