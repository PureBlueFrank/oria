# API 与飞书入口基础（V0.6-T02 进行中）

当前交付可运行的 HTTP 外壳、JWT 身份映射、资源授权接缝、飞书签名/加密回调解析和出站适配器、IM v1 展示模型，以及事务化持久去重接收器。审批详情与决定 HTTP 路由、持久审批卡片绑定和原子批准处理器已实现；服务启动可显式装配飞书审批受理，共用审批运行时。新增受信内部卡片投递/激活服务和批准回调的结果卡片响应；默认 CLI/API 不自动发送卡片。2026-10-22 起可另行显式启用 [Job 会话/提交/查询/取消](durable-jobs.md)，当前仅持久化受理；Graph 与真实飞书双场景链路尚未接通。

## 本地启动

```bash
make sync
uv run oria api serve --local --host 127.0.0.1 --port 8000
```

`GET /v1/health` 返回 API 版本和 `job_service=not_configured`；`GET /v1/me` 返回固定本地 actor 与独立的 `oria-api` executor。`--local` 只能绑定 `127.0.0.1` 或 `::1`，请求来源也必须为回环；不得将此模式放到反向代理后对外暴露。未指定 `--local` 时必须提供服务端配置，不自动降级为本地身份。访问日志和代理身份头信任关闭。

## JWT 配置

以下是合成示例，不包含可用凭证。将配置与身份目录保存在受限权限的部署目录；相对 `identity_file` 从配置所在目录解析。用原子替换更新身份文件，服务端每次鉴权重新读取；删除映射即撤权。没有新增环境变量，也不自动读取 `.env`。

```json
{
  "jwt": {
    "issuer": "https://identity.example",
    "audience": "oria",
    "jwks_url": "https://identity.example/.well-known/jwks.json"
  },
  "identity_file": "identities.json"
}
```

```json
{
  "bindings": [{
    "issuer": "https://identity.example",
    "subject": "synthetic-user",
    "actor": {
      "subject_id": "operator-1", "tenant_id": "demo-tenant", "kind": "human",
      "roles": ["operator"], "authn_method": "verified-external"
    },
    "executor": {
      "subject_id": "oria-api", "tenant_id": "demo-tenant", "kind": "service",
      "roles": ["runtime"], "authn_method": "server-config"
    }
  }]
}
```

```bash
uv run oria api serve --config /path/to/private/server.json
```

只接受固定 RS256 与至少 2048 位 RSA 公钥，要求 `iss/aud/sub/exp/iat`，校验签名和时间；`nbf` 存在时也校验。JWKS 仅从服务端配置的 HTTPS 地址获取，不跟随重定向、不读取 token 中的 URL。缓存最多 60 秒，刷新最短间隔 5 秒；密钥轮换需保留足够重叠窗口，未知 key 在刷新冷却期内失败关闭。生产代理负责 HTTPS 终止和请求速率限制。配置的 issuer/JWKS URL 属于部署信任根。

JWT 中即使签有 `tenant/roles` 也不会被用作授权事实；仅 `(issuer, sub)` 选择目录中的当前 Principal。资源层 `authorize_resource` 还会重新解析身份、检查租户、复用现有 PolicyEngine，并对审批执行职责分离。资源必须先按可信 tenant 查询；不存在与拒绝都返回相同错误。审批路由复用既有 ApprovalService 的 tenant 查询与 Policy；Job 路由另加所有者及场景权限校验，提交不等于 Graph 已执行。

错误统一为 `{code, message, request_id, retryable, details}`，不返回异常原文、校验输入、外部响应、令牌或回调正文。`request_id` 由服务端生成；响应禁止缓存。OpenAPI 可在 `/openapi.json` 查看，完整 snapshot/drift 门禁仍属 T06。

## 审批详情与决定（2026-10-06）

