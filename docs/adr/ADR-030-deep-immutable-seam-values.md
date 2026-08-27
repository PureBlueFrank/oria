# ADR-030：跨 seam 值类型采用校验时深度冻结

- 状态：已接受
- 日期：2026-08-27
- 决策者：Codex（V0.1-T02 remediation）
- 关联任务：V0.1-T02、V0.1-T03、V0.1-T04、V0.6-T03

## 背景

Pydantic `frozen=True` 只阻止模型字段重新赋值，不会冻结字段内部的 `dict`、`list`
或嵌套 JSON 容器。已经通过校验的 `ToolCall.args`、
`AuthorizationContext.attributes` 和 `Message.content` 因此仍能原地改变。这会使工具参数校验、
审批参数哈希、checkpoint 重放和 Provider 归一化观察到不同值，破坏“校验后稳定”的跨 seam 契约。

## 候选方案

1. 校验时深度冻结：JSON object 转为只读映射，JSON array 转为 tuple；对外 JSON 投影仍为
   object/array。优点是值对象自身在整个生命周期内稳定，消费者无需记住额外边界；代价是一次递归转换，
   且需要保证 Pydantic schema/序列化兼容。
2. 每个安全边界做复制和规范化：在 ToolExecutor、Provider、Checkpoint、审批等入口分别复制。
   单个入口实现简单，但同一个“已校验”对象在到达入口前仍可变化，未来每增加一个 seam 都容易漏掉。
3. 只约定“跨 seam 前规范化”：不改变值类型，由调用方在传递前主动规范化。成本最低，但契约无法由
   类型和测试强制执行，尤其不适合 T03 的哈希/审批绑定与 T06 的恢复语义。

## 决策

采用方案 1。所有继承 `ValueModel` 的跨 seam 值在 Pydantic 校验完成后递归冻结：

- mapping 复制为不可变 `FrozenDict`；
- JSON list 复制为不可变且保持 JSON array 序列化兼容的 `FrozenList`；显式 tuple 保持 tuple，
  set 复制为 frozenset；
- 嵌套 Pydantic 值模型保持其自身 frozen 契约；
- 原始调用方容器与模型内部容器不共享可变引用；
- `model_dump()`、`model_dump_json()` 和 JSON Schema 仍使用标准 JSON object/array；
- 所有浮点在同一递归遍历中检查有限性，NaN 和正负 Infinity 在校验期拒绝。

直接保存 provider 原始响应和 reasoning 文本仍允许内部代码通过显式方法读取，但它们属于内部字段，
默认公开投影和 repr 必须排除。

## 后果

- 正向影响：校验、授权、哈希、checkpoint 和序列化看到稳定值；一次实现覆盖未来跨 seam 模型。
- 代价与局限：构造值模型时有一次 O(n) 递归复制；调用方若需要修改，必须显式构造新值模型。
- 性能边界：值类型只承载有界控制面/模型上下文数据；超过内联上限的内容按架构契约转 ObjectStore，
  不用超大容器规避冻结。
- 迁移/回滚：对依赖原地修改模型容器的代码是有意的破坏性收紧；改为先构造普通容器，再创建新模型。

## 验证

- `ToolCall.args`、`AuthorizationContext.attributes`、`Message.content` 及嵌套容器原地修改均失败。
- 输入容器在模型创建后被修改，不影响模型值。
- 所有值模型 JSON Schema 可构建，判别联合可序列化/反序列化往返。
- NaN、Infinity、-Infinity 校验失败；有限浮点 JSON 往返保持值。
- reasoning/raw response 默认 repr 与 JSON 投影不泄漏，内部显式读取仍可用。

## 关联资料

- 架构主文档章节：§3.1 值类型、§3.2 reducer、§3.7 canonical args hash
- 详细执行任务：V0.1-T02；测试 V01-CFG-04、V01-LOG-01、V01-CTX-01
