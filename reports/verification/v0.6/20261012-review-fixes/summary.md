# Oria 审查修复与全工程检查（2026-10-12）

后续状态：本报告新增的三项缺陷已在后续修复中处理，见 [新增问题修复验证卡](../20261012-additional-review-fixes/summary.md)。下文保留发现时的状态与原始证据。

此前委托审查的三项问题及同快照原生 OCR 的一项问题均已修复；本轮全工程检查另确认三项待修复缺陷。全量本地测试通过，但这些新增缺陷未被现有测试覆盖。

## 已完成的四项修复

| 问题 | 修复 | 验证 |
| --- | --- | --- |
| 异步业务处理期间撤权仍可提交审批 | receiver 在 acceptor 完成全部读取/写入后、事务提交前重新检查当前身份及回调有效期；失败使决定、receipt/replay、审计和 outbox 一起回滚 | 新 action 与已提交 duplicate，分别在读取及业务写入后撤权，四个真实 RuntimeServices + SQLite 回归用例 |
| 事务化 Policy 拒绝没有授权审计 | 拒绝携带延迟审计动作，在 receiver 事务回滚完成后，通过原 LocalPolicyEngine 记录脱敏拒绝事件，避免 SQLite 第二连接写锁冲突 | 新 action 与 duplicate 两个回归：仅 audit 增加一条，审批及其他账本均不变；五秒内完成 |
| resolve() 的零 audit 结论由替身掩盖 | 保留真实授权审计；测试装配 PlatformAuditService，并增加完整 RuntimeServices 的重复解析验证；校正接口说明及两份历史报告 | 每次解析有一次授权审计，审批决定、receipt/replay/outbox 与绑定不变 |
| 两个 repository 重复维护失效化 SQL | owner 与 session-bound facade 共用 `_invalidate_campaign_binding`，保持各自事务所有权和原 SQL 语义 | 既有审批失效化、事务受理及全量套件通过 |

实现涉及 `ingress/receipts.py`、`ingress/approval_cards.py`、`permission/local.py`、`storage/platform.py`，保留工作区已有改动。未提交或推送。

## 验证结果与失败历史

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 修复前新增回归 | 6 failed，19 deselected；证明两个行为缺陷可复现 | [regression-before.log](regression-before.log) |
| 初次定向验证 | 30 passed，2 failed；旧用例仍断言拒绝零 audit | [targeted-initial.log](targeted-initial.log) |
| 最终定向验证 | 32 passed，1 warning，18.66 秒 | [targeted-final.log](targeted-final.log) |
| 全工程格式、Lint、类型 | 358 个 Python 文件格式通过；Ruff 通过；179 个源码文件 mypy 通过 | [lint-initial.log](lint-initial.log) |
| 全量本地套件 | 1073 passed，3 deselected，5 warnings，292.46 秒；包含 Contract/Integration/Security/Recovery | [community-final.log](community-final.log) |
| CLI smoke | `oria --version` 返回 0.1.0 | [smoke-final.log](smoke-final.log) |
| wheel / sdist 构建 | 通过；离线复用本机已有、满足 pyproject 范围的 Hatchling 1.32.4，不安装新依赖 | [build-final.log](build-final.log)、[build-recreated.log](build-recreated.log) |
| wheel 内容 | 四份修复源码及迁移文件/manifest 与工作区逐字节一致 | [wheel-content-final.log](wheel-content-final.log) |

初次 `make build` 使用独立空缓存，因离线缺少 Hatchling 失败，保留 [build-initial.log](build-initial.log)。首份构建产物位于临时目录，续接后不可读取，因而重新构建到被 Git 忽略的 `.artifacts/review-fixes-20261012/` 并验证内容；该产物未纳入提交。全量测试的五条 warning 来自 Starlette/httpx 弃用提示及既有迁移外键反射提示。

全部运行使用现有锁定环境，关闭依赖同步/网络。未运行 Live Provider、真实飞书、PostgreSQL/Enterprise 或 Performance；没有向 Kimi 或其他外部审查服务发送代码。

## 全工程检查范围

- 清点、语法解析及哈希覆盖 179 个源码文件、29 个工程脚本、150 个测试文件，共 358 个 Python 文件，见 [whole-project-inventory.json](whole-project-inventory.json)。
- 全工程运行上述格式、Lint、类型及本地测试；人工复核重点覆盖 API/飞书身份、Policy/Guardrail、工具执行、审批 CAS/事务、tenant 隔离、checkpoint/ledger 恢复、Memory 生命周期、RAG 授权与对象路径、只读分析查询、Provider 会话与结构化输出、Agent 权限/预算以及资源/迁移装配。
- 以下新增问题均用本地合成数据复现，见 [whole-project-probes.log](whole-project-probes.log)。Memory 探针使用真实 RuntimeServices + SQLite；Provider 探针仅替换传输层，不启动 app-server 或调用模型；JSON 探针调用真实解析函数。

## 新增待修复问题

### [P1] Codex Provider 的模型会话没有租户命名空间

位置：`src/oria/providers/codex_app_server.py:196–205`。

同一个 Provider 实例收到 tenant A/B 的相同外部 `thread_id` 时，`_threads` 和 `_message_counts` 都仅按 `ctx.thread_id` 索引，因此 tenant B 复用 A 的远端模型会话及历史消息计数。探针给两个租户分别发送一条不同消息，只创建一个模型线程；B 的输入还因 A 的计数被截成空列表。真实通道下，这既会串用前一个租户的模型上下文，也会遗漏当前租户输入。默认 Mock 路径不受影响，风险发生于选用 Codex 且共享 Provider 的多租户运行时。

建议：模型线程及消息计数至少同时绑定 `tenant_id + thread_id`，增加相同外部线程 ID 的跨租户回归，并核验同租户的会话所有权边界。本次未进行真实 Codex Live 调用。

### [P2] Memory 缓存命中绕过过期及删除状态

位置：`src/oria/memory/persistent.py:117–120`。

`search()` 命中缓存直接返回完整 MemoryItem，未重新查询有效记录。真实 SQLite 探针先缓存查询，再推进时钟超过 `expires_at`：`view()` 已为空，相同 `search()` 仍返回该记忆。另一个运行实例删除记录后，数据库正文/向量已清除，但原实例相同查询仍返回缓存正文。现有 TTL 测试到期后改用另一查询，未覆盖命中缓存的路径；这违反 ADR-012 的到期不可见和删除传播约定。

建议：缓存结果返回前核验数据库当前生命周期，或仅缓存可安全复用的计算结果；只过滤本地 expires_at 无法解决其他实例的删除。补充相同查询跨 TTL、另一运行实例删除、空结果缓存后新增记忆的生命周期回归。

### [P2] 尾逗号修复会静默改变 JSON 字符串

位置：`src/oria/providers/structured.py:89`。

`_TRAILING_COMMA.sub` 对整个文本执行替换，没有识别字符串及转义边界。输入 `{"text":"synthetic,}",}` 本应只删除对象末尾的多余逗号，真实 `parse_structured_text()` 返回的字符串却变成 `synthetic}`。修改后的对象仍可通过 JSON Schema，因此模型返回的证据/文本会被静默改写。

建议：使用识别字符串和转义的扫描器，仅修复字符串外的尾逗号，或将这类非法 JSON 交回原有校验修复流程；增加包含 `,}`、`,]` 和转义引号的回归。

以上三项属于全工程检查新增发现，尚未修改相关实现。此前四项修复已完成；V0.6-T02 仍在进行中，生产飞书装配、Job/wait/Graph 恢复及真实通道门禁不因本轮本地验证而关闭。
