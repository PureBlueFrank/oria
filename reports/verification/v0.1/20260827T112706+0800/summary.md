---
run_id: "20260827T112706+0800"
version: "V0.1"
task_id: "V0.1-T02"
depends_on: ["V0.1-T01"]
verification_level: "F"
commit: "a39c6393d91083e43f8dacca01aa8a8c3e38eb4a"
executed_at: "2026-08-27T11:27:06+08:00"
environment: "macOS 26.6.1 (Darwin 25.6.0 x86_64) / Python 3.11.15 / uv 0.12.6 / uv.lock sha256:14df51cee897e58e68b5608e245bd1a7… / pydantic 2.13.4 / pydantic-settings 2.15.0 / pytest 9.1.1 / typer 0.27.1 / ruff 0.16.4 / mypy 1.20.2"
provider_model: null
config_fingerprint: "sha256:c4604efae52f0027d1d345af68eaa903242fc8c7fe4169fb4469967f3dcff48f"
dataset_version: null
eval_fingerprint: null
commands:
  - cmd: "uv run ruff format --check ."
    result: "28 files already formatted"
  - cmd: "uv run ruff check ."
    result: "All checks passed!"
  - cmd: "uv run mypy src/oria"
    result: "Success: no issues found in 20 source files"
  - cmd: "uv run pytest -m 'not live and not enterprise and not performance'"
    result: "24 passed"
  - cmd: "uv run pytest -m 'unit or contract'"
    result: "15 passed, 9 deselected"
  - cmd: "uv run pytest -m security"
    result: "9 passed, 15 deselected"
  - cmd: "uv build"
    result: "Successfully built dist/oria-0.1.0.tar.gz 与 dist/oria-0.1.0-py3-none-any.whl"
  - cmd: "uv run oria --version"
    result: "0.1.0"
  - cmd: "uv run oria config doctor --output json"
    result: "退出码 0，输出 ok=true，凭证仅以 credential_configured 布尔投影出现"
  - cmd: "uv run python -c 'import oria.core.types'（及 core.context / core.runtime / core.protocols / permission.local / ingress.local / config / cli）"
    result: "8 个模块全部 import OK（修复 JsonValue 缺陷后）"
artifacts:
  - "src/oria/core/{types,protocols,context,runtime,registry}.py"
  - "src/oria/config/{models,resolve}.py"
  - "src/oria/permission/local.py"
  - "src/oria/ingress/local.py"
  - "src/oria/domain/services.py"
  - "src/oria/cli.py（config doctor 子命令）"
  - "tests/contract/{test_config_resolution,test_runtime_lifecycle,test_context_isolation}.py"
  - "tests/security/{test_config_boundaries,test_runtime_boundaries}.py"
  - "dist/oria-0.1.0-py3-none-any.whl、dist/oria-0.1.0.tar.gz"
evidence_refs:
  - "reports/verification/v0.1/20260827T084858+0800/summary.md（V0.1-T01 工程基线）"
