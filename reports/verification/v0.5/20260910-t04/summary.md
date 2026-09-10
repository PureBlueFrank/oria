# V0.5-T04 验证卡

- 日期：2026-09-10
- 级别：Fixture / Community / Security
- 结果：passed
- 网络：未运行真实网络、Live、Enterprise 或 Performance

## 范围

- 新增严格、可 JSON 序列化的 `SupervisorHandoff` / `SubagentResult` 和完整默认的 checkpoint state。
- 实现确定性 tool-based 短语路由，将请求分派到招商与归因两个专职 Subagent。
- 两个 Subagent 均复用 `build_research_graph` 和现有 `ResearchSpec`，未复制 model/tool/validate 循环。
- 将工具可见性限制为 spec allowlist 与原 `ctx.policy` 授权的交集，保留执行前 Guardrail 重新鉴权。
- 固定最大 handoff 上限为 2，显式回收 failed/waiting termination，并将 supervisor 图注册到 `ctx.agents`。

## 命令与结果

1. `make lint`
   - `ruff format --check`：322 files already formatted
   - `ruff check`：All checks passed
   - `mypy src/oria`：Success，160 source files
2. `uv run pytest -m "not live and not enterprise and not performance" -q`
   - 963 passed, 1 deselected, 4 warnings, 379.37s
   - warning 为既有 SQLite migration 的 SQLAlchemy PRAGMA foreign-key 解析警告。
3. `uv run pytest -m security -q`
   - 117 passed, 847 deselected, 49.29s

## 失败历史与修正

首次 `make lint` 在格式检查阶段发现 `src/oria/agent/supervisor.py` 和 `tests/contract/test_v05_supervisor.py` 各一处可机械格式化差异。对这两个文件执行 Ruff formatter 后，重跑完整 lint 门禁通过。

新增定向 CT/IT/SEC 首次收集时，pytest 拒绝使用保留 fixture 名 `request` 作为 parametrize 参数；重命名为 `query` 后，定向用例 10 passed。随后执行全部必需门禁，得到上述最终结果。

## 边界与决策

- 当前失败回收选择终止/上报，不在招商与归因之间改派，因为两个专职 Subagent 的输入、工具与结果 schema 不可互换。
- Community `build_runtime` 保持现有工具注册语义，因此默认可完整执行招商 Subagent；归因工具未挂载时 fail closed 为 `subagent_tools_unavailable`。这不改变 V0.4 归因专用 assembly 的工具注册语义。
- 本卡只证明多 Agent 控制流、权限和恢复契约，不声明多 Agent 比单 Agent 质量更高。等额预算的 Live 对照属于 V0.5-T05/T07。
- 无数据库 migration、无新运行时依赖、无远程写入。
