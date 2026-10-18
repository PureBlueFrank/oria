# 飞书群内核心流程演示与 GIF 回放

这条入口用真实飞书消息控制现有本地 Workflow 与归因 Agent，用于开发者导览。**模型、业务数据、业务角色和企业 Adapter 都是 Mock/Fixture；不会真实发券、投放或通知商家。** 它与默认 `api serve` 分离，不代表 V0.6 的生产会话、Durable Job、多 worker 或可靠投递已经完成。

首版使用冻结的华东餐饮、混合报名、10 家候选场景。输入是有意收敛的对话指令，不把 Mock 描述为开放式自然语言理解。确认参数后才创建活动；“演示批准”和“演示确认”由现有本地执行器使用各自的合成角色执行，保留 Policy 与职责分离。真实平台用户仅控制演示，不能指定 tenant、role、审批 ID、checkpoint 或工具参数。此处文字演示审批与另行通过的[真实卡片按钮回调验证](../../reports/verification/v0.6/20261018-t02-real-approval/summary.md)分别留证。

## 准备

使用独立 OriaTest，保留共享 ClawBot/Hermes 的配置。测试群现名为“招商oria平台测试群”，底层 chat ID 通过只读接口重新核验，不能只靠群名信任入站请求。

OriaTest 仅需五项权限：`im:chat:read`、`im:chat.members:read`、`im:message:send_as_bot`、`im:message:update`、`im:message.group_at_msg:readonly`。新增最后一项权限、订阅 `im.message.receive_v1` 和发布配置版本，均需应用所有者批准。不申请读取群内全部消息或群历史；权限/事件说明见[官方接收消息文档](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)。

Python 3.11 / uv 0.12.6，依赖照常 `make sync`。凭证 JSON 存于仓库外、权限 0600，包含 `app_id`、`app_secret`、`encrypt_key`、`verification_token`，不放在命令行或 README 中。

以下目录为本次专用新目录；重复演示请换目录。参数名中的凭证路径和操作者名称由运行者提供：

```bash
DEMO_ROOT="/private/tmp/oria-feishu-demo-001"
mkdir -m 700 "$DEMO_ROOT"

ORIA_RUN_ENTERPRISE=1 ORIA_ENTERPRISE_TARGETS=feishu \
uv run python scripts/prepare_feishu_demo.py \
  --credentials /path/to/private/oria-test.json \
  --output "$DEMO_ROOT/private-scope.json" \
  --chat-name "招商oria平台测试群" \
  --operator-name "测试操作者"

ORIA_RUN_ENTERPRISE=1 ORIA_ENTERPRISE_TARGETS=feishu \
uv run oria feishu-demo \
  --config "$DEMO_ROOT/private-scope.json" \
  --data-dir "$DEMO_ROOT/run" \
  --port 8018 --seconds 1800
```

准备脚本最多四次只读接口请求，要求应用为激活的 OriaTest、完整第一页中群和操作者各唯一匹配，否则关闭。私有范围配置通过排他创建保存为 0600；不覆盖原始凭证文件，旧配置中的群名也不会被偷偷改写。

服务只监听 `127.0.0.1`，仅提供 `POST /v1/ingress/feishu/events`。在平台“事件配置”中设置可达的受限 HTTPS 地址并添加接收消息事件；临时公网入口需另行批准，最多 30 分钟，仅转发该方法/路径，结束关闭。原先卡片回调的临时地址已失效；本次只准备消息事件入口，不借机改动其他应用。

每次请求复用现有签名、AES、token、应用与身份校验，只接受已配置操作者在已配置群中以 @ 开头的文字事件。原始正文/回调不进入演示指令账本；仅保留白名单指令、合成槽位、摘要和安全 ViewModel。每次发送前重读私有配置，删除或变更配置即撤销当前运行范围。

## 群内演示脚本

本次采用用户提供的真实飞书群聊截图生成 GIF，并与已核验的实际回复记录交叉核对，无需系统录屏。卡片始终保留“真实飞书通道 · 合成数据 · Mock 模型/业务系统 · 演示角色”。下表也列出入口支持的额外查询；本次实际执行的 20 条指令见[脱敏回复记录](../../reports/verification/v0.6/20261018-feishu-conversation-demo/real-conversation-views.json)，不能把未发送的“恢复演示”称为本次真实群聊已验证。

每句都先 **@OriaTest**，等待上一条卡片到达，再发送下一句。审批与事件只能按当前阶段继续，不支持一次消息自动批准全部步骤。