受信应用可通过 `create_app(authenticator=authenticator, approvals=ApprovalAPI(runtime, repository, directory))` 注入已启动的 RuntimeServices、既有 ApprovalRepository 和当前身份目录。调用方负责资源生命周期；库接口不会迁移数据库或新建生产审批。`api serve` 现可通过显式 `approval_runtime_config` 装配；不配置时仍对已认证审批请求返回 **503**。

- `GET /v1/approvals/{approval_id}?page=1` 按当前可信 tenant 查询，并重新检查审批角色与身份映射。返回 `detail_ref/status/revision/page/pages/card`；不存在与无权访问均为 404。详情只投影操作范围、时间、审批说明和活动编号，沿用卡片脱敏，不返回 requester、checkpoint、参数摘要或原始状态。当前投影最多五段，所以只有第一页；越界页拒绝。
- `POST /v1/approvals/{approval_id}/decision` 接收 `decision`（`approve` 或 `reject`）、从详情取得的 `expected_revision`，以及可选 `reason`。拒绝必须提供非空白理由；额外的 tenant、role、checkpoint、wait 等参数返回 422。版本已变化或重复决定返回 409，应刷新详情重新核对。
- 决定复用现有职责分离、有效期与策略版本校验；发起人不能自批。通过版本检查也不代表获得执行权限，后续 Graph 恢复仍需原有参数、checkpoint、业务绑定和 Policy 校验。接口只记录决定，不直接恢复 Graph。
- 审批 Repository 的 `replace` 必须携带读取时的 `expected` 快照。数据库更新要求整条快照仍匹配，成功决定和 audit/outbox 同事务；两个不同请求竞争同一审批时，仅一个成功，失败方不覆盖决定、不新增审计。内存 Repository 保持同一契约。没有新增迁移或依赖。
- 此 HTTP 版本校验不是飞书 `action_ref` 绑定。已新增 tenant/审批版本/身份/卡片消息的持久关联与按钮原子批准；wait 关联、默认会话入口的自动投递和后续 Job 恢复仍待实现。受信内部投递见下文，飞书接收器仍须显式启用，默认保持关闭。

## 启用本地审批服务（2026-10-08）

在服务端 JSON 配置中增加 `"approval_runtime_config": "runtime.yaml"`，文件路径相对该 JSON 所在目录解析。运行时 YAML 使用现有配置格式；最小示例：

```yaml
data_dir: /absolute/path/to/initialized-oria-data
```

本轮仅允许 Mock LLM、Fixture Embedding 和 platform/business 双 SQLite。API 只读取显式 YAML，不加载个人默认配置，也不接受 `ORIA_*` 环境覆盖。运行时 YAML 中相对 `data_dir` 以 YAML 所在目录解析；建议使用绝对路径，避免其他 CLI 命令相对工作目录解析时产生差异。

数据库必须事先通过已有 `oria db upgrade --config /path/to/runtime.yaml` 或演示数据初始化流程准备。启动只读核验两条迁移链和完整 schema；缺库、旧版本或损坏时停止，不创建数据库、不自动迁移。通过检查后启动现有运行时，其 saver、向量索引和对象存储会在配置的数据目录初始化运行所需资源。部署操作须自行明确授权；本次测试只使用临时目录。

仍使用 `oria api serve --config /path/to/server.json` 启动。应用 lifespan 管理审批运行时、数据库和 HTTP 客户端；启动失败与正常关闭均回收资源。审批决定持久化在上述 platform 数据库。身份目录的启动快照提供 Policy 信任集合，每次请求仍读取当前目录；撤销映射即时生效，新增用户或角色变更需重启，使 Policy 快照同步。`--local` 不启用审批服务。默认不启用飞书 receiver；显式启用方法见下节。Job 可另行[显式启用持久化受理](durable-jobs.md)，实际 Graph 执行、真实模型和 PostgreSQL API 接入仍未装配。

## 启用飞书审批受理（2026-10-16）

在已有服务端 JSON 中同时配置 `feishu`、`approval_runtime_config` 和布尔值 `"feishu_approval_receiver": true`。示例中的密钥为合成占位值：

