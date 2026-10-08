# API 与飞书入口基础（V0.6-T02 进行中）

当前交付可运行的 HTTP 外壳、JWT 身份映射、资源授权接缝、飞书签名/加密回调解析和出站适配器、IM v1 展示模型，以及事务化持久去重接收器。新增可注入运行时的审批详情与决定 HTTP 路由。尚未交付 Job 提交/查询/取消、飞书审批 action 持久绑定、实际业务受理处理器或真实飞书双场景链路。不要把 HTTP 健康检查或合成回调通过解释为企业接入完成。

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

JWT 中即使签有 `tenant/roles` 也不会被用作授权事实；仅 `(issuer, sub)` 选择目录中的当前 Principal。资源层 `authorize_resource` 还会重新解析身份、检查租户、复用现有 PolicyEngine，并对审批执行职责分离。资源必须先按可信 tenant 查询；不存在与拒绝都返回相同错误。审批路由已复用既有 ApprovalService 的 tenant 查询与 Policy；Job 路由仍未连接，不能据此声明双场景全链路验收完成。

错误统一为 `{code, message, request_id, retryable, details}`，不返回异常原文、校验输入、外部响应、令牌或回调正文。`request_id` 由服务端生成；响应禁止缓存。OpenAPI 可在 `/openapi.json` 查看，完整 snapshot/drift 门禁仍属 T06。

## 审批详情与决定（2026-10-06）

受信应用可通过 `create_app(authenticator=authenticator, approvals=ApprovalAPI(runtime, repository, directory))` 注入已启动的 RuntimeServices、既有 ApprovalRepository 和当前身份目录。调用方负责资源生命周期；库接口不会迁移数据库或新建生产审批。`api serve` 现可通过显式 `approval_runtime_config` 装配；不配置时仍对已认证审批请求返回 **503**。

- `GET /v1/approvals/{approval_id}?page=1` 按当前可信 tenant 查询，并重新检查审批角色与身份映射。返回 `detail_ref/status/revision/page/pages/card`；不存在与无权访问均为 404。详情只投影操作范围、时间、审批说明和活动编号，沿用卡片脱敏，不返回 requester、checkpoint、参数摘要或原始状态。当前投影最多五段，所以只有第一页；越界页拒绝。
- `POST /v1/approvals/{approval_id}/decision` 接收 `decision`（`approve` 或 `reject`）、从详情取得的 `expected_revision`，以及可选 `reason`。拒绝必须提供非空白理由；额外的 tenant、role、checkpoint、wait 等参数返回 422。版本已变化或重复决定返回 409，应刷新详情重新核对。
- 决定复用现有职责分离、有效期与策略版本校验；发起人不能自批。通过版本检查也不代表获得执行权限，后续 Graph 恢复仍需原有参数、checkpoint、业务绑定和 Policy 校验。接口只记录决定，不直接恢复 Graph。
- 审批 Repository 的 `replace` 必须携带读取时的 `expected` 快照。数据库更新要求整条快照仍匹配，成功决定和 audit/outbox 同事务；两个不同请求竞争同一审批时，仅一个成功，失败方不覆盖决定、不新增审计。内存 Repository 保持同一契约。没有新增迁移或依赖。
- 此 HTTP 版本校验不是飞书 `action_ref` 绑定。卡片动作与 tenant/资源/版本/wait 的持久关联、按钮受理和后续 Job 恢复仍待实现；没有因此打开默认飞书接收器。

## 启用本地审批服务（2026-10-08）

在服务端 JSON 配置中增加 `"approval_runtime_config": "runtime.yaml"`，文件路径相对该 JSON 所在目录解析。运行时 YAML 使用现有配置格式；最小示例：

```yaml
data_dir: /absolute/path/to/initialized-oria-data
```

本轮仅允许 Mock LLM、Fixture Embedding 和 platform/business 双 SQLite。API 只读取显式 YAML，不加载个人默认配置，也不接受 `ORIA_*` 环境覆盖。运行时 YAML 中相对 `data_dir` 以 YAML 所在目录解析；建议使用绝对路径，避免其他 CLI 命令相对工作目录解析时产生差异。

数据库必须事先通过已有 `oria db upgrade --config /path/to/runtime.yaml` 或演示数据初始化流程准备。启动只读核验两条迁移链和完整 schema；缺库、旧版本或损坏时停止，不创建数据库、不自动迁移。通过检查后启动现有运行时，其 saver、向量索引和对象存储会在配置的数据目录初始化运行所需资源。部署操作须自行明确授权；本次测试只使用临时目录。

