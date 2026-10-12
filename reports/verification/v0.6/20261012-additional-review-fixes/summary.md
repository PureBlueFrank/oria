# Oria 全工程检查新增问题修复（2026-10-12）

三项新增问题已修复：Codex Provider 会话隔离、Memory 搜索缓存生命周期及 JSON 尾逗号修复的字符串保真。此前四项审查修复及工作区其他已有改动均保留。

## 修复与回归

| 问题 | 最终行为 | 新增回归 |
| --- | --- | --- |
| [P1] 不同租户相同线程 ID 复用模型上下文 | 远端模型线程和消息计数均按 `(tenant_id, actor.subject_id, thread_id)` 隔离；同一主体的同一线程继续复用后续输入 | 2 项：跨租户、同租户不同主体；独立首轮完整发送，同一主体 follow-up 正确增量发送 |
| [P2] 缓存返回过期或另一实例删除的记忆 | 仅缓存 tenant/subject/query 对应的查询向量，每次重新读取数据库有效记录并排序；不缓存 MemoryItem 或结果列表 | 4 项：TTL 边界与边界之后，相同查询看到另一 RuntimeServices 删除，以及空查询结果后看到另一实例新增 |
| [P2] 尾逗号修复静默改写字符串 | 扫描器识别字符串、引号和反斜杠转义，仅移除字符串外的尾逗号；保留原 JSON 解码与 Schema 校验 | 7 项：逗号加花括号/方括号、空白、转义引号/反斜杠、Unicode、嵌套数组及含标点的键名 |

修改源码：`src/oria/providers/codex_app_server.py`、`src/oria/providers/structured.py`、`src/oria/memory/persistent.py`。Memory 每次搜索增加当前有效记录的数据库读取，复用查询向量以避免重复嵌入计算；没有引入跨实例结果缓存或新的持久化 schema。

修复前先运行新增回归：**13 failed / 12 deselected，5.95 秒**，证明场景可复现，见 [regression-before.log](regression-before.log)。最终定向套件 **29 passed，10.01 秒**，覆盖上述回归及既有 Provider envelope、非法 JSON/Schema、Memory 权限、删除审计、投毒边界和 CLI，见 [targeted-final.log](targeted-final.log)。

Provider 测试调用真实 adapter，仅替换模型线程/传输层，不启动 Codex app-server 或调用真实模型；Memory 用真实 RuntimeServices、SQLite、Policy 与 FixtureEmbedder；JSON 测试调用真实解析函数。定向与全量结果有重叠，不相加。

## 验证状态

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 格式 / Lint / 类型 | 358 个 Python 文件格式通过；Ruff 通过；179 个源码文件 mypy 通过 | [lint-final.log](lint-final.log) |
| 全量本地回归 | 1086 passed / 3 deselected / 5 warnings，318.29 秒；包含 Contract/Integration/Security/Recovery | [community-final.log](community-final.log) |
| wheel / sdist | 离线构建通过，复用本机已缓存且满足 pyproject 范围的 Hatchling 1.32.4，无依赖安装 | [build-final.log](build-final.log) |
| wheel 内容 | 三份修复源码与工作区逐字节一致 | [wheel-content-final.log](wheel-content-final.log) |
| CLI smoke | `oria --version` 返回 0.1.0 | [smoke-final.log](smoke-final.log) |

本轮源码及测试指纹见 [source-snapshot.json](source-snapshot.json)。

五条 warning 与上一轮相同：Starlette/httpx 弃用提示及既有迁移外键反射提示；没有测试失败。最终文档链接、源码指纹及 `git diff --check` 均通过。

使用现有 uv 0.12.6 / Python 3.11 锁定环境，关闭网络及依赖同步。产物放在 Git 忽略的 `.artifacts/additional-review-fixes-20261012/`。没有修改锁文件、迁移或环境变量，没有操作用户持久数据、提交或推送。

ADR-001/012、Memory 保留与删除说明及威胁模型同步到最终行为；原始检查报告保留失败/发现历史并链接本卡。Live Provider、真实飞书、PostgreSQL/Enterprise、Performance 本轮未运行；V0.6-T02 仍在进行中，既有生产装配和 Job/wait/Graph 恢复门禁不因本轮修复而关闭。

提交整理说明：两轮修复中三份 pytest 失败日志仅移除行尾空格和制表符，失败内容、执行结果及失败历史均保留。