```json
{
  "jwt": {
    "issuer": "https://identity.example",
    "audience": "oria",
    "jwks_url": "https://identity.example/.well-known/jwks.json"
  },
  "identity_file": "identities.json",
  "approval_runtime_config": "runtime.yaml",
  "feishu": {
    "app_id": "example-app",
    "encrypt_key": "replace-with-deployment-secret",
    "verification_token": "replace-with-deployment-secret"
  },
  "feishu_approval_receiver": true
}
```

开关默认 `false`，不接受字符串 `"true"` 等隐式转换。启用但缺少飞书配置或审批运行时配置时，服务拒绝启动，不创建数据目录。数据库仍须预先迁移，运行时仍限定 Mock/Fixture 与双 SQLite；启用回调不会激活真实模型、出站投递或自动恢复 Graph。

HTTP 审批与飞书回调共享同一个应用生命周期中的 RuntimeServices、Policy 和 Repository。接收器仅处理已签发并持久绑定的审批卡片 action；引用仍由受信内部业务服务通过 `ApprovalCardBindings.issue()` 签发，启动不会创建审批、签发引用或发送卡片。重复点击、跨进程重启后的重投均复用受理记录；新签名投递可增加 replay 记录，但不再决定审批或新增决定审计/outbox。所有回调继续逐次检查当前身份、职责分离与绑定。

2026-10-18 起，已启用的审批 receiver 在决定事务提交后重新读取并授权结果，返回 `{"card": {"type": "raw", "data": ...}}`，将共享卡片更新为无按钮的当前终态。重复回调和跨进程重启后重投返回同一审批的当前结果，不新增成功授权审计、决定或 outbox。若提交后的结果读取失败，响应为 503，决定可能已经记录；调用方应重试或查询当前状态，不能据此认定决定未提交。重试仍执行完整去重及重新授权。

缺少 Job 消息处理器的文本消息仍返回 **503**，不写受理账本；未启用开关时，业务回调同样保持 **503**，URL challenge 仍可验证。无效卡片引用与无权限统一拒绝。启动装配失败和正常关闭会回收数据库、运行时及 HTTP 客户端；注入服务与应用自管服务不能混合，避免使用不同生命周期的审批与受理对象。本地独立进程和真实回环 HTTP 的验证见 [装配验证卡](../../reports/verification/v0.6/20261016-t02-receiver-lifecycle/summary.md)。

## 飞书边界

服务端配置可加 `feishu` 对象，包含 `app_id`、`encrypt_key`、`verification_token`，必须由部署方提供并保密。默认时间窗 300 秒；不配置时飞书入口返回 503。应用开启 `im.message.receive_v1` 和新版 `card.action.trigger`，使用 `/v1/ingress/feishu/events`。旧版回调与非文本消息暂不支持。

签名时间戳接受 Unix 秒及实测出现的 Go 时间文本（数字时区偏移、可选纳秒与 `m=...` 后缀），最大字段长度 96。墙上时间用于原有时效和过期判断，完整原文用于验签；Go 文本原样绑定 replay 键。单调时钟后缀不改变墙上时间，未知格式或非法日期/时区拒绝；`max_age_seconds` 仍须为 1–300，没有跳过或扩大时效检查的开关。

