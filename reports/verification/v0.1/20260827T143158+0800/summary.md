---
run_id: "20260827T143158+0800"
version: "V0.1"
task_id: "V0.1-T03"
depends_on: ["V0.1-T02"]
verification_level: "F"
commit: "1b67b431e3ccf7def5400179a346c9ad816443b9"
executed_at: "2026-08-27T14:31:58+08:00"
environment: "macOS 26.6.1 (Darwin 25.6.0 x86_64) / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:14df51cee897e58e68b5608e245bd1a7… / pydantic 2.13.4 / sqlalchemy 2.0.52 / alembic 1.19.1 / aiosqlite 0.22.1 / langgraph-checkpoint-sqlite 3.1.1 / pytest 9.1.1 / mypy 1.20.2 / ruff 0.16.4"
provider_model: null
config_fingerprint: "sha256:c4604efae52f0027d1d345af68eaa903242fc8c7fe4169fb4469967f3dcff48f"
dataset_version: "oria-synthetic-merchant-recruitment 1.0.0"
eval_fingerprint: null
commands:
  - cmd: "make lint"
    result: "60 files already formatted；Ruff All checks passed；mypy Success: no issues found in 41 source files"
  - cmd: "make test"
    result: "82 passed"
  - cmd: "uv run pytest -m 'unit or contract'"
    result: "56 passed, 26 deselected"
  - cmd: "uv run pytest -m security"
    result: "23 passed, 59 deselected"
  - cmd: "uv run pytest -m integration"
    result: "3 passed, 79 deselected（integration marker 首次出现真实用例，均为 tmp_path 下本地 SQLite，无网络、无外部服务）"
  - cmd: "uv run python scripts/verify_t03_bypass_boundaries.py --work-dir .artifacts/probe"
    result: "rejected storage exposure, hard-rule overrides, restricted-field serialization, migration detours, path escapes, and asset tampering"
  - cmd: "uv build"
    result: "Successfully built dist/oria-0.1.0.tar.gz 与 dist/oria-0.1.0-py3-none-any.whl"
  - cmd: ".artifacts/package-venv/bin/python scripts/verify_t02_wheel.py"
    result: "imported 40 Oria modules from .artifacts/package-venv/lib/python3.11/site-packages/oria/__init__.py（T02 时为 20 个）"
  - cmd: "（在 .artifacts 目录内，非源码 cwd）./package-venv/bin/python ../scripts/verify_t03_wheel.py --data-dir ./t03data"
    result: "verified installed T03 wheel assets, two revisions, idempotent data init, and zero Campaign/CouponBatch tables"
  - cmd: "uv run mypy --strict --python-executable .artifacts/package-venv/bin/python scripts/wheel_type_consumer.py"
    result: "Success: no issues found in 1 source file（探针已扩展为消费 Context / JsonValue / EligibleMerchantSet / MerchantService）"
  - cmd: "（在 .artifacts 目录内）./package-venv/bin/oria data init --data-dir ./clidata --output json"
    result: "第一次：{ok:true, merchants_inserted:12, platform_revision:platform_0001, business_revision:business_0001, dataset_version:1.0.0, saver_setup:true}"
  - cmd: "（重复执行同一命令验证幂等）"
    result: "第二次：merchants_inserted:0，其余字段一致；data_dir 内仅生成 sqlite/platform.db 与 sqlite/business.db"
artifacts:
  - "src/oria/domain/{models,eligibility,repositories,services}.py"
  - "src/oria/storage/{database,repositories}.py"
  - "src/oria/migrations/{runner,manifest.json}、migrations/platform/versions/platform_0001_catalog.py、migrations/business/versions/business_0001_merchants.py"
  - "src/oria/resources/{loader.py,demo_data/{campaign_rules.v1.json,merchants.v1.json,manifest.json}}"
  - "src/oria/data.py、src/oria/cli.py（data init 子命令）"
  - "tests/unit/test_t03_resources_and_eligibility.py、tests/contract/test_t03_domain_services.py、tests/integration/test_t03_data_init.py、tests/security/test_t03_bypass_boundaries.py"
  - "scripts/{verify_t03_bypass_boundaries,verify_t03_wheel}.py、scripts/wheel_type_consumer.py（扩展）"
  - "dist/oria-0.1.0-py3-none-any.whl、dist/oria-0.1.0.tar.gz"
evidence_refs:
  - "reports/verification/v0.1/20260827T084858+0800/summary.md（T01 工程基线）"
  - "reports/verification/v0.1/20260827T112706+0800/summary.md（T02 首次报告；result: passed 判定过早，保留不改）"
  - "reports/verification/v0.1/20260827T133710+0800/summary.md（T02 remediation；result: blocked，保留不改）"
  - "reports/verification/v0.1/20260827T134842+0800/summary.md（T02 package 门禁补验；result: passed）"
