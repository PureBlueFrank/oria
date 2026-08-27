# Oria

Oria 是面向招商活动编排的企业级 AI Agent 平台。项目目前正按 `V0.1-T01`
建立可复现的 Python 工程基线，业务 Workflow 尚未实现。

## 开发环境

- Python 3.11
- uv 0.12.6

```bash
uv sync --locked --group dev
uv run oria --version
```

## 当前可用检查

```bash
make lint
make test
make build
make smoke
```

Live Provider 和 Enterprise 测试默认不运行。显式运行时必须同时提供开关与非空目标列表，
例如 `ORIA_RUN_LIVE=1 ORIA_LIVE_TARGETS=deepseek uv run pytest -m live`。

架构与分阶段执行要求见 `Oria架构设计.md` 和 `docs/Oria详细执行路线.md`。
