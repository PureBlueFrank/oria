# V0.6-T03：当前身份恢复与 Job 租约原语

日期：2026-10-22，Asia/Shanghai。接续 [两场景输入适配](../20261022-t03-graph-inputs/summary.md) 的 1203 项本地基线；原工作区改动保留。任务整体仍为进行中，**不关闭 T03/T04/Core，不声明实际 Graph、领域副作用或多 PostgreSQL worker 已安全接管**。

## 实现

- 每次按 Job 的原 issuer/external subject 重新解析当前 actor，核对 tenant/internal subject，以固定的独立 worker executor 构造 Context。原 API executor、bearer、角色快照不继承；目录属性变更失败关闭。worker 要被 Policy 显式信任且有 job_worker 角色，service ID 必须不同于当前 API executor，双角色信任也不能绕过隔离。
- 新增 `job:execute`，要求资源所有者、operator/campaign_admin、受信 job_worker；招商另查既有 campaign:draft:write，归因另查 analytics:read。各异步边界及事务提交前重查目录/Policy；事务内拒绝先回滚，再经 AuditService 写拒绝审计。
- 内部 claim/heartbeat/finish 使用完整快照 CAS；状态、计数、租约和序号事件原子提交。queued 可领取，running 仅旧租约到期且未取消时可接管。每次 claim 增 epoch；首次 attempt=1，正常审批/事件等待后的 queued 续行保持 attempt，失败接管才增加尝试。预算耗尽、deadline 到期或暂停期间过期均不能继续写入。
- lease_owner 由 tenant/service/实例派生，token 只携带 tenant/job/owner/epoch，不单独授予权限。TTL 默认 30 秒、严格 1–300 秒，并受 Job timeout 限制；实例 ID 默认为新 UUID，仅属于当前进程生命周期。heartbeat 保持 epoch/attempt；取消后只能提交 cancelled/failed，不能发布新指针或成功/等待。
- publish_checkpoint 要求显式 tenant-scoped 官方 saver，namespace 从 Job/epoch 派生，checkpoint 必须真实存在。相同 epoch 只能沿 accepted 父链推进，祖先/兄弟/其他 namespace/缺失指针拒绝，最多回溯 256 个；同一指针重复发布不新增事件。接管保留原 accepted pointer，旧 epoch 迟到写入不影响它。

没有新 migration、依赖、环境变量、HTTP 执行入口或默认开关。Job API 继续只受理。设计见 [ADR-025](../../../../docs/adr/ADR-025-job-lease-and-fencing.md)，操作及限制见 [指南](../../../../docs/guides/durable-jobs.md)。

## 检查与证据

Python 3.11.15、uv 0.12.6，现有锁定 `.venv`。命令均设置 `UV_CACHE_DIR=.artifacts/uv-cache UV_OFFLINE=1 UV_NO_SYNC=1`；真实本机端口仅用于既有回环测试，无外部 Provider 或企业调用。来源与源码指纹见 [source-snapshot.json](source-snapshot.json)，复用的基线卡保持原内容与数字，各轮不累加。

| 检查 | 结果与证据 |
| --- | --- |
| 最终完整本地门禁 | **1228 passed / 9 deselected / 5 既有 warnings**，343.38 秒，退出码 0；[regression-final.log](regression-final.log) |
| 租约最终定向 | **25 passed**，23.38 秒；[targeted-final.log](targeted-final.log) |
| 扩展定向 | 84 passed，45.09 秒；当时 24 个租约用例及既有 Job/Graph 输入/身份安全，[targeted-expanded.log](targeted-expanded.log) |
| 静态 | 381 文件格式、Ruff、190 源文件 mypy 通过；[lint-verified.log](lint-verified.log) |
| 构建 | 最终 wheel/sdist 离线成功；[build-final.log](build-final.log) |
| 包内容与导入 | 6 个模块/manifest 与源码一致、全部迁移 hash 正确，新值类型/服务从 wheel 直接导入；[package-content.json](package-content.json) |
| CLI smoke | 0.1.0；[smoke.log](smoke.log) |

直接验证范围：同进程两服务竞争和两独立仓储进程只有一个 claim；续租不增加 attempt/epoch；过期接管增加 epoch、旧 token 无法 heartbeat/finish/publish；连续四次正常等待续行在 max_attempts=1 时仍可领取；期限截断、失败预算、取消后不可成功；真实 SQLite saver 指针存在性/父链/幂等/旧 epoch orphan；目录/Policy 在提交或 saver 读取期间撤权；事件写失败和期限跨越回滚；跨实例/tenant/epoch 拒绝；API 与 worker 身份不能合并。

两独立仓储子进程使用明确的受信测试 guard，只验证 SQL/CAS，不替代完整 worker 身份装配或进程接管。等待续行用受信 fixture 将 waiting 置回 queued，尚未实现审批/inbox 的真实解析链。时钟为确定性 UTC fixture，不证明 PostgreSQL 分布式时钟行为。saver 证明的是存储 namespace，不证明实际 Graph runner 的 epoch namespace 装配。

## 失败和中间记录

1. 首批 17 passed，随后补充竞争、等待续行、token 作用域及故障用例；[targeted-initial.log](targeted-initial.log)。本轮细化 attempt 与 epoch 的不同语义，避免正常 HITL 消耗失败重试预算，未减免等待/恢复门禁。
2. 初次 mypy 发现 Optional token/parent config 的收窄缺失；随后发现测试嵌套 with 与 SQL 行长，修正后静态检查通过。保留 [lint-initial.log](lint-initial.log)、[lint-second.log](lint-second.log)、[lint-third.log](lint-third.log)、[lint-final.log](lint-final.log)。
3. 审查发现仅要求 worker 角色不足以强制 service ID 独立。第一版用例未信任该 fixture 身份，先被普通 Policy 拒绝，保留 [fixture 初始结果](api-worker-separation-fixture-initial.log)。改为显式受信的合成配置后，旧实现确实未拒绝同 ID，回归失败：[修复前](api-worker-separation-before.log)。加入目录 executor ID 的独立检查后，最终 25 passed，未放宽 Policy 或修改任何真实权限。
4. 身份隔离补强前的完整门禁 1227 passed/9 deselected/5 warnings，399.80 秒，保留 [regression.log](regression.log)；该轮不覆盖补强后的源码或新增第 25 个用例。最终源码另行完整复验，不把前轮结果替代最终结果。初次构建另留 [build.log](build.log)，最终包位于独立输出目录。

最终完整门禁 1228 passed，新增 25 项，不与 1227 或 84 项相加。五项既有告警为 Starlette/httpx 弃用及 SQLite/Alembic 外键反射提示。日志仅脱敏用户/临时路径并清理行尾空白，失败内容保留；机器可读范围见 [checks.json](checks.json)，文档检查见 [documentation-checks.json](documentation-checks.json)。

## 未接入

当前只保护 Job 行及 accepted pointer；领域库提交、execution ledger reservation、外部调用前 fencing 和实际 Graph runner 尚未接入。自动 retry/timeout 回收、取消后过期清理、队列轮询、飞书会话、审批/外部事件到 Job 的恢复、独立 worker 与 orphan 保留/清理仍待后续。finish 是受信内部状态原语，不代替业务结果校验。

没有运行新 PostgreSQL revision、真实飞书、Live/Enterprise/Performance；没有发送消息、开放公网、迁移用户库、提交或推送，无 GitHub Actions。V0.5-T07 保持 29/80。下一优先项为领域提交/副作用边界 fencing，再接受控调度与异步恢复；未通过的版本门禁继续保留。
