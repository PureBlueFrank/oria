# 飞书群内合成核心流程演示

日期：2026-10-18，Asia/Shanghai。用户将测试群改名为“招商oria平台测试群”，明确批准独立 OriaTest 增加用户 @ 消息权限、接收消息事件和版本发布，并选择真实飞书 + 合成数据 + Mock 模型/业务系统的演示范围。

**本地实现及门禁通过；OriaTest 1.0.1 已发布；真实群聊 20 条指令全部投递成功，招商 10/10 完成，归因三种结果与规则分页均有实际回复。用户取消视频，改为 GIF；注明来源和 Mock 范围的运行记录回放已集成 README。临时公网入口已关闭。**

## 实现与边界

- 显式 `oria feishu-demo` 复用现有 `local_executor` 与 `run_attribution_ask`，不复制领域状态机，不装入默认 `api serve`。场景 A 固定华东餐饮、混合报名、10 家候选；场景 B 使用已审阅开发案例的 Mock 回放。
- 参数补齐后仍需明确确认。文字“演示批准”“演示确认”和模拟事件仅控制合成业务角色，保持原有 Policy、职责分离、检查点与 inbox/业务幂等。它与上午真实卡片按钮审批的证据分开，不把一个人的演示描述成真实多人协作。
- 真实接入按私有配置固定应用、群和操作者，复用既有 token/签名/加密/时间窗校验；仅接受群内 @ 文本。业务数据与运行配置隔离，Mock/Fixture 强制显式选择，不继承环境中的 Live 模型或企业 Adapter。
- 演示指令只存白名单命令、合成槽位与内容摘要；源消息和 nonce 冲突拒绝。独立演示目录有单进程锁，80 条独立指令、160 个 replay 键与最多 30 分钟时限。执行/发送结果不明时暂停后续写动作，无自动重发。
- 不包含生产 Job、lease/fencing、多 worker、可靠出站调度、PostgreSQL API 或真实业务投放；V0.6-T02/T03 门禁不因本次导览而关闭。见 [ADR-038](../../../../docs/adr/ADR-038-guided-feishu-demo.md) 和[操作与素材指南](../../../../docs/guides/feishu-conversation-demo.md)。

## 本地验证

| 检查 | 结果与证据 |
| --- | --- |
| 首轮定向 | 1 failed / 11 passed，2 家候选参数低于现有 Mock 固定查询的 10 家，被核心边界正常拒绝。[原始失败](local-first.log) |
| 固定 Fixture 参数后 | 12 passed；随后补充应用生命周期、撤权与配置准备用例，最终 15 passed / 1 warning。[定向](local-second.log)、[生命周期](local-lifecycle.log)、[准备与排他保存](local-preparation.log) |
| 完整回归初次 | 1131 passed / 1 failed / 9 deselected。唯一失败为沙箱拒绝回环端口 `bind`，与业务断言无关；保留失败。[日志](community-sandbox-restricted.log) |
| 完整回归复验 | 允许测试回环端口后，1132 passed / 9 deselected / 5 既有 warnings，346.91 秒。[日志](community-final.log) |
| 静态门禁 | 产品实现的 365 文件格式、Ruff、182 源文件 mypy 通过；初次行宽和后续格式调整记录保留。[首次](lint-initial.log)、[格式调整](lint-format-adjustment.log)、[最终](lint-final.log) |
| 构建与启动 | 离线 wheel/sdist、相关源码包内容一致性与包内真实私有值扫描通过；CLI smoke 0.1.0 和新命令帮助通过。[构建](build-final.log)、[包内容](package-content.json)、[启动](smoke-final.log)、[帮助](cli-help.log) |
| 本机真实 HTTP | 临时独立进程完成隐藏 API/文档路径拒绝、非法回调 401、实际私有测试配置下的合成 AES challenge 200，并停止；外部消息数 0。[记录](local-http-preflight.json) |

新增 15 项用例覆盖实际本地 Graph 的双审批、双等待、三层 Fixture 确认与最终 published/sent，持久检查点读取，三种归因结论，签名/AES HTTP 受理、源/nonce 冲突、目标/身份/未 @ 拒绝，未知结果暂停，单进程与运行时回收，配置撤销及私有范围文件的排他保存。

使用 Python 3.11 / uv 0.12.6 与现有锁文件；`UV_OFFLINE=1 UV_NO_SYNC=1`，复用项目缓存，运行 `make lint`、`make test`、`uv build --out-dir .artifacts/feishu-conversation-demo-20261018`、`make smoke`。各轮数量不累加，不把 Fixture 或真实飞书通道计为真实模型/企业业务通过。没有新增依赖、迁移或环境变量；真实通道显式使用 `ORIA_RUN_ENTERPRISE=1 ORIA_ENTERPRISE_TARGETS=feishu`。

## 真实通道与失败历史