assertions:
  - id: "T03 产物 / Domain Service 类型化契约"
    covered: true
    note: "ctx.domain 的公开成员精确等于 {campaign_rules, merchants}；Context、RuntimeServices 与 domain registry 均不暴露 repository/repositories/engine/session/session_factory；MerchantService.eligible_merchants 的参数经 inspect.signature 断言恰为 (rule_set_id, limit, ctx)。Repository 在唯一 build_runtime() 内完成进入 context manager、取得资源、注入 Service，无第二套装配路径。"
  - id: "T03 产物 / EligibilityPolicy"
    covered: true
    note: "类目、城市、报名系统、黑白名单、销售组织按确定性 AND 语义过滤，denylist 优先级最高；同输入两次评估结果相等（确定性）。对照 ADR-028，LLM 不参与硬资格判定。"
  - id: "T03 产物 / 两条 migration 与双库 revision"
    covered: true
    note: "platform_0001_catalog 建立 documents/document_versions/ingestion_runs；business_0001_merchants 建立商家表；两库各自独立版本表 alembic_version_platform / alembic_version_business。"
  - id: "T03 产物 / wheel 内脱敏 resources 与 manifest"
    covered: true
    note: "demo_data manifest 声明 dataset_id、version 1.0.0、source=synthetic、contains_real_entities=false、license=CC0-1.0、固定 generator_seed、六类规则字段清单与两个数据文件的 sha256；规则 JSON 顶层即 basic/recruitment_scope/enrollment_policy/benefit_policy/confirmation_policy/merchant_material 六类；商家数据 12 条。migrations manifest 记录双链 head 与文件 sha256。调用方另行 grep 确认资源中无任何真实公司或商家名称。"
  - id: "T03 产物 / oria data init"
    covered: true
    note: "经同一 runner 升级两库并调用官方 saver setup；从已安装 wheel 在全新非源码目录执行成功，首次插入 12 个商家、重复执行插入 0 个（幂等），仅在 data_dir 内生成两个 SQLite 文件。"
  - id: "T03 完成验证 / 已安装 wheel 中 migration 与 resource"
    covered: true
    note: "verify_t03_wheel.py 在仅装新 wheel 的隔离解释器中验证 assets、两条 revision、data init 幂等，并确认 business 库中零 Campaign/CouponBatch 表（§4.1 只读边界）。"
  - id: "绕道断言 / 存储层不可绕过"
    covered: true
    note: "无第二条获取 Repository 的路径；向 eligible_merchants 传 filters={'ignore_denylist': True} 抛 TypeError。"
  - id: "绕道断言 / 硬规则不可放宽"
    covered: true
    note: "六个硬规则集合字段任一传空元组均在校验期抛 ValidationError，调用方无法用空集合语义放宽硬条件。"
  - id: "绕道断言 / 受限字段不泄漏"
    covered: true
    note: "denylist 商家 ID 与销售组织值在 model_dump_json()、repr() 与 caplog 捕获的 DEBUG 日志中均不出现；allowlist_merchant_ids / denylist_merchant_ids / sales_org_scope 三个键本身不进入 model_dump()；调用方提供的规则 ID 不被 LookupError 消息回显。"
  - id: "绕道断言 / 路径逃逸"
    covered: true
    note: "在 data_dir 下预置指向外部目录的 symlink 后 data init fail closed，外部目录保持为空（补足了 Path.is_relative_to() 仅为词法检查的弱点）；production 配置经 model_copy 重新引入相对 data_dir 亦被拒绝且未创建目录。"
  - id: "绕道断言 / 迁移绕道"
    covered: true
    note: "预先 stamp platform head 试图跳过建表被拒且不创建 business 库；把 platform 版本表写入 business 链的 revision 被拒，business 库 revision 保持 business_0001（双库不串链）。"
  - id: "绕道断言 / 资产篡改 fail closed"
    covered: true
    note: "篡改 demo 资源内容触发 integrity 错误、删除 migration 文件触发 unavailable 错误；且资产校验失败发生在任何 data_dir 写入之前（断言 data_dir 未被创建）。"