- URL challenge 验证 Verification Token；支持 AES-256-CBC 加密载荷。携带签名头的 challenge 也必须通过验签。
- 普通回调要求完整签名、时间窗、应用 ID、Token 和平台租户/用户一致；使用 SHA-256(timestamp + nonce + encrypt_key + 原始 body) 验签后解密。
- 身份目录使用 issuer `feishu:<app_id>:<tenant_key>` 与 subject `open_id`，不从文本或卡片 value 获取角色/租户。
- 消息按平台 source message 派生稳定去重键；卡片 value 只允许版本、不透明 `action_ref` 和可选的严格整数 `page`（1–20）。页码仅用于展示，实际页数在授权后验证。该引用不等于审批授权，处理器必须在服务端绑定资源/版本并重新鉴权，Job/wait 仍待后续。
- `TransactionalFeishuReceiver` 已实现持久 source/action/replay 去重：`platform_0010` 新增只保存键、摘要与回执引用的 `ingress_receipts/ingress_replays`。业务处理器的授权和写入与账本共用同一事务，失败或提交前崩溃一起回滚；相同消息重投不再次写入，不同内容复用同一键返回 409。重复请求也重新检查当前身份及业务授权。
- 接收器只接受受信服务实现的 `TransactionalAcceptor`，其 `accept(session, event, receipt_ref, duplicate=...)` 检查当前 Policy 与服务端资源绑定，并只能使用提供的事务写入业务请求，不能另开连接、主动提交或执行外部副作用。审批卡片复用真实 ApprovalService，可通过上述开关装配受理接缝；本地 SQLite 回调用于验证。Job/wait 消息处理器尚未完成，已验签文本消息仍返回 **503**。本地审批批准不代表 Job/Graph 恢复或真实飞书链路完成。
- 新表带 tenant 复合键与 PostgreSQL FORCE RLS；本轮仅实测 SQLite。迁移通过已有 `oria db upgrade` 路径执行，测试只操作临时数据库；没有改动用户现有数据。nonce 的到期字段已保存，但本轮不自动清理账本，受理回执必须保留至业务幂等保留期结束后再制定清理策略。
- `FeishuChannel` 提供授权后的文本与卡片发送；只向服务端配置的 tenant/chat/subject 白名单发送，调用现有 `notification:send` Policy。共享卡片可通过 `update_view()` 更新，须为当前实例成功发送且确认回执的同 tenant/chat/发送主体卡片；普通卡片、任意消息 ID 或其他主体均拒绝。此内存目标守卫最多保留 1024 条，重启/淘汰后不授权主动 PATCH，不替代持久投递账本。部署方注入按 tenant 取 token 的函数，当前 CLI 不装配出站通道。超时返回 `delivery_unknown`，不自动重发；可靠 delivery ledger 属于 T05。文件上传不支持，也不会读取给定路径。
- `IMViewModel` v1 覆盖回执、确认、进度、结果、待审批/待事件与失败，限制字段/长度，每页最多五段，展示详情编号与下一步建议。卡片经过已有输出脱敏，JSON 2.0 的文本另作 Markdown/HTML 转义。业务服务只能投影已授权的公开摘要，不能传入隐式思维链或原始 Tool/State。审批 HTTP 和飞书详情/翻页均逐次授权；后者新增真实交互待验收。

## 持久审批卡片引用（2026-10-11）

`ApprovalCardBindings` 是受信业务服务的内部接口，不开放客户端创建绑定。`issue(approval_id, identity, chat_id=..., message_id=..., decision_intent="approve")` 接收服务端确认的卡片投递目的地与消息 ID，重新读取待审批记录并检查当前审批权限、职责分离和有效期；返回高熵、不透明 `CardAction`。省略意图仍签发兼容的 `approve` 引用；2026-10-21 起另支持 `reject` 和只读 `view`，未知意图失败关闭。绑定只保存引用 SHA-256、可信租户/审批 ID、完整审批版本指纹、决定意图、卡片 JSON 版本、外部 issuer/subject、actor、chat/message ID 和有效期，不保存原始引用、卡片正文、checkpoint 或凭证。

`platform_0011` 新增 `approval_card_bindings`，以 tenant + 引用摘要为主键，带 tenant + approval 复合外键；PostgreSQL 继承 FORCE RLS。迁移通过既有明确初始化/升级入口执行，API 不自动迁移。本轮只操作临时 SQLite 数据库，测试升级/降级和资源重建后解析；真实 PostgreSQL 未验证。