assertions:
  - id: "V01-CFG-01"
    covered: true
    note: "community+demo 无 Key 解析成功，实测选中 mock LLM(api_dialect=mock, model=mock-demo)、fixture embedder、sqlite platform/biz、chroma vector、memory cache、local object；fingerprint 匹配 ^sha256:[0-9a-f]{64}$；credential_configured=false"
  - id: "V01-CFG-02"
    covered: true
    note: "community+standard 选 deepseek 且无 Key 时抛 ConfigResolutionError（缺 env 报 DEEPSEEK_API_KEY、显式 api_key:null 报 requires an API key）；并以『提供 Key 后成功解析为 provider=deepseek』作正向对照，证明系统不是静默降级为 mock"
  - id: "V01-CFG-03"
    covered: true
    note: "production+demo、production+MockLLM、production+FixtureEmbedder 三种组合分别被拒，断言精确报错文案"
  - id: "V01-CFG-04"
    covered: true
    note: "ResolvedRuntimeConfig 及嵌套模型赋值抛 ValidationError（frozen）；混合 ${} 引用/未知 profile/非映射 YAML 根三类来源错误被拒；两个不同 API Key 得到相同 fingerprint、换 provider 则 fingerprint 变化，证明 secret 不参与指纹；明文不出现在 fingerprint、public_summary()、repr、str 中"
  - id: "V01-CFG-05"
    covered: true
    note: "production 相对 data_dir 被拒；解析后的 data_dir 与全部 data_paths 位于注入的 tmp_path 之下且不位于 Path.home() 之下"
  - id: "V01-LIFE-01"
    covered: true
    note: "注入 5 个 resource_factory 令第 4 个失败：异常向外传播，created==[alpha,beta,gamma]，closed==[gamma,beta,alpha]==reversed(created)，每个资源恰关闭一次，失败项与其后未创建项均不出现在 created/closed"
  - id: "V01-LIFE-02"
    covered: true
    note: "ready=True 后 exit_stack.sealed=True/closed=False；运行期 enter_async_context 抛 LifecycleSealedError 且资源未被登记；tools/guardrails/nodes/agents/ingress/notifier 六个 registry 均 sealed 且 register 抛 RegistrySealedError；节点局部 AsyncExitStack 可自行关闭且不触及进程级资源；aclose 后进程资源才关闭、ready=False"
  - id: "V01-CTX-01"
    covered: true
    note: "asyncio.gather 并发两个 tenant/run Context，每轮 await asyncio.sleep(0) 强制让出事件循环，25 轮交错；断言事件序列中同时存在 (a,b) 与 (b,a) 相邻对以证明真实交错（顺序执行不可能产生 b→a）；两侧全部读数均等于自身身份且不等于对方身份；Context 为 frozen dataclass，赋值抛 FrozenInstanceError；A 侧 run 拆除后 B 侧仍读到 runtime.ready=True 且 process_closed 为空"
  - id: "V01-CTX-01（安全侧）"
    covered: true
    note: "LocalPolicyEngine 对 actor/ctx 主体不一致、resource.tenant_id 跨租户、action 不在本地只读集合三类请求分别拒绝并给出精确 reason；合法请求 allow=True、policy_version=local-v1、constraints={tenant_id: local-community}"
  - id: "V01-LOG-01"
    covered: true
    note: "InboundRequest.raw_body 因 exclude=True 不出现在 model_dump()/model_dump_json()，因 repr=False 不出现在 repr()（同时构成『raw body 只用于验签、不持久化』的证据）；ReasoningDelta.text（思维链）不入 repr；SecretValue 明文不入 repr/str；ChatResult.raw_response 不入 repr；config doctor --output json 在注入真实格式 Key 时输出不含该明文"
  - id: "raw body 只用于验签、不得持久化"
    covered: true
    note: "见 V01-LOG-01；另 InboundRequest 强制 timezone-aware received_at，naive datetime 抛 ValidationError；非 UTF-8 body 由 ingress 拒绝"
  - id: "tenant / roles 不可由自由 CLI 参数冒充"
    covered: true
    note: "config doctor --help 不含 --tenant/--roles/--subject/--actor；local_operator()/local_cli_executor() 返回固定 tenant_id=local-community、固定 subject_id、authn_method=trusted-local-profile；伪造 Principal 走 authorize 被拒（reason=principal is not the trusted community identity）；ingress 忽略请求头中的 x-spoofed-tenant/x-spoofed-roles 并以常量映射身份；request_id 不匹配或 executor 非可信主体时抛 IngressVerificationError；InboundMessage(verified=False) 在类型层面即抛 ValidationError"
  - id: "无写 Home / 无 import-time client"
    covered: "partial"
    note: "已断言解析出的全部路径不位于 Path.home() 之下，且测试仅写入 tmp_path；『import 时不创建 client』尚无专门断言，目前仅由 8 个模块 import 成功且无副作用间接支持"
result: "passed"
blocked_by: []
known_limits:
  - "验证等级仅为 F（Fixture）。全程使用 MockLLM 与 FixtureEmbedder，未调用任何真实 Provider、未下载或推理任何真实 Embedding 模型、未接入真实数据库或企业系统。不得据此声称真实模型或企业接入已验证。"
  - "本任务不含 V0.1-T03 及以后的内容：无 migration、无 Repository/EligibilityPolicy、无 RAG/Retriever、无 Tool、无 StateGraph/Checkpoint、无 oria data init / oria demo。"
  - "V0.1 Core Gate 未通过，Live 卡未运行。本报告只覆盖 T02。"
  - "过程事实：本任务由多个执行体接续完成。运行时骨架源码由 Codex(gpt-5.6-sol) 产出后因额度中断；contract/security 测试由 OpenCode(zhipuai glm-5.3) 分两批补齐；JsonValue 缺陷修复、失效回退代码清理与全部命令的独立复跑由 Hermes 执行。所有结论均以本机实际命令输出为依据。"
  - "遗留观察 1：resolve.py 的 _validate_matrix 中 production 专属的 MockLLM/FixtureEmbedder 拒绝分支，在 environment 非 test 时会先被 standard-profile 通用检查拦截并报不同文案。两条路径都是 fail-closed，但错误归因路径与设计意图不完全一致，测试为精确命中该分支使用了 environment=test。建议 T03+ 复核检查顺序。"
  - "遗留观察 2：配置来源冲突的实际语义是『YAML/env/CLI 之间按优先级静默覆盖』，被拒绝的是混合 ${} 引用、未知 profile 名、非映射 YAML 根这三类。测试按实现真实行为断言，未按『任何来源冲突都报错』的字面理解断言。"
  - "遗留观察 3：V01-LIFE-02 对 exit_stack 是否封存的断言经由 runtime._exit_stack 私有属性完成，因当前无公开访问器；Context 自身未提供节点级局部资源栈设施，测试以标准库 AsyncExitStack 模拟节点内局部资源。"
  - "遗留观察 4：V01-CTX-01 的『互不可见』除身份值比对外，还包含一条基于 dir(ctx) 的结构性断言，属表面检查，不能证明底层无共享可变状态。"
  - "遗留观察 5：tenant/roles 冒充用例断言 config doctor --help 全文不含 tenant/roles/subject/actor 等词，属较强断言；将来若在帮助文本中出现这些词（即便非身份参数）会导致该用例失败。"