result: "passed"
blocked_by: []
known_limits:
  - "验证等级仅为 F（Fixture）。全程未调用真实 Provider、未下载或推理任何真实 Embedding 模型、未接入企业系统。SQLite 为真实本地组件，但按 §1.2 口径本轮只声明 F，不声明 C（Community Real）——真实本地链路的完整声明留待 T05 起 Retriever/Chroma 落地后统一评估。"
  - "本轮严格未实现 T04 及以后：无 Provider/Embedder/结构化输出、无 RAG/Retriever/CampaignRuleSnapshot、无 Tool、无 PromptManager/StateGraph/Checkpoint、无 oria demo。"
  - "V0.1 Core Gate 仍未通过（尚缺 T04–T09）；Live 卡未运行。"
  - "六类规则字段目前只做到「结构齐全 + 可校验」，逐字段来源引用（citation）属 T05 的 CampaignRuleSnapshot 职责，本轮不覆盖。V01-RULE-01/02 未在本轮验证。"
  - "过程事实：T03 实现由 Codex 产出，中途因用量限额中断（230,147 tokens，剩余额度需等待重置），当时留下 1 个 ruff format 未通过、报告与状态文档未写。调用方（Hermes）完成 ruff format/check 修复、全部 12 条门禁命令实跑、绕道测试与 demo 数据脱敏的独立审查、本报告与状态文档撰写、提交与推送。Codex 未执行 commit/push（按指令）。"
  - "调用方独立审查范围：确认无 type:ignore/noqa/skip/xfail/assert True，pyproject.toml 未被放宽，四份历史报告字节未变，demo 资源无真实公司或商家名称，14 个绕道测试逐条阅读确认为真实攻击路径而非形式断言。"
  - "GitHub Actions 上 T03 的首次 CI 结果需在推送后确认，特别是新增的 integration 测试是否被 required test-core 正确收集、以及 Linux 上 alembic/SQLite 行为是否与 macOS 一致。"
---

# V0.1-T03 验证报告

## 结论

`V0.1-T03` 产物完成，12 条门禁命令在本机实际通过，证据见上方 `commands`。按路线 §1.4「任务完成」口径判定为 **完成**，验证等级 **F**。

**V0.1 Core Gate 仍未通过**（尚缺 T04–T09），Live 卡未运行。下一步为 `V0.1-T04`（Provider / Embedder / 结构化输出），其后 `V0.1-T05` 依赖 T03 与 T04 两者。

## 本轮首次执行 T02 复盘定下的规则

T02 的误判成因是「断言只覆盖直接路径被拒绝，未覆盖绕道」。本轮起测试清单强制区分两类断言，效果具体体现为 14 个绕道用例，其中三条覆盖了先前 `references` 级别已知但未被测试的弱点：

| 绕道方向 | 具体攻击 | 结果 |
| --- | --- | --- |
| 参数后门 | `eligible_merchants(..., filters={"ignore_denylist": True})` | `TypeError`（签名无此参数） |
| 空集合语义 | 六个硬规则字段分别传 `()` | 校验期 `ValidationError` |
| 日志侧信道 | DEBUG 级 `caplog` 中查找受限商家 ID 与销售组织 | 均不出现 |
| 错误消息回显 | 调用方提供的规则 ID 是否出现在 `LookupError` 文案 | 不出现 |
| 符号链接逃逸 | `data_dir/sqlite` symlink 到外部目录 | fail closed，外部目录为空 |
| 事后改配置 | `model_copy(update={edition: production, data_dir: 相对路径})` | fail closed，目录未创建 |
| 迁移跳过 | 预先 stamp `alembic_version_platform` 为 head | fail closed，business 库未创建 |
| 双库串链 | 把 business 的 revision 写进 platform 版本表 | 拒绝，business revision 未受影响 |
| 资产篡改 | 改 demo 资源内容 / 删 migration 文件 | `integrity` / `unavailable` 错误 |
| 写入顺序 | 资产校验失败时是否已创建 `data_dir` | 未创建 |

其中 symlink 一条直接补上了 T02 报告里记录的「`Path.is_relative_to()` 是词法检查，不能证明真实文件系统边界」这一遗留弱点。

## 测试规模变化

| 阶段 | 全量 | unit+contract | security | integration |
| --- | --- | --- | --- | --- |
| T01 | 7 | 7 | 0 | 0 |
| T02 首次 | 24 | 15 | 9 | 0 |
| T02 remediation | 47 | 38 | 9 | 0 |
| T03 | **82** | 56 | **23** | **3** |

## 对 T04 / T05 的前置提示

- T04 必须遵守 T02 remediation 收紧后的 provider/dialect 与 capability 矩阵（矛盾组合已在配置层拒绝），并沿用 reasoning / provider raw payload 的「默认公开投影脱敏 + 内部显式访问」边界。
- T04 的 Provider/Embedder 装配必须继续走唯一的 `build_runtime()`，与本轮 Repository 注入方式一致，不要另起 Provider Runtime。
- T05 的 `CampaignRuleSnapshot` 需要为六类规则字段补齐逐字段来源引用；本轮 demo 数据已按六类分节且带 sha256 manifest，可直接作为提取与校验的输入。
- 新增领域值类型继续继承 ADR-030 的深度不可变与非有限浮点拒绝契约。
