# 许可证与版本边界

Oria 采用 **Apache 2.0 开源社区版 + 闭源商业企业版** 的产品模式。商业企业版在开源核心之上提供独立交付的专有扩展。

## 开源社区版

- 本仓库的 Oria 原创代码、文档、示例及合成测试资源采用根目录的 [Apache License 2.0](../LICENSE)，明确标注其他许可的第三方内容除外。
- 在遵守许可证条款的前提下，可以商用、修改、再分发及用于闭源产品；使用本仓库公开能力无需购买 Oria 企业版许可。再分发须遵守许可证第 4 条，包括附带许可证副本、标明修改、保留适用声明，以及在存在上游 NOTICE 时按要求保留其内容。
- 本仓库已经公开的 PostgreSQL、权限、审批、审计和企业集成实现仍属于开源范围，其许可证不会因使用场景是企业生产环境而变化。

## 闭源商业企业版

- 企业专有模块在独立私有仓库或发行包中维护，通过现有 Protocol、Adapter 和插件契约接入开源核心，并随交付物附带单独的商业许可。
- 商业许可约束企业专有模块及约定的商业服务，不替代随产品分发的 Apache 2.0 核心许可证，也不撤销已获授权的开源权利。
- 企业版的专有功能清单、部署范围、价格、支持和 SLA 由企业版交付说明及商业协议明确；本次定位不表示这些能力已实现或已经发布。

## 配置、验证与第三方内容

`edition=community|production` 和 `runtime_profile=demo|standard` 是当前代码的装配与运行配置，不是商业授权开关。`enterprise` 测试标记、E-like/E 验证等级和路线中的企业组件表示验证对象与环境，不表示相关公开代码属于闭源企业版。

第三方依赖、模型权重、SDK 和其他单独标注的素材继续适用各自许可证或服务条款；根目录 LICENSE 不替代这些条款。

许可证正文优先于本文摘要。参考 [Apache 官方许可证](https://www.apache.org/licenses/LICENSE-2.0)及[官方许可 FAQ](https://www.apache.org/foundation/license-faq.html)。

## English

Oria has an **Apache 2.0 open-source Community edition and a proprietary commercial Enterprise edition**. The Enterprise edition extends the open-source core with separately distributed proprietary modules.

### Community edition

- Original Oria code, documentation, examples, and synthetic test resources in this repository are licensed under the root [Apache License 2.0](../LICENSE), except third-party content explicitly covered by another license.
- Commercial use, modification, redistribution, and inclusion in proprietary products are permitted subject to the license. Using the public capabilities does not require purchasing an Oria Enterprise license. Redistribution must comply with section 4, including supplying the license, identifying modifications, preserving applicable notices, and handling upstream NOTICE content when present.
- PostgreSQL, authorization, approval, audit, and enterprise integrations published here remain open source, including when used in enterprise production environments.

### Proprietary Enterprise edition

- Proprietary modules are maintained in separate private repositories or packages, integrate through the existing Protocol, Adapter, and plugin contracts, and ship with a separate commercial license.
- Commercial terms cover those proprietary modules and agreed services. They do not replace the Apache 2.0 license for the distributed public core or revoke rights already granted under that license.
- Enterprise features, deployment scope, pricing, support, and SLAs are specified in Enterprise delivery documentation and commercial agreements. This positioning does not indicate that these capabilities have been implemented or released.

### Configuration, verification, and third-party content

`edition=community|production` and `runtime_profile=demo|standard` select assembly and runtime configuration, not commercial licensing. The `enterprise` test marker, E-like/E verification levels, and enterprise component targets describe verification environments, not proprietary feature boundaries.

Third-party dependencies, model weights, SDKs, and separately identified assets retain their own licenses or service terms. The root LICENSE does not replace those terms.

The license text takes precedence over this summary. See the [official Apache license](https://www.apache.org/licenses/LICENSE-2.0) and [licensing FAQ](https://www.apache.org/foundation/license-faq.html).