| 章节 | 群内输入 | 展示的能力 |
| --- | --- | --- |
| 开场 | 帮助 | 合成边界、支持的核心能力和指令 |
| 对话澄清 | 发起招商活动 → 餐饮 → 华东 → 混合报名 → 10家 | 逐槽追问；尚未创建活动 |
| 参数确认 | 确认发起演示 | 正式运行本地 Graph，进入招商审批 |
| 规则与资格 | 规则 → 第2页 → 商家 | 六类规则、版本引用、硬资格过滤与 Mock 软排序；商家可继续第2/3页 |
| 第一次审批 | 演示批准 | 合成审批人通过 Policy，活动和券物化，进入报名等待 |
| 报名汇聚 | 模拟商家报名 → 模拟报名结束 | 受信 Mock inbox、报名/自动圈品汇聚、等待恢复 |
| 动态确认链 | 演示确认，逐次发送直到离开业务确认阶段 | 当前 Fixture 是商家→销售→销售经理，共三级；每次只决定当前一级 |
| 异步选品 | 模拟选品完成 | 逐商品决定和完成事件，恢复到 C 端审批 |
| 第二次审批 | 演示批准 | 模拟 C 端投放 published、模拟商家通知 sent、10 步闭环 |
| 状态与恢复 | 状态 / 恢复演示 | 从已有持久检查点读取状态，不新建活动；如演示进程重启，须维护者先完成本地重启再查询 |
| 归因片段 | 归因示例 | 有界只读工具调查、可回查证据、Mock 回放标记 |
| 边界片段 | 证据冲突示例 / 证据不足示例 | 冲突与弃答，证据不足不编造结论 |

## GIF 与真实截图导览

[查看 GIF](../demo/feishu/core-flow.gif)（1280 × 1040，3 分 30 秒，约 5.0 MiB），或克隆仓库后用浏览器打开 `docs/demo/feishu/index.html`。交互页默认暂停，支持逐幕浏览、章节选择、播放/暂停和点击截图放大；自动播放到末尾停止。GIF 本身循环播放，没有暂停控件，细读请用交互页或[逐幕文字与来源](../demo/feishu/storyboard.md)。

用户提供的 17 张真实飞书客户端截图全部用于新版，按实际消息顺序裁成 24 个画面，配合开场、十步流程概览和三页平台能力/范围说明，共 29 幕。保留原始指令、卡片文字、数值和 Mock 标识，不重绘机器人消息；仅裁掉个人头像、昵称、无关留白，并遮蔽第 17 张的悬浮操作栏及相邻已读图标。长卡片拆分展示，页脚注明原文件序号及上/下段，正文未合成或补写。

截图与 `real-conversation-views.json` 中回复 #2–20 对应；#1“帮助”没有原生截图，开场以项目文档概览和实际终态截图说明。全流程到达 10/10、两次审批的职责分离另与持久记录核对。商家查询、真实服务重启、多智能体、PostgreSQL 和真实按钮回调未出现在这些群聊截图中；平台工程能力页引用 `ARCHITECTURE.md` / `ROADMAP.md` 并明确标成文档说明，不能代替对应验证。

原始截图留在用户本机，不复制含头像、昵称的整张图片到仓库。公开目录只保留裁后素材、源文件名/尺寸/哈希、裁切及遮蔽坐标和逐幕来源。归因采用 Mock 开发集，不证明真实模型自主调查能力；规则中的历史日期为冻结 Fixture 数据。

复现素材处理（FFmpeg 须已安装；不增加项目依赖）：

```bash
uv run python scripts/build_feishu_screenshot_assets.py \
  --source-dir /path/to/private/screenshots
```

`native-story.json` 管理裁切、字幕、时长和响应序号；脚本生成 `screenshots/` 和 `native-data.js`。浏览器打开 `index.html?capture=1&scene=0` 至 `scene=28`，逐幕截取 1280 × 1040 完整画面；通过 FFmpeg 256 色 `palettegen` / `paletteuse` 编码，按各幕停留时间保留 29 帧，末帧 14 秒。编码示例、逐幕检查和指纹见[素材验证记录](../../reports/verification/v0.6/20261018-feishu-native-gif/summary.md)。浏览器导出缓存放在忽略的 `.artifacts/`，最终 GIF 与静态封面直接供 README 引用，无需外部媒体托管。

早期因无法读取本机飞书、官方 Web 消息页空白，曾采用安全 ViewModel 重排为 65 秒回放；该历史版本保留为 `record-replay.html` / `record-replay.gif`，仍明确标注“格式化回放”。`scripts/generate_feishu_replay.py` 只维护历史版的 `replay-data.js`，不再用于生成当前 README GIF。旧编码检查见[原始验证记录](../../reports/verification/v0.6/20261018-feishu-conversation-demo/media-checks.json)。

## 重复、中断与验收

单进程文件锁限制同一目录只有一个演示服务；最多 80 条独立源消息，一条最多发送一张卡片，平台重复源消息只返回既有受理，不重复推进或刷屏。签名 nonce 与源内容冲突拒绝。

指令先持久排队，再由单个演示执行循环调用既有核心服务。排队未执行的指令可在相同范围重启后继续；执行中或发送中的进程退出标记为待核对，发送超时同样暂停后续写动作，不自动重试。只读状态仍可查看；维护者必须对账后选择新演示目录。这不是生产 Job lease/fencing、自动灾难恢复或跨系统事务。

至少留存：本地签名/加密 HTTP 测试、群与操作者只读核验、真实 @ 消息请求/发送次数及无私有值的摘要、人工确认画面、临时入口关闭证据、最终 GIF 与来源检查。原始回调、签名、凭证、群成员标识与无关聊天不得进入公开报告或媒体素材。
