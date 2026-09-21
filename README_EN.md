# Oria

[简体中文](README.md) | **English**

Oria is an open-source AI Agent engineering project for the full lifecycle of merchant recruitment campaigns.

A complete campaign spans requirement understanding, rule retrieval, merchant and product screening, campaign and coupon planning, multi-party approvals, enrollment and product collection, post-recruitment assortment selection, channel publishing, and result notification. Some work benefits from an LLM's ability to process unstructured information and perform open-ended analysis; eligibility rules, authorization boundaries, and business side effects must remain deterministic. Oria brings both into one executable, recoverable, and auditable system: the LLM handles understanding, exploration, and explanation, while deterministic policies, state machines, approvals, idempotency ledgers, and evidence validation control the final boundary.

## Highlights

| Design | What it provides |
| --- | --- |
| **Contract-based plugin core** | Protocols such as `LLMProvider`, Policy, Tool, Retriever, Memory, and Guardrail are assembled through registries and factories. Mock and real implementations change the assembly, not the core engine. The architecture includes an isolated MCP boundary for untrusted extensions; see the [ROADMAP](ROADMAP.md) for delivery status. |
| **Two paradigms, one execution layer** | The same LangGraph execution layer supports recoverable workflows and bounded ReAct loops, sharing HITL, checkpoints, tool contracts, and governance. |
| **Deterministic side-effect boundary** | The LLM performs understanding, exploration, soft ranking, and explanation. Policies, state machines, approvals, idempotency keys, and the execution ledger control business writes. |
| **Traceable retrieval and evaluation** | RAG includes hybrid retrieval, reranking, ACL enforcement, and citation validation. Frozen datasets, CI gates, live runs, and human blind review retain layered evidence without treating Mock results as proof of real-model capability. |

The project is organized around two complementary scenarios:

- **Scenario A · Campaign orchestration:** Starting from campaign requirements and immutable rule snapshots, it performs hard-eligibility filtering, soft ranking within the eligible set, campaign and coupon drafting, two approval stages, enrollment/product aggregation, business confirmation, asynchronous assortment selection, consumer-channel publishing, and merchant notification.
- **Scenario B · Business anomaly attribution:** Within a constrained set of read-only analytical tools, the Agent chooses its investigation path dynamically, distinguishes attributable, conflicting-evidence, and insufficient-evidence outcomes, and stores traceable citations for its conclusion.

Oria uses LangGraph for both workflows and bounded Agent loops. Its current engineering foundation combines SQLite/checkpoints, RAG, Policy, HITL, an execution ledger, outbox patterns, and layered evaluation. The architecture reserves extension points for multi-agent orchestration, context and memory governance, durable jobs, MCP, enterprise data backends, and observability, while keeping the Community environment independently runnable with synthetic data and Mock adapters.

## Architecture Overview

Oria separates execution orchestration, business invariants, and external implementations, then assembles models, tools, storage, and enterprise capabilities through stable contracts.