`resolve(verified_event)` 读取绑定与审批记录，并记录必要的授权审计；它不提交审批决定，不写 receipt/replay 或 outbox，也不是决定凭据。`TransactionalApprovalCardAcceptor` 才是点击受理接缝：它只使用 `TransactionalFeishuReceiver` 提供的 session，调用现有 `ApprovalService` 的 Policy、SOD、过期和策略版本不变量，以完整快照 CAS 写入决定、审批审计和 outbox，与 receipt/replay 一起提交或回滚。成功审计的 correlation ID 为 receipt_ref。

receiver 在受理事务提交前再次核验当前身份及回调有效期，处理期间撤权会使审批、审计/outbox 和 receipt/replay 一起回滚；该检查也覆盖重复回调。Policy 拒绝的受理先回滚，再经原有 AuditService 独立记录脱敏拒绝审计，避免 SQLite 写锁争用；这类审计不会创建新的业务受理或 outbox。

同一已提交决定 action 的重试会重查当前身份、Policy、SOD 和 binding，但不要求审批仍为 pending 或匹配签发时 revision，也不会再写审批/审计/outbox。另一决定 action_ref 指向已决定审批时则失败并回滚新 receipt。本地注入 receiver 的签名 HTTP 回调已验证；10 月 16 日补齐显式配置的服务启动装配，默认仍关闭，缺失时保持 503。10 月 18 日新增受信内部投递/更新与终态结果卡片；默认入口的自动投递触发、Job/wait 与 Graph 恢复仍待后续；真实外部验证的范围见下文。

## 受信审批卡片投递与更新（2026-10-18）

`ApprovalCardDelivery` 是受信内部服务接口，复用同一个 RuntimeServices、`ApprovalCardBindings`、当前身份目录和 `FeishuChannel`。调用 `deliver(approval_id, sender=..., approver=..., chat_id=..., delivery_id=...)` 时，sender 和 approver 必须来自当前服务端身份映射且属于同一租户；发送者仍须满足通知 Policy/收件人白名单，审批人另受明确的 tenant/chat/subject 目的地白名单、审批 Policy、有效期与职责分离限制。不能让聊天文本或客户端提交角色、租户、目的地、审批人映射或消息 ID。

投递顺序固定为：授权审批摘要 → 发送无按钮共享卡片 → 取得非空消息回执 → 持久绑定该消息与完整审批版本 → PATCH 同一消息以显示交互控件。`issue_view()` 从同一已授权审批快照原子签发三种绑定，确保摘要与绑定版本一致；原始 action_ref 不落库。发送前与取得 token 后都重查两人的当前身份映射。令牌按 tenant 注入，不由服务读取个人环境或凭证文件。

发送结果不确定时不签发绑定、不激活按钮、不自动重发。卡片已发送但绑定失败时，返回 `card_binding_unavailable` 和该消息回执，保留无按钮卡片供人工对账；激活 PATCH 失败时如实返回拒绝或 `delivery_unknown`，不额外发送卡片。服务器不会因展示失败回滚或重做已经提交的审批决定。可靠投递账本、后台重试和跨进程主动 PATCH 恢复仍由 T05 后续实现；签名回调的终态响应则可复用持久绑定跨进程恢复。

10 月 18 日通过卡仅包含原批准按钮，见 [投递与结果更新卡](../../reports/verification/v0.6/20261018-t02-approval-delivery/summary.md)。2026-10-21 新增 JSON 2.0 拒绝表单及详情/上一页/下一页，默认启动仍不发送消息，新增真实交互待独立验收，见 [飞书审批联调指南](feishu-approval-validation.md)。

## 带理由拒绝与审批详情（2026-10-21）

本地新增 `platform_0012`，测试仅迁移临时 SQLite 库，未升级任何用户数据。旧绑定默认保留 JSON 1.0；受信投递的新卡片从占位到终态使用 JSON 2.0。存在 reject/view 绑定时降级会拒绝，不能通过删除数据降级。本 revision 的 PostgreSQL 未实测，10 月 17 日证据不覆盖它。