仍使用 `oria api serve --config /path/to/server.json` 启动。应用 lifespan 管理审批运行时、数据库和 HTTP 客户端；启动失败与正常关闭均回收资源。审批决定持久化在上述 platform 数据库。身份目录的启动快照提供 Policy 信任集合，每次请求仍读取当前目录；撤销映射即时生效，新增用户或角色变更需重启，使 Policy 快照同步。`--local` 不启用审批服务。此装配未启用飞书 receiver、Job、真实模型或 PostgreSQL 接入。

## 飞书边界

服务端配置可加 `feishu` 对象，包含 `app_id`、`encrypt_key`、`verification_token`，必须由部署方提供并保密。默认时间窗 300 秒；不配置时飞书入口返回 503。应用开启 `im.message.receive_v1` 和新版 `card.action.trigger`，使用 `/v1/ingress/feishu/events`。旧版回调与非文本消息暂不支持。

- URL challenge 验证 Verification Token；支持 AES-256-CBC 加密载荷。携带签名头的 challenge 也必须通过验签。
- 普通回调要求完整签名、时间窗、应用 ID、Token 和平台租户/用户一致；使用 SHA-256(timestamp + nonce + encrypt_key + 原始 body) 验签后解密。
- 身份目录使用 issuer `feishu:<app_id>:<tenant_key>` 与 subject `open_id`，不从文本或卡片 value 获取角色/租户。
- 消息按平台 source message 派生稳定去重键；卡片 value 只允许版本和不透明 `action_ref`。该引用不等于审批授权，后续处理器必须在服务端绑定资源/版本/wait 并重新鉴权。
- `TransactionalFeishuReceiver` 已实现持久 source/action/replay 去重：`platform_0010` 新增只保存键、摘要与回执引用的 `ingress_receipts/ingress_replays`。业务处理器的授权和写入与账本共用同一事务，失败或提交前崩溃一起回滚；相同消息重投不再次写入，不同内容复用同一键返回 409。重复请求也重新检查当前身份及业务授权。
- 接收器只接受受信服务实现的 `TransactionalAcceptor`，其 `persist` 只能使用提供的事务写入已接受业务请求，不能另开连接、主动提交或执行外部副作用；`authorize` 必须绑定真实资源、版本和 wait 并调用 Policy。当前只有合成业务处理器用于验证，默认服务仍没有实际 receiver 装配，已验签业务消息仍返回 **503**。不能把合成写入的一次性证明当作 Job/审批链路完成。
- 新表带 tenant 复合键与 PostgreSQL FORCE RLS；本轮仅实测 SQLite。迁移通过已有 `oria db upgrade` 路径执行，测试只操作临时数据库；没有改动用户现有数据。nonce 的到期字段已保存，但本轮不自动清理账本，受理回执必须保留至业务幂等保留期结束后再制定清理策略。
- `FeishuChannel` 提供授权后的文本与卡片发送；只向服务端配置的 tenant/chat/subject 白名单发送，调用现有 `notification:send` Policy。部署方注入按 tenant 取 token 的函数，当前 CLI 不装配出站通道。超时返回 `delivery_unknown`，不自动重发；可靠 delivery ledger 属于 T05。文件上传不支持，也不会读取给定路径。
- `IMViewModel` v1 覆盖回执、确认、进度、结果、待审批/待事件与失败，限制字段/长度，每页最多五段，展示详情编号与下一步建议。卡片使用纯文本，经过已有输出脱敏。业务服务只能投影已授权的公开摘要，不能传入隐式思维链或原始 Tool/State。审批 HTTP 详情已接入逐次授权；飞书详情/翻页 action 仍待后续接入。

## 验证与限制

新增测试使用真实 RSA 签名与 AES 加密、合成飞书载荷和 Mock HTTP transport。七种展示状态有 golden 文件。去重测试使用真实 SQLite 事务、并发请求、独立进程异常退出和重建连接。未使用真实 OIDC/飞书应用、真实用户或企业数据，未发送真实消息。完整状态见 [基础验证卡](../../reports/verification/v0.6/20261003-t02-foundation/summary.md) 和 [持久去重验证卡](../../reports/verification/v0.6/20261005-t02-receipts/summary.md)。

协议核对来源：[PyJWT 使用文档](https://pyjwt.readthedocs.io/en/latest/usage.html)、[飞书官方回调处理源码](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/event/dispatcher_handler.py)、[官方 AES 解密源码](https://github.com/larksuite/node-sdk/blob/main/utils/aes-cipher.ts)、[卡片回调结构](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/event/callback/model/p2_card_action_trigger.py)、[发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)。
