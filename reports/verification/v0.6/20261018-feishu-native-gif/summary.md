# 真实飞书截图 GIF 素材核验

日期：2026-10-18，Asia/Shanghai。范围：替换 README 演示素材与导览文档，不改变 Oria 产品行为或版本门禁。

**通过。** 用户提供的 17 张真实飞书群聊截图全部使用，裁成 24 个可读画面，组织为 29 幕、210 秒的导览。当前 [GIF](../../../../docs/demo/feishu/core-flow.gif)、[封面](../../../../docs/demo/feishu/poster.png)、[交互页](../../../../docs/demo/feishu/index.html)与 [README](../../../../README.md)已更新。旧重排版保留为 `record-replay.*`，原始截图留在用户本机。

## 来源与准确性

- 逐一查看原图和裁后画面，保留原生消息文字、数值、@ 指令、Mock 提示和顺序。仅裁去头像、个人昵称、留白、无关尾部；第 17 张的悬浮操作栏与相邻已读图标做遮蔽，没有重绘或补写消息。
- 原始 1.png–17.png 与[安全响应记录](../20261018-feishu-conversation-demo/real-conversation-views.json) #2–20 对应。#1“帮助”没有原生截图，开场使用终态截图与单独标明的能力概览，不伪造帮助卡片。来源文件名、尺寸、SHA-256、裁切/遮蔽坐标及响应映射见 [screenshot-sources.json](screenshot-sources.json)。
- [持久终态](../20261018-feishu-conversation-demo/real-workflow-complete.json)支持招商 10/10、published/sent 以及两项审批 requester/decider 分离。此证据复用原验证，没有重新发送消息、创建活动或调用模型。
- 截图中的模型、业务系统、数据和业务角色为 Mock/Fixture；文字审批由同一操作者驱动合成角色。原生飞书画面不代表真实企业业务投放、真实模型质量或真实多人审批。

## 完整性覆盖

| 范围 | 画面与依据 |
| --- | --- |
| 需求澄清与参数确认 | 幕 03–07；类目、区域、报名模式、10 家候选，确认后才创建活动 |
| 01 规则与草案、02 招商审批 | 幕 08 的规则/候选冻结与审批等待；具体检索/硬资格内部逻辑仅在工程说明介绍，不冒充截图中的可见 Trace |
| 03 招商发布、04 报名圈品 | 幕 09–10；审批通过后的已完成阶段与报名等待，关窗前不越过等待点 |
| 05 动态确认 | 幕 11–13；商家、销售、销售经理三级 Fixture 确认，各自独立展示 |
| 06 关联券、07 异步选品 | 幕 14–15；已完成阶段、选品待定及完成后的选中/关联计数 |
| 08 C 端审批、09 投放、10 通知 | 幕 15–18；pending_approval → 全部十步完成 → published/sent |
| 经营异常归因 | 幕 19–22；attributed、3 个证据路径、conflicting 与 insufficient；不宣称 Mock 的动态模型能力 |
| 六类规则与翻页 | 幕 23–26；基础信息、招商范围、报名规则、优惠档位、确认规则、商家素材；日期是冻结数据 |
| 平台工程能力与交付范围 | 幕 01–02、27–29；单独标识文档说明，依据 ARCHITECTURE/ROADMAP，涵盖插件、检索、权限、检查点、幂等、审计、记忆、协作与分层评测 |

工程说明没有把默认消息到 Durable Job/Graph、可靠出站、多 worker、企业业务 Adapter、MCP 隔离集成和 Web 工作台描述成已交付。真实模型、服务重启、真实按钮回调与 PostgreSQL 不由这组截图证明；另见 [ROADMAP](../../../../ROADMAP.md)、[真实按钮回调验证](../20261018-t02-real-approval/summary.md)和 [PostgreSQL 验证](../20261017-postgres-local/summary.md)。

完整时间线、每幕字幕和裁后图片入口见[逐幕说明](../../../../docs/demo/feishu/storyboard.md)。

## 可读性、编码与交互检查

- 画布 1280 × 1040，字体/原生卡片按章节保持稳定。长卡片分为相邻幕次，截图没有拉伸；最小显示比例约 0.884（封面），正文图片大多约 0.979。每幕停留 4–14 秒，复杂能力说明停留 12–14 秒。
- GIF：29 编码帧，210.000 秒，5,261,898 字节，256 色，无抖动，循环播放。全部 29 个帧起始时间与故事文件一致，末帧 14 秒；指纹见 [media-checks.json](media-checks.json)。
- 29 幕浏览器检查：图片全部加载，标题、说明、内容卡、来源行和边界行无文字溢出。实际查看封面和最长卡片，解码抽查幕 02/14/25/27/28/29。全部 29 个编码帧与浏览器原帧比较，最低 PSNR 46.51 dB；此指标只验证编码损失，清晰度另经人工查看。[浏览器检查](browser-checks.json)、[逐帧编码比较](encode-frame-quality.log)。
- 交互页已检查上一幕、下一幕、章节选择、截图放大/关闭、自动播放推进、暂停、末页禁用下一幕；控制台无错误。默认暂停，适合细读。GIF 本身不提供暂停，因此 README 同时提供交互页与逐幕文字入口。
- 裁后 PNG 不含文本、EXIF 或其他个人元信息；公开素材不带个人头像/昵称、凭证或原始回调。保留的活动/商家/商品编号来自合成记录。未重新读取私有凭证文件。

## 验证与复现

本次 `make lint` 通过：367 文件格式、Ruff 和 182 源文件 mypy，见 [lint.log](lint.log)。相关产品、测试、锁文件指纹与原验证一致，复用原 1132 项本地回归，不重复运行或计为新增通过。原测试失败历史、版本状态与 Live/Enterprise 边界均保留。

裁切与逐幕配置在 `docs/demo/feishu/native-story.json`；运行 `scripts/build_feishu_screenshot_assets.py --source-dir <原图目录>` 生成裁后 PNG 和 `native-data.js`。浏览器逐幕捕获 `index.html?capture=1&scene=0` 至 `scene=28`，每帧 1280 × 1040。截图目录之外，任何私人文件不用于预览。

将帧路径与故事文件中的秒数写入 FFconcat 列表后，编码命令如下（示例路径按实际缓存替换）：

```bash
ffmpeg -f concat -safe 0 -i frames.ffconcat \
  -vf 'palettegen=max_colors=256:reserve_transparent=0:stats_mode=full' \
  -frames:v 1 palette.png
ffmpeg -f concat -safe 0 -i frames.ffconcat -i palette.png \
  -lavfi '[0:v][1:v]paletteuse=dither=none' \
  -fps_mode vfr -loop 0 -final_delay 1400 core-flow.gif
```

本次直接读取 `file://` 的浏览器预览被协议限制拒绝；改用只绑定 127.0.0.1、仅允许该演示目录静态文件的只读预览，未扩大到整个工作区。沙箱首轮拒绝回环端口绑定，获得工具批准后完成本地预览，未创建公网隧道。构建帧和编码缓存仅放在忽略的 `.artifacts/`。

本次没有对外发送消息、发布、提交或推送，没有触发远端 CI。最终本地引用、差异、指纹及临时预览关闭情况见 [final-checks.json](final-checks.json)；旧媒体的历史哈希与 `record-replay.*` 一致。

提交前另对四份历史日志中的用户目录前缀、两份摘要中的测试操作者显示名做脱敏，保留全部结果与失败历史。前后哈希见 [prepublication-redactions.json](prepublication-redactions.json)；已有来源快照记录的历史摘要哈希不覆写。
