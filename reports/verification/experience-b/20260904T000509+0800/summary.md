# 体验优化任务 B 收尾验证报告

- 验证时间：2026-09-04 00:05:09 +08:00
- 基线 commit：`f8aecd8`
- 验证等级：Fixture / Community
- 结果：`passed`

## 范围

- 将本地 Workflow 手册中 14 处 JSON 中断字段术语改为 human 输出里的“当前阶段”、“下一步命令”和终态文案。
- 表格按终端显示宽度自适应，优先分配长文本列；非 TTY 回退为 160 列，长文本列最少 60 显示宽度。
- 超出列宽的单元格换行展示，不再使用 `…` 截断；保留中文双宽度计算与补齐逻辑。
- `oria demo --output human` 复用同一 renderer，没有独立截断逻辑。

## 实测

使用隔离目录 `/tmp/guide-verify-<random>` 和 `--output human` 完整执行：

1. 初始化并启动 Workflow。
2. 从“下一步命令”复制 `--approval-id` 完成招商发布审批。
3. 注入报名并关窗。
4. 从每轮“下一步命令”复制新的 `--confirmation-task-id`，完成商家 → 销售 → 销售经理三轮确认。
5. 注入选品决定与完成事件。
6. 从“下一步命令”复制第二个 `--approval-id`，完成 C 端发布审批并进入“通知商家并闭环”终态。

实测阶段文案：

- `招商发布审批`
- `报名与商品圈选`
- `动态业务确认`（当前第 1/3、2/3、3/3 级）
- `提交并等待招后选品`
- `C 端发布审批`
- `通知商家并闭环`，终态文案为“流程已完成: C 端投放与商家通知已闭环。”

所有 Workflow human 输出共检查 422 条表格行，最大显示宽度为 160，未发现 `…`。另行实跑 `oria demo --output human`，未发现 `…`。

## 门禁

- `uv run pytest tests/unit/test_workflow_presentation.py tests/unit/test_cli.py -q`：`18 passed in 1.97s`（最终重跑）。
- `make lint`：Ruff format check 通过，Ruff check 通过，mypy 检查 132 个源文件无问题。
- JSON 实跑：`workflow start --output json` 顶层字段仍为 `detail/interrupts/ok/status/thread_id`，首个 interrupt 仍为 `launch_approval`。
- JSON 单元回归：`LocalWorkflowResult.view` 仍不进入 serialization schema。

## 边界

本次未修改 Graph、业务逻辑或 JSON schema。未运行 Live、Enterprise、Performance、真实网络或真实企业 Adapter 验证。