---

# V0.1-T02 验证报告

## 结论

`V0.1-T02` 产物完成，任务对应的静态检查与 Fixture 等级测试在本机实际通过，证据为上方 `commands` 中的真实输出。按路线 §1.4「任务完成」口径判定为 **完成**；按 §1.2 验证等级仅为 **F**，不构成对真实模型、真实 Embedding、真实数据库或企业系统的任何验证。

当前门禁状态：**V0.1 Core Gate 未通过**（尚缺 T03–T09），**Live 卡未运行**。下一步可并行实施 `V0.1-T03` 与 `V0.1-T04`。

## 交付产物

| 产物 | 位置 |
| --- | --- |
| Pydantic 值类型、Protocol（含 `InboundRequest`/`IngressContext`/`InboundMessage`） | `src/oria/core/types.py`、`src/oria/core/protocols.py` |
| 进程级 `RuntimeServices` + 每次执行 `Context`、teardown 封存 | `src/oria/core/context.py`（`SealedAsyncExitStack`、`LifecycleSealedError`） |
| `build_runtime()` + AsyncExitStack 骨架 | `src/oria/core/runtime.py` |
| ServiceRegistry 封存 | `src/oria/core/registry.py`（`RegistrySealedError`） |
| 配置模型、只读 `ResolvedRuntimeConfig`、脱敏 fingerprint | `src/oria/config/models.py`、`src/oria/config/resolve.py` |
| `oria config doctor`（human + `--output json`） | `src/oria/cli.py` |
| 本地 PolicyEngine、`local-community`/`local-operator` 主体规则 | `src/oria/permission/local.py` |
| 入站 seam（CLI ingress、固定身份映射） | `src/oria/ingress/local.py` |
| Domain Service seam | `src/oria/domain/services.py` |
| 固定数据目录布局 | `RuntimeDataPaths`（`sqlite/platform.db`、`sqlite/business.db`、`chroma`、`objects`、`reports_tmp`） |

## 本次修复的源码缺陷

`src/oria/core/types.py` 中 `JsonValue` 原为隐式递归类型别名：

```python
JsonValue: TypeAlias = str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"] | None
```

在锁定的 pydantic 2.13.4 / Python 3.11 下，**import `oria.core.types` 即抛 `RecursionError`**，连带 `oria.core.context`、`oria.core.runtime`、`oria.core.protocols`、`oria.permission.local`、`oria.ingress.local` 全部不可导入，即整个 T02 运行时骨架不可运行。

该缺陷未被 `ruff` 与 `mypy` 捕获，因为静态检查不执行 import；也未被首批仅覆盖 `oria.config` 的测试触发，`oria config doctor` 恰好可用是因为 config/cli 不引用 `JsonValue`。缺陷由第二批生命周期/上下文测试首次暴露。

修复方式为改用 pydantic 官方递归 JSON 类型（Python 3.11 不支持 PEP 695 `type` 语句，故不采用该写法；`typing_extensions.TypeAliasType` 亦可行但需新增直接依赖）：

```python
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, field_validator, model_validator
```

修复后 8 个模块全部可 import，`make lint` 与 24 个测试全部通过。此事件同时印证路线 §1.4 的口径：静态检查全绿且代码存在，并不等于任务完成。

## 未覆盖与后续项

- `无 import-time client` 仅间接支持，建议后续补专门断言。
- 上方 `known_limits` 中 5 条遗留观察项均为如实记录，未在本任务修改源码予以调和。
- Live/Enterprise/性能测试本次未运行，不产生任何卡片。