[![Oria system architecture](docs/diagrams/oria-system-architecture.visual-check.1440x900.light.png)](https://purebluefrank.github.io/oria/diagrams/oria-system-architecture.html)

[Open the interactive architecture diagram](https://purebluefrank.github.io/oria/diagrams/oria-system-architecture.html) · [View the maintainable JSON source](docs/diagrams/oria-system-architecture.architecture.json)

- **Layered:** The ingress layer only normalizes requests. The runtime manages workflows, Agent loops, recovery, and human approval. The domain layer owns state machines, hard eligibility, and all business-write invariants.
- **Pluggable:** Models, tools, storage, and enterprise integrations are assembled through `typing.Protocol` plus registries and factories. Registries are sealed after runtime startup, so implementations can be replaced without rewriting domain logic.
- **Governed consistently:** Policy, Guardrails, Audit, and Eval apply across built-in and extended capabilities. Trusted extensions may run as in-process plugins; untrusted extensions must cross an isolated MCP boundary.

See the [architecture overview](ARCHITECTURE.md) for detailed layers, plugin boundaries, and data invariants.

## Online Scenario Tour

**[Open the Oria online scenario tour](https://purebluefrank.github.io/oria/demo/)**

No installation or API key is required. You can inspect the execution trace for Scenario A and switch to representative attribution cases from Scenario B. This tour uses versioned synthetic data; it is not an online sandbox connected to real enterprise systems. See the [local workflow guide](docs/guides/local-workflow.md) for the complete flow and execution boundaries.

## 60-Second Local Demo

Requires Python 3.11 and uv 0.12.6. Install the locked dependencies and run the zero-configuration, network-free Scenario A demo:

```bash
uv sync --locked --group dev
uv run oria demo
```

The demo prepares synthetic data and produces a cited campaign proposal without executing real business publishing. To try Scenario B, run `uv run oria attribution ask`; see the [Scenario B attribution demo](docs/guides/attribution-demo.md) for parameters and Live mode.

## Choose Your Path

| Path | Best for | Dependencies | Entry point | What it proves |
| --- | --- | --- | --- | --- |
| Zero-config demo | First-time exploration | Core dependencies, no key | `uv run oria demo` | Read-only proposals, citations, and hard-eligibility boundaries under Mock/Fixture |
| Complete local workflow | Evaluating the 10-step flow, HITL, and recovery | Local SQLite, synthetic data, Mock adapters | [Local workflow guide](docs/guides/local-workflow.md) | Community business semantics, two approvals/two waits, and idempotent reconciliation |
| Enterprise persistence adapter | Evaluating PostgreSQL dual databases, repositories, saver, and RLS | Two independent test databases and an explicit Enterprise switch | [PostgreSQL integration guide](docs/guides/postgresql.md) | Proves the backend only when real PostgreSQL integration tests pass; missing DSNs remain blocked |
| Scenario B attribution demo | Observing a bounded ReAct investigation | No key by default; configured model for Live | `uv run oria attribution ask` | Executable evidence chains under Mock replay; dynamic investigation-path selection only in Live mode |
| Real DeepSeek | Trying real-model drafting and soft ranking | `standard` extra, DeepSeek key, initial BGE download | [Real LLM quickstart](docs/guides/real-llm.md) | Calls to the selected DeepSeek model and local BGE; does not prove enterprise adapters |
| Development verification | Contributors and architecture reviewers | Development dependencies | `make lint && make test` | Local regressions and static gates excluding Live, Enterprise, and Performance |

## Enterprise Integration

Enterprise deployments can connect Platform and Business to separate PostgreSQL databases while retaining the same Repository contracts used by SQLite. Production setup requires two explicit TLS-enabled DSNs and least-privilege roles without `BYPASSRLS`; invalid or missing PostgreSQL configuration fails closed rather than falling back to SQLite. Oria migrations own the business and platform tables, while the official `AsyncPostgresSaver` manages its checkpoint tables through `setup()`.

See the [PostgreSQL integration guide](docs/guides/postgresql.md) for configuration, migration, and verification commands. Real PostgreSQL Enterprise verification in this repository is currently blocked because two test DSNs were not available; contract tests are not reported as proof of a real PostgreSQL deployment.

## Development and Documentation

```bash
make lint
make test
make build
make smoke
```

`make test` does not run tests marked Live, Enterprise, or Performance. Those validations require an explicit run switch, a non-empty known target, and the necessary credentials or components. A skipped test, Mock, or Fixture result is never counted as a pass for those environments.

- Getting started: [Scenario B attribution demo](docs/guides/attribution-demo.md) · [Real DeepSeek](docs/guides/real-llm.md) · [Complete local workflow](docs/guides/local-workflow.md)
- Reference: [Data model and core tables](docs/reference/data-model.md) · [ADR index](docs/adr/README.md) · [Threat model](docs/security/V0.3场景A威胁模型.md)
- Planning and evidence: [Roadmap](ROADMAP.md) · [Verification evidence index](reports/verification/README.md) · [Verification template](reports/verification/TEMPLATE.md)

Dependencies must be synchronized through `uv.lock`. The repository does not commit secrets, tokens, real customer data, or `.env` files.