拒绝按钮提交 `action.form_value.rejection_reason`，要求 1–1000 字且不能全为空白；未知表单字段、非字符串和超长理由拒绝。决定类型以绑定记录为准，给 approve 引用添加理由不能改为拒绝。理由只按已有领域契约保存，受理账本保存内容摘要；同一动作换理由返回 409，不能修改已提交决定。

详情和每次翻页均重新检查目录、Policy、职责分离、卡片身份/chat/message/有效期及实际页数。详情回调不决定审批、不生成业务 outbox，重试不新增绑定；审批版本改变后不恢复旧版本的决定控件。终态移除批准/拒绝及输入框，可保留只读详情入口。所有操作仍复用现有 HTTP/卡片审批运行时，没有 Job 处理器时文本消息继续返回 503。

实现与验证边界见 [ADR-039](../adr/ADR-039-reasoned-approval-card-interactions.md) 和 [本轮验证卡](../../reports/verification/v0.6/20261021-t02-approval-interactions/summary.md)。T02 仍进行中；按详细路线 §1.4，新增真实交互验证不作为后续代码实施前置，T03 已继续开发，整体门禁仍保留。

## 验证与限制

2026-10-17 已另行通过 [PostgreSQL 17.11 本地持久化验证](../../reports/verification/v0.6/20261017-postgres-local/summary.md)。它覆盖迁移、Repository/RLS/连接池与官方 saver，未改变本指南中 `api serve` 仍限定 Mock/Fixture 与 SQLite 的装配范围；不是 PostgreSQL API、真实飞书或 Job 恢复的证据。

本地自动化测试使用真实 RSA 签名与 AES 加密、合成飞书载荷和 Mock HTTP transport。七种展示状态有 golden 文件。去重测试使用真实 SQLite 事务、并发请求、独立进程异常退出和重建连接。这些本地测试不调用真实 OIDC/飞书应用，不使用真实用户或企业数据，也不发送真实消息。历史状态见 [基础验证卡](../../reports/verification/v0.6/20261003-t02-foundation/summary.md) 和 [持久去重验证卡](../../reports/verification/v0.6/20261005-t02-receipts/summary.md)。

2026-10-18 已通过独立的 [真实飞书出站验证](../../reports/verification/v0.6/20261018-feishu-channel/summary.md)：经用户明确批准，使用已有 ClawBot 应用向唯一匹配的 oriaTest 群发送一条合成文本和一张无按钮卡片。现有 `FeishuChannel`、`IMViewModel` 和 Policy 均参与调用；两次发送均返回成功回执，用户确认两条可见且卡片显示正常。验证上下文中的 actor/executor 为合成身份，凭证只在进程内使用；未验证真实用户身份映射、入站事件、审批按钮回调、Job/Graph 恢复或正式投递装配，未修改应用权限与事件配置。该结果不关闭 T02 门禁，也不新增默认运行的 pytest Enterprise target。

协议核对来源：[PyJWT 使用文档](https://pyjwt.readthedocs.io/en/latest/usage.html)、[飞书官方回调处理源码](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/event/dispatcher_handler.py)、[官方 AES 解密源码](https://github.com/larksuite/node-sdk/blob/main/utils/aes-cipher.ts)、[卡片回调结构](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/event/callback/model/p2_card_action_trigger.py)、[发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)。

2026-10-18 独立 OriaTest 的 [真实审批联调](../../reports/verification/v0.6/20261018-t02-real-approval/summary.md) 已通过：唯一测试群与真实审批人映射、一张合成卡片发送及 PATCH 激活、接收进程重启后真实签名/AES 回调、单次决定和终态卡片显示。用户确认“已批准，按钮已消失”；捕获的同一真实请求仅在内存中复验一次，未新增审计/outbox。实测发现并修复上述 Go 时间文本兼容问题，保留全部失败历史。临时入口已关闭，测试目录映射已移除；默认入口自动投递、带理由拒绝、详情/翻页与 Job/Graph 仍待后续。
