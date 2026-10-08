# V0.6-T02 审批服务启动装配

日期：2026-10-08；基于 HEAD `857dc3e` 的本地工作区，保留此前未提交增量。**T02 仍进行中**。

## 本轮交付

- 服务端 JSON 新增可选 `approval_runtime_config`，显式 YAML 解析，不加载个人默认配置或环境覆盖。当前仅支持 Mock/Fixture 与双 SQLite。
- lifespan 装配现有 RuntimeServices、真实 Policy 和 SQLiteApprovalRepository；审批请求可通过既有 JWT 接口持久化决定。身份撤销即时生效，新增用户/角色调整需重启 Policy 快照。
- 数据库启动检查使用只读连接，核对两条完整 schema/head；缺库或旧版本时终止，不创建数据库或运行迁移。启动后运行时按配置初始化 saver、索引和对象存储资源。
- 正常关闭及启动失败释放资源；未配置时保持 503，飞书 receiver 和 Job 不因此自动开启。

## 验证

- 定向：26 passed，1 warning，7.68 秒。真实本地 Runtime/Policy/SQLite + RSA 签名和 Mock JWKS HTTP；覆盖决定持久化、撤权、缺库、旧版本、启动失败释放客户端及不接受环境覆盖。没有真实 OIDC/飞书网络或模型调用。
- 静态检查：354 文件格式、Ruff、177 源文件 mypy 通过。
- 首轮测试 3 failed：新测试使用 Python 模式序列化含 frozenset 的身份，改为 JSON 模式后通过；首轮格式检查问题已修正。保留此失败历史。
- 初次构建因沙箱 DNS 获取 hatchling 失败，经批准联网复验成功；sdist/wheel 构建通过。
- 全量非 Live/Enterprise/Performance 回归：**1046 passed、3 deselected、5 warnings，302.67 秒**。
- 当前 wheel 重新安装到既有独立验证环境，资源、迁移版本、全新临时数据初始化/重复初始化、配置 schema 与只读启动门禁均通过；依赖复用项目锁定环境，不宣称空缓存部署验证。发行物排除缓存、本地文档链接和 `git diff --check` 通过。
- 结构化结果见 [checks.json](checks.json)；原始日志位于本地 `.artifacts/verification/20261008-t02-lifecycle/`，不提交环境路径日志。

## 剩余工作

飞书 action_ref 持久绑定与按钮受理、Job/Graph 恢复、真实飞书链路尚未交付。PostgreSQL 企业验证仍未运行，T01 原环境阻塞不变；T07 Live 29/80 不变。未运行 Live/Enterprise/Performance，未迁移用户数据，未提交/推送或触发 GitHub Actions。

使用见 [API 指南](../../../../docs/guides/api-foundation.md)，前置验证见 [审批接口与并发保护](../20261006-t02-approvals/summary.md)。
