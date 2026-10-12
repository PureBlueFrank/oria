# ADR-037：API 身份映射与飞书受理边界

- 状态：实施中，T02 尚未收口
- 日期：2026-10-03
- 关联任务：V0.6-T02 / T03 / T05

## 背景与决策

HTTP 与飞书入口需要把已验证外部身份转换为现有 Principal/Policy 契约。直接采用 token 的角色、将 webhook 验签等同于审批权限，或在数据库受理前返回成功，都会破坏已有权限和恢复边界。

本轮采用固定 RS256、受信 HTTPS JWKS 和服务端身份目录。目录按外部 issuer/subject 映射 human actor 与独立 service executor；租户、角色和属性全部由目录管理。启动时验证目录，每个请求/资源授权边界重新读取，撤权不依赖 bearer 过期。JWT 和飞书都使用同一个目录模型；重复外部标识或同一 Principal 的冲突属性会失败关闭。

资源访问使用现有 PolicyEngine，不建立第二套业务 RBAC。HTTP 身份验证成功只代表“谁在请求”，资源授权、审批职责分离及恢复时重新鉴权仍必需。2026-10-06 新增可注入运行时的审批详情/决定路由，复用 ApprovalService；Job 路由尚未交付；10 月 8 日补齐显式配置的 SQLite 审批生命周期装配。

飞书适配器验证时间窗、签名、Token、应用和平台身份，输出经过结构约束的消息或不透明卡片 action 引用。nonce/source message/action 稳定键随规范化结果传递；真正的防重放和去重必须与受理持久化在同一事务中完成。没有 durable receiver 时 HTTP 返回 503；不能用内存 set 或先写“已处理”标识替代业务受理。2026-10-05 已实现 `TransactionalFeishuReceiver`，由受信服务处理器在同一事务内重新鉴权并持久化；SQLite 合成受理可验证一次性语义。真实 Job/审批处理器仍未接通，T03 不因本基础实现自动满足依赖。

出站通道限制可信 tenant/chat/subject 白名单，执行现有通知策略并使用纯文本/版本化卡片投影。回执不确定时不自动重发；T05 才装配可靠投递。展示层不接受任意字典字段，每页最多五段，携带稳定详情编号；后续详情端点必须再次按当前用户授权。

2026-10-06 审批 HTTP 决定要求详情版本指纹；Repository 更新以完整读取快照为条件，决定与审计/outbox 一起提交，防止不同回调或客户端并发覆盖。指纹仅检测旧版本，不授予权限，也不替代后续持久 action/wait 绑定或 Graph 恢复授权。

2026-10-08：审批启动要求显式 YAML 配置，不继承个人配置或环境覆盖；只读 schema/head 检查先于运行时启动，不自动迁移。当前装配限定 Mock/Fixture 与 SQLite，身份撤销即时检查，新增身份/角色需要重启 Policy 快照。

2026-10-11：新增持久审批卡片引用，摘要存储，tenant/审批版本/服务端决定意图/外部身份/chat/message/有效期绑定；仅受信内部服务签发。同日后续接通 SQLite 事务化批准受理：receipt/replay、完整快照 CAS、审批审计与 outbox 由 receiver 的唯一 session 提交；新 action 重查 Policy/SOD/过期/冻结策略版本，已提交重试重查当前身份、Policy、SOD 与 binding，但不再要求旧 pending revision。当前只签发显式 `approve` 意图；没有安全理由契约的 reject 失败关闭。

## 局限与后续

2026-10-12 审查修复：受理提交前重查身份与回调有效期，覆盖异步处理期间撤权和重复回调；事务内 Policy 拒绝通过请求局部异常携带审计操作，receiver 回滚后才调用原有 AuditService，保持业务原子性并避免 SQLite 双连接写锁争用。解析会记录授权审计，业务决定/receipt/replay/outbox 仍由受理接口负责；真实审计装配纳入解析回归。审批失效化的独立事务与 session-bound 路径共用同一 SQL 实现。

- 文件目录是可替换的首个服务端身份源，需用受限部署权限与原子替换维护；不是完整的企业身份治理或 OIDC 登录流程。
- 持久去重、审批 HTTP 与审批卡片原子批准已有本地 SQLite 验证；尚未装配到默认服务启动路径，也不包含生产卡片投递/更新、飞书详情/分页、带理由拒绝或 Job/wait 恢复。
- 飞书签名和加密按官方 SDK 源码核对，测试使用合成载荷，未运行真实企业通道。
- 新增 FastAPI、PyJWT、cryptography 与显式 uvicorn 依赖，经锁文件同步。10 月 5 日新增 `platform_0010` 账本迁移及完整性清单，保留 tenant 隔离与 PostgreSQL FORCE RLS；仅测试临时库，无生产迁移或新环境变量。回执不存储 raw body、正文、角色、凭证或卡片 token；到期清理不在本轮自动启用。

## 验证

见 [API 使用与限制](../guides/api-foundation.md) 和 [验证卡](../../reports/verification/v0.6/20261003-t02-foundation/summary.md)。现有失败/暂停 Live 卡保持不变，V0.6 Core 未完成。
