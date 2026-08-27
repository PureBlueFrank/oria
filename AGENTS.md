# Repository Guidelines

## 项目结构与模块组织

仓库已建立 Python 3.11 `src` layout：业务代码放在 `src/oria/`，测试放在 `tests/`，静态资源放在 `assets/`，设计与架构说明放在 `docs/`。测试目录应尽量镜像源码结构，例如 `src/oria/agents/router.py` 对应 `tests/agents/test_router.py`。不要提交 `.DS_Store`、缓存、构建产物或本地 IDE 配置。

## 构建、测试与本地开发

使用锁定的 `uv 0.12.6 + Python 3.11`，不得绕过 `uv.lock` 在个人环境直接安装依赖。当前可用入口：

- `make sync`：按锁文件同步核心与开发依赖。
- `make lint`：运行 Ruff 格式检查、Lint 和 mypy。
- `make test`：运行不含 Live/Enterprise/Performance 的本地测试。
- `make build`：构建 wheel 与 sdist。
- `make smoke`：验证 `oria` CLI 入口。

更细的单项命令见 `README.md` 和 `docs/Oria详细执行路线.md` §2.1。Live/Enterprise 必须显式提供运行开关与非空的已知 target，不得把默认未运行记为通过。

## 编码风格与命名约定

统一使用 UTF-8、LF 换行和文件末尾换行。遵循所选语言的官方风格，并将格式化、Lint 规则纳入仓库配置。若采用 Python，使用 4 空格缩进及 `snake_case`；若采用 JavaScript/TypeScript，使用 2 空格缩进、变量与函数用 `camelCase`、类型与组件用 `PascalCase`。模块应职责单一，避免为未确认的需求增加抽象或配置项。

## 测试要求

每项行为变更都应包含相应测试；缺陷修复应先添加能够复现问题的回归用例。测试名称描述可观察行为，例如 `test_rejects_expired_token` 或 `router.test.ts`。优先覆盖核心 Agent 编排、工具调用、权限边界和失败恢复。目前没有覆盖率门槛；pytest marker 固定为 `unit/contract/integration/live/enterprise/slow/security/recovery/performance`。PR Core 默认只运行无外部依赖的测试，Live/Enterprise 结果必须独立记录。

## Oria 执行前置检查

开始实现任务前，必须阅读 `Oria架构设计.md` 和 `docs/Oria详细执行路线.md`，确认版本、任务 ID、前置门禁、真实验证场景和测试用例。Fixture、社区真实组件、公开模型 Live、企业 Adapter 的结果必须分开记录；Mock 通过不得写成真实模型或企业接入通过。完成后保存脱敏验证证据并更新路线状态，未执行或失败项如实标记。

## 提交与 Pull Request

当前目录尚无可分析的 Git 历史。后续采用 Conventional Commits，例如 `feat: add agent registry`、`fix: handle tool timeout`。每个提交聚焦一个目的。PR 应说明背景、主要改动、验证命令和已知风险，并关联相关 Issue；涉及界面变更时附截图，涉及配置变更时同步更新示例配置与文档。

## 安全与配置

不得提交密钥、令牌、真实客户数据或 `.env` 文件。新增环境变量时提供脱敏的 `.env.example`，并说明默认值、用途及最小权限要求。日志中避免记录提示词原文、凭证和个人信息。
