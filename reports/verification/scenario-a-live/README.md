# 场景 A Live 开发验证（2026-09-09）

环境：uv 0.12.6，Python 3.11.15。无新增依赖，未调用 Live Provider、Enterprise 或 Performance。此次离线验证不能证明 Moonshot strict JSON schema 兼容性。

实际运行历史：

1. `uv run pytest tests/contract/test_config_resolution.py -q`：22 passed in 0.18s。
2. 首次 `uv run mypy src/oria`：4 errors in 1 file，均为 computed field/property 装饰器类型检查问题；改为显式报告字段后解决。
3. 首次 `uv run pytest tests/unit/test_scenario_a_live.py -q`：3 failed, 6 passed in 4.01s。原因是 standard + fixture embedding 在 development 环境被现有配置矩阵拒绝；与场景 B 一致显式选择 test 环境后解决。
4. 修正后的 `uv run pytest tests/unit/test_scenario_a_live.py -q`：9 passed in 4.02s。
5. `make lint` 完整输出见 `lint.txt`。
6. 指定社区 suite 的完整输出见 `community-pytest.txt`。
7. `uv run python scripts/run_scenario_a_live.py --help`：退出码 0，展示必填 target、默认 output 和可选 max-new-case-runs。
8. `git diff --check`：退出码 0。

保留上述失败历史；未通过重复运行挑选结果。未读取或修改用户禁止读取的详细路线与架构资料；本次范围内未宣称版本退出门禁或 Live 验收完成。

最终结果：`make lint` 退出码 0（297 files already formatted；All checks passed；mypy 检查 147 个源文件无问题）。社区 suite 退出码 0：`897 passed, 1 deselected, 4 warnings in 271.72s (0:04:31)`。4 条 warning 为迁移测试的 SQLAlchemy 外键反射提示，原文保留于输出文件。
