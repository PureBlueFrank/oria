# V0.6-T02 飞书审批受理启动装配

日期：2026-10-16；基于 `c57d59e08c7a09969214a130aa67c0e191bab9ea` 的本地工作区。任务依赖为 V0.5-Core / V0.5-T03，均已完成。验证等级为 Fixture / Community / Security / Recovery。**本增量完成，T02 仍进行中，T03 依赖未满足。**

## 交付与边界

服务端 JSON 新增严格布尔 `feishu_approval_receiver`，默认关闭。启用时须同时提供 `feishu` 和 `approval_runtime_config`；缺配置或使用字符串代替布尔值时，在创建数据目录前拒绝启动。数据库预初始化、只读完整 schema/head 检查、不继承个人配置或环境覆盖、Mock/Fixture 与双 SQLite 限制均沿用现有契约。没有新增环境变量、依赖或迁移，没有操作用户持久数据。

`APIServices` 在同一个应用 lifespan 中装配审批 HTTP 与事务化飞书 receiver，共用 RuntimeServices、Policy 和 SQLiteApprovalRepository；禁止与独立注入的审批/factory/receiver 混合。保留原有库注入接口及审批专用 context manager。receiver 复用原有 ApprovalService、当前身份目录、职责分离、完整快照 CAS、审批审计/outbox 和 receipt/replay 原子事务；没有修改决定或恢复权限。

受理仅接受已持久绑定的卡片 action。受信内部服务仍负责签发 `ApprovalCardBindings`，启动不创建审批、不签发引用、不发送卡片。文本消息缺少 Job 处理器时返回 503，不创建 receipt/replay。正常关闭及受理装配失败都会释放运行时、数据库和 HTTP 客户端。

## 验证结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 生命周期与配置定向 | 12 passed / 1 warning，14.96 秒；包含两个独立 API 进程、真实回环 HTTP、重启和撤权 | [lifecycle-final.log](lifecycle-final.log) |
| HTTP/卡片共享审批 | 1 passed / 1 warning，3.44 秒；真实 RSA/JWT + Mock JWKS，卡片批准后 HTTP 读到新决定，旧版本不能覆盖 | [shared-services.log](shared-services.log) |
| 格式 / Lint / 类型 | 359 文件格式、Ruff、179 源文件 mypy 均通过 | [lint-final.log](lint-final.log) |
| 全量本地回归 | 1094 passed / 3 deselected / 5 warnings，352.86 秒 | [community-final.log](community-final.log) |
| wheel / sdist | 离线构建通过，复用项目现有构建依赖缓存 | [build-final.log](build-final.log) |
| 发行物内容 | 两份修改源码与 wheel 字节一致；wheel 227 项、sdist 727 项，均不包含 `.artifacts` 缓存 | [package-content.log](package-content.log) |
| CLI smoke | 通过，版本返回 0.1.0 | [smoke-final.log](smoke-final.log) |

新增 8 项测试。两次定向结果不与全量套件相加；全量包含相同场景。定向唯一 warning 为既有 Starlette/httpx 弃用提示；全量五条 warning 与 10 月 12 日相同，为该弃用提示和既有迁移外键反射提示，没有测试失败。构建工具提示缓存目录位于源目录内，已直接检查两份发行物确认未包含缓存。产物仅存于忽略目录 `.artifacts/receiver-lifecycle-20261016/`。

独立进程用例启动两个先后运行的真实 uvicorn API 进程，通过 127.0.0.1 临时端口发送本地签名飞书载荷，共用临时 SQLite 数据库。相同已签名回调重投不增加账本或决定；新签名投递只增加 replay 键，不新增决定、决定审计或 outbox；重启后依然如此；身份文件撤销后返回 401。此处只有单个 API 进程依次运行，不代表双 worker 或 lease/fencing 验证。

HTTP/卡片共享服务用例通过真实 RSA/JWT 校验和合成 JWKS，访问同一临时审批；卡片批准后 HTTP 详情立即显示 approved，旧 revision 的 reject 返回 409。启动失败测试在 receiver 构造阶段注入异常，并确认运行时、数据库和 HTTP 客户端均关闭。其余用例覆盖默认禁用、challenge、缺配置、严格布尔和禁止混合注入。

## 环境与命令

uv 0.12.6、Python 3.11.15、现有锁定环境；关闭网络依赖下载和依赖同步。锁文件指纹、实际包版本和源码/测试指纹见 [source-snapshot.json](source-snapshot.json)。合成数据沿用既有飞书签名 helper、服务端身份 fixture 和临时审批 seed；没有真实用户、企业数据、模型或外部网络调用。

测试与静态检查使用 `UV_CACHE_DIR=/private/tmp/oria-uv-cache UV_OFFLINE=1 UV_NO_SYNC=1`，构建使用仓库既有 `.artifacts/uv-cache`，不安装个人环境依赖或改动锁文件。

```bash
uv run pytest -q tests/integration/test_v06_feishu_server_lifecycle.py tests/integration/test_v06_api_lifecycle.py
uv run pytest -q tests/integration/test_v06_feishu_server_lifecycle.py::test_http_and_card_services_share_decisions_and_reject_stale_revision
make lint
make test
uv build --out-dir .artifacts/receiver-lifecycle-20261016
make smoke
```

轮次与失败历史见 [attempts.md](attempts.md)，机器结果见 [checks.json](checks.json)，源码指纹与本地引用复核见 [consistency.log](consistency.log)。已审查源码差异，`git diff --check` 通过。未提交、推送或触发 GitHub Actions，不声明远端 CI 通过。

## 未完成与后续顺序

继续 V0.6-T02：生产卡片投递/更新装配、带理由拒绝、飞书详情/翻页与真实通道验收。T02 收口前不进入 T03 主线；Job/wait/Graph 恢复仍为后续任务。真实 PostgreSQL、真实飞书、Live Provider、Enterprise 和 Performance 本轮均未运行；T01 原 PostgreSQL blocked 卡和 V0.5-T07 Live 29/80 暂停状态不变。

本轮不重跑或改写历史 Live 卡，不宣称企业接入或完整 Durable Job 已完成。前置事务、并发和失败恢复证据复用 [10 月 11 日事务受理验证](../20261011-t02-transactional-acceptance/summary.md) 与 [10 月 12 日审查修复](../20261012-review-fixes/summary.md)，不是本轮独立重验的外部事实。