1. 私有配置准备首轮失败，未输出敏感信息；补充阶段诊断后确认四次 API 均成功，但误用 `Path.open(opener=...)` 导致保存 TypeError。现改用 `os.open(O_EXCL,0600)` 并增加回归；之后四次只读发现通过，恢复工作后再次刷新群及操作者。每轮最多四次请求，无群消息写入。[首次](discovery.log)、[诊断](discovery-second.log)、[修复后](discovery-final.log)、[刷新](discovery-renewed.log)。
2. 用户批准的首轮入口已通过后台地址校验；后续浏览器读取被自动审批服务的额度不足中断，动作未执行，不是安全拒绝。该入口按时自动关闭，未收到演示指令。[关闭记录](initial-window-closed.json)。下午重试浏览器成功，随后用户明确批准新的最多 30 分钟窗口。
3. OriaTest 的单项 `im:message.group_at_msg:readonly` 已开通；仅添加 `im.message.receive_v1`，发布 1.0.1，原有 测试操作者 可用范围与禁止对外共享保留。共享 ClawBot/Hermes 未改动。新事件地址在发布后重新校验保存。
4. 真实群内 @“帮助”经接收器与 FeishuChannel 发送导览卡片，指令 journal 标记 sent，用户确认可见。[首条真实对话](real-conversation-first.json)。此记录不含原始消息、平台身份 ID、签名、token、action_ref 或 callback body。

## 完整真实群聊与媒体交付

- 20 条白名单指令全部为 sent，无失败或结果不明；用户在真实测试群中发送指令并确认首条导览可见。后端安全回复记录覆盖参数澄清与确认、第一次审批、报名与关窗、三级动态确认、选品、第二次审批、归因/冲突/不足、规则及第 2 页。[全部实际回复](real-conversation-views.json)。
- 持久记录显示核心流程 completed、10/10，模拟投放 published、模拟商家通知 sent；两项审批均 approved，requester 与 decider 分离。[终态核验](real-workflow-complete.json)。本次群聊没有发送“恢复演示”或重启进程，不把本地恢复测试记作真实群聊恢复验证。
- 首次终态证据查询误用了不存在的 decided_by 列；改为实际 decider 后通过。只读查询，无业务写入，保留[查询失败元信息](evidence-query-initial.json)。
- 更新窗口在 1357.11 秒主动结束，接收器、隧道与管理进程停止，127.0.0.1:8018 拒绝连接。[关闭证据](renewed-window-closed.json)。再次联调仍需新的明确时限批准。

用户取消视频并要求 GIF。首版制作时，工具不支持本机飞书原生界面捕获；已登录的官方 Web 消息页持续空白，重新加载未解决。因此采用实际投递记录生成独立 Oria 回放页，再截图拼接，持续显示“真实群聊记录回放”“实际回复内容 · 格式化回放”及 Mock 范围。此媒体不作为客户端截图/录屏的证据。

`generate_feishu_replay.py` 经实际 `render_card` 与实际页码构造 10 幕；所有卡片内容来自源回复序号，章节解释来自相同流程与终态核验。交互页支持上/下一幕、播放/暂停与展开完整当页回复，已实际检查。浏览器全页截图裁去右侧空白，FFmpeg 128 色编码，最终 GIF 1120 × 760、65.04 秒、1,032,245 字节，11 编码帧（10 幕，末帧重复用于停留），循环播放。已检查关键幕次与编码结果；没有重建飞书原生界面。

首版历史交付：[GIF](../../../../docs/demo/feishu/record-replay.gif)、[静态封面](../../../../docs/demo/feishu/record-replay-poster.png)、[交互回放](../../../../docs/demo/feishu/record-replay.html)、[README](../../../../README.md)。素材与来源检查见 [media-checks.json](media-checks.json)。最终新增回放脚本后，366 文件格式/Ruff 与 182 源文件 mypy 再次通过，见 [素材阶段静态检查](lint-media-final.log)。新增媒体已再次构建，并核对包内当前源码、GIF 与回放数据以及凭证值，见 [媒体最终构建](build-media-final.log)与[包内容检查](package-media-content.json)。173 个改动与新增文件的七项实际私有值扫描、145 项本地文档引用检查通过；两份自建私有范围文件已逐个删除，原始用户凭证保留，接收端与预览服务均已停止，见 [清理记录](cleanup.json)。

未生成或托管视频，没有提交、推送或触发远端 CI。

状态见 [checks.json](checks.json)，源码指纹见 [source-snapshot.json](source-snapshot.json)。失败历史保留，日志仅清理用户目录和行尾空白，不选择性删除失败轮次。

## 媒体后续替换

同日用户提供 17 张真实飞书截图后，README 的 core-flow.gif、poster.png 与 index.html 已替换为真实截图导览。旧媒体已按上述 record-replay 路径归档，原 media-checks.json 中的路径与哈希是首版生成时的历史快照，其 GIF 哈希仍与归档文件一致；不改写本次运行、测试及失败历史。新版素材检查见[独立验证卡](../20261018-feishu-native-gif/summary.md)。
