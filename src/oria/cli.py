import asyncio
import hashlib
import json
import sys
import uuid
from collections.abc import Coroutine
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import typer

from oria import __version__
from oria.attribution_demo import AttributionAskError, run_attribution_ask
from oria.chat.session import run_chat
from oria.config import ConfigResolutionError, resolve_runtime_config
from oria.config.models import ResolvedRuntimeConfig
from oria.core.protocols import Reranker
from oria.core.runtime import build_runtime
from oria.data import DataInitializationError, initialize_data
from oria.demo import DemoResult, DemoRunError, run_demo
from oria.eval import (
    AttributionEvalError,
    AttributionEvalReport,
    RagDatasetError,
    RagEvalReport,
    load_attribution_rubric,
    load_rag_dataset,
    load_rag_eval_config,
    run_attribution_eval,
    run_rag_eval,
    write_value_model,
)
from oria.memory import PersistentMemory
from oria.orchestrator.local_executor import (
    LocalWorkflowResult,
    close_enrollment_window,
    complete_selection,
    decide_confirmation,
    decide_local_approval,
    inject_merchant_event,
    inject_selection_decision,
    start_local_workflow,
)
from oria.permission.local import local_cli_executor, local_operator
from oria.presentation.attribution import render_attribution
from oria.presentation.workflow import (
    MerchantMatch,
    MerchantMatches,
    WorkflowViewModel,
    proposal_rule_summary,
    render_workflow,
)
from oria.rag.rerank import CrossEncoderReranker, FixtureReranker

app = typer.Typer(
    name="oria",
    help="Oria enterprise AI agent platform.",
    no_args_is_help=False,
    invoke_without_command=True,
)
config_app = typer.Typer(help="Inspect and validate runtime configuration.")
data_app = typer.Typer(help="Initialize versioned local data stores.")
eval_app = typer.Typer(help="Run versioned evaluation suites.")
workflow_app = typer.Typer(help="Start and resume the local Scenario A workflow.")
approval_app = typer.Typer(help="Approve or reject an active workflow HITL request.")
mock_app = typer.Typer(help="Inject authenticated synthetic Scenario A events.")
attribution_app = typer.Typer(help="Run the bounded Scenario B attribution demonstration.")
memory_app = typer.Typer(help="View, delete, and export opted-in long-term memories.")
app.add_typer(config_app, name="config")
app.add_typer(data_app, name="data")
app.add_typer(eval_app, name="eval")
app.add_typer(workflow_app, name="workflow")
app.add_typer(approval_app, name="approval")
app.add_typer(mock_app, name="mock")
app.add_typer(attribution_app, name="attribution")
app.add_typer(memory_app, name="memory")


class OutputFormat(StrEnum):
    HUMAN = "human"
    JSON = "json"


class EvalVerification(StrEnum):
    FIXTURE = "fixture"
    COMMUNITY = "community"


def _demo_view(result: DemoResult) -> WorkflowViewModel:
    recommendations = result.proposal.recommended_merchants
    return WorkflowViewModel(
        thread_id=result.thread_id,
        flow_name="招商活动只读提案演示",
        stage_index=1,
        stage_total=1,
        current_stage="生成规则与活动草案",
        completed_steps=("生成规则与活动草案",),
        pending_action="只读提案已完成; 本次演示没有创建活动、券或投放记录。",
        next_command=("oria workflow start --thread-id <thread-id> --campaign-id <campaign-id>"),
        rule_summary=proposal_rule_summary(result.proposal, result.executed_at.isoformat()),
        merchant_matches=MerchantMatches(
            matched_count=result.validation.eligible_merchant_count,
            evaluated_count=result.validation.eligible_merchant_count,
            items=tuple(
                MerchantMatch(
                    merchant_id=item.merchant_id,
                    display_name=item.merchant_id,
                    llm_rank=item.rank,
                    recommendation_reason=item.reason,
                )
                for item in recommendations
            ),
        ),
    )


def _default_eval_asset(relative: str) -> Path:
    checkout = Path("eval") / relative
    if checkout.exists():
        return checkout
    return Path(str(resources.files("oria").joinpath("eval_assets", relative)))


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RagDatasetError(f"evaluation identity file is unavailable: {path.name}") from exc


_DEFAULT_RAG_MANIFEST = _default_eval_asset("datasets/rag/v1.manifest.json")
_DEFAULT_RAG_CONFIG = _default_eval_asset("config/rag.yaml")
_DEFAULT_ATTRIBUTION_MANIFEST = _default_eval_asset("datasets/scenario_b/manifest.json")
_DEFAULT_ATTRIBUTION_RUBRIC = _default_eval_asset("config/attribution-rubric-v1.yaml")


@attribution_app.command("ask")
def attribution_ask(
    question: Annotated[
        str | None,
        typer.Argument(help="Reviewed development question; matching is deterministic and exact."),
    ] = None,
    case_id: Annotated[
        str | None,
        typer.Option("--case-id", help="Exact reviewed development case ID."),
    ] = None,
    llm_profile: Annotated[
        str | None,
        typer.Option(
            "--llm-profile",
            help="Use a configured non-Mock model in Live mode; omitted means offline replay.",
        ),
    ] = None,
    output: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json."),
    ] = OutputFormat.HUMAN,
    data_dir: Annotated[
        Path,
        typer.Option(
            "--data-dir",
            help="Base data root; this command writes only below its reports-tmp directory.",
        ),
    ] = Path(".oria-data"),
) -> None:
    """Investigate one reviewed Scenario B development case with a bounded ReAct loop."""

    try:
        result = asyncio.run(
            run_attribution_ask(
                _DEFAULT_ATTRIBUTION_MANIFEST,
                data_dir=data_dir,
                case_id=case_id,
                question=question,
                llm_profile=llm_profile,
            )
        )
    except AttributionAskError as exc:
        payload = {"ok": False, "error": {"code": exc.code, "message": exc.detail}}
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(f"Attribution demo blocked ({exc.code}): {exc.detail}", err=True)
        raise typer.Exit(code=exc.exit_code) from None
    except (AttributionEvalError, ConfigResolutionError, RagDatasetError, ValueError) as exc:
        payload = {
            "ok": False,
            "error": {"code": "invalid_attribution_demo", "message": str(exc)},
        }
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(f"Attribution demo configuration invalid: {exc}", err=True)
        raise typer.Exit(code=2) from None

    if output is OutputFormat.JSON:
        typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))
    else:
        typer.echo(render_attribution(result))


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the installed Oria version and exit."),
    ] = False,
) -> None:
    """Run the Oria command-line interface."""
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        if _stdio_is_tty():
            _run_chat_command(
                config_path=None,
                runtime_profile=None,
                llm_profile=None,
                embedding_profile=None,
                data_dir=None,
            )
        else:
            typer.echo(ctx.get_help())


def _stdio_is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@app.command("demo")
def demo(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json."),
    ] = OutputFormat.HUMAN,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Read an explicit YAML configuration file."),
    ] = None,
    runtime_profile: Annotated[
        str | None,
        typer.Option("--runtime-profile", help="Override the runtime profile."),
    ] = None,
    llm_profile: Annotated[
        str | None,
        typer.Option("--llm-profile", help="Override the active LLM profile."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Override the runtime data root."),
    ] = None,
) -> None:
    """Run the cited, read-only Scenario A proposal with automatic initialization."""

    try:
        resolved = resolve_runtime_config(
            config_path=config_path,
            runtime_profile=runtime_profile,
            llm_profile=llm_profile,
            data_dir=data_dir,
        )
    except ConfigResolutionError as exc:
        payload = {"ok": False, "error": {"code": "invalid_config", "message": str(exc)}}
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(f"Configuration invalid: {exc}", err=True)
        raise typer.Exit(code=2) from None

    try:
        result = asyncio.run(run_demo(resolved))
    except DemoRunError as exc:
        error = {"code": exc.code, "message": "Oria demo failed closed"}
        if exc.detail:
            error["detail"] = exc.detail
        payload = {
            "ok": False,
            "correlation_id": exc.correlation_id,
            "error": error,
        }
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            detail = f": {exc.detail}" if exc.detail else ""
            typer.echo(
                f"Demo failed ({exc.code}, correlation={exc.correlation_id}){detail}",
                err=True,
            )
        raise typer.Exit(code=1) from None

    payload = result.model_dump(mode="json")
    if output is OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        typer.echo(render_workflow(_demo_view(result)))
        typer.echo(f"\n提案报告: {result.report_path}")


@config_app.command("doctor")
def config_doctor(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json."),
    ] = OutputFormat.HUMAN,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Read an explicit YAML configuration file."),
    ] = None,
    runtime_profile: Annotated[
        str | None,
        typer.Option("--runtime-profile", help="Override the runtime profile."),
    ] = None,
    llm_profile: Annotated[
        str | None,
        typer.Option("--llm-profile", help="Override the active LLM profile."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Override the runtime data root."),
    ] = None,
) -> None:
    """Resolve configuration once and report a secret-free diagnostic summary."""
    try:
        resolved = resolve_runtime_config(
            config_path=config_path,
            runtime_profile=runtime_profile,
            llm_profile=llm_profile,
            data_dir=data_dir,
        )
    except ConfigResolutionError as exc:
        payload = {"ok": False, "error": {"code": "invalid_config", "message": str(exc)}}
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(f"Configuration invalid: {exc}", err=True)
        raise typer.Exit(code=2) from None

    payload = {"ok": True, "config": resolved.public_summary()}
    if output is OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        typer.echo("Configuration valid")
        typer.echo(f"Profile: {resolved.edition}+{resolved.runtime_profile}")
        typer.echo(f"LLM: {resolved.llm.profile_id} ({resolved.llm.model})")
        typer.echo(f"Data directory: {resolved.data_dir}")
        typer.echo(f"Fingerprint: {resolved.config_fingerprint}")


@data_app.command("init")
def data_init(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json."),
    ] = OutputFormat.HUMAN,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Read an explicit YAML configuration file."),
    ] = None,
    runtime_profile: Annotated[
        str | None,
        typer.Option("--runtime-profile", help="Override the runtime profile."),
    ] = None,
    llm_profile: Annotated[
        str | None,
        typer.Option("--llm-profile", help="Override the active LLM profile."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Override the runtime data root."),
    ] = None,
) -> None:
    """Idempotently migrate both SQLite databases and seed synthetic demo data."""
    try:
        resolved = resolve_runtime_config(
            config_path=config_path,
            runtime_profile=runtime_profile,
            llm_profile=llm_profile,
            data_dir=data_dir,
        )
    except ConfigResolutionError as exc:
        payload = {"ok": False, "error": {"code": "invalid_config", "message": str(exc)}}
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(f"Configuration invalid: {exc}", err=True)
        raise typer.Exit(code=2) from None

    try:
        result = asyncio.run(initialize_data(resolved))
    except DataInitializationError as exc:
        payload = {"ok": False, "error": {"code": "data_init_failed", "message": str(exc)}}
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(f"Data initialization failed: {exc}", err=True)
        raise typer.Exit(code=1) from None

    payload = {"ok": True, "data": result.model_dump(mode="json")}
    if output is OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        typer.echo("Data initialized")
        typer.echo(f"Dataset: {result.dataset_version}")
        typer.echo(
            f"Revisions: platform={result.platform_revision}, business={result.business_revision}"
        )
        typer.echo(f"Merchants inserted: {result.merchants_inserted}")


@eval_app.command("run")
def eval_run(
    suite: Annotated[
        str,
        typer.Option("--suite", help="Evaluation suite: rag or attribution."),
    ],
    verification: Annotated[
        EvalVerification,
        typer.Option("--verification", help="Use fixture or pinned community models."),
    ] = EvalVerification.FIXTURE,
    split: Annotated[
        str,
        typer.Option("--split", help="development, holdout, or all."),
    ] = "all",
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help="Versioned dataset manifest."),
    ] = None,
    eval_config: Annotated[
        Path | None,
        typer.Option("--eval-config", help="Pinned community model configuration."),
    ] = None,
    rubric: Annotated[
        Path | None,
        typer.Option("--rubric", help="Blind attribution rubric."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Fresh evaluation runtime data root."),
    ] = None,
    report_path: Annotated[
        Path | None,
        typer.Option("--report", help="Machine-readable evaluation report."),
    ] = None,
    gates_path: Annotated[
        Path | None,
        typer.Option("--gates", help="Optional gate file bound into eval_fingerprint."),
    ] = None,
    lock_path: Annotated[
        Path | None,
        typer.Option("--lock", help="Optional dependency lock bound into eval_fingerprint."),
    ] = None,
) -> None:
    """Run a reviewed deterministic evaluation suite without network access."""

    if suite not in {"rag", "attribution"} or split not in {
        "development",
        "holdout",
        "all",
    }:
        typer.echo(
            json.dumps(
                {"ok": False, "error": {"code": "invalid_eval_selection"}},
                sort_keys=True,
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    report: AttributionEvalReport | RagEvalReport
    try:
        if suite == "attribution":
            if verification is not EvalVerification.FIXTURE:
                raise AttributionEvalError(
                    "attribution community/live verification belongs to V0.4-T05"
                )
            if eval_config is not None or gates_path is not None or lock_path is not None:
                raise AttributionEvalError(
                    "attribution fixture eval does not accept RAG config, gates, or lock options"
                )
            manifest_path = manifest or _DEFAULT_ATTRIBUTION_MANIFEST
            rubric_path = rubric or _DEFAULT_ATTRIBUTION_RUBRIC
            load_attribution_rubric(rubric_path)
            data_root = data_dir or Path(".artifacts/eval/attribution-data")
            output_path = report_path or Path(".artifacts/eval/attribution_v1.json")
            report = asyncio.run(
                run_attribution_eval(
                    manifest_path,
                    rubric_path=rubric_path,
                    data_dir=data_root,
                    split=cast(Literal["development", "holdout", "all"], split),
                )
            )
        else:
            if rubric is not None:
                raise RagDatasetError("RAG eval does not accept an attribution rubric")
            manifest_path = manifest or _DEFAULT_RAG_MANIFEST
            config_path = eval_config or _DEFAULT_RAG_CONFIG
            data_root = data_dir or Path(".artifacts/eval/rag-data")
            output_path = report_path or Path(".artifacts/eval/rag_v1.json")
            pinned = load_rag_eval_config(config_path)
            dataset = load_rag_dataset(manifest_path)
            if dataset.manifest.dataset_version != pinned.dataset_version:
                raise RagDatasetError("RAG dataset version does not match pinned eval config")
            if (gates_path is None) != (lock_path is None):
                raise RagDatasetError("RAG gates and dependency lock must be bound together")
            gates_sha256 = _sha256_file(gates_path) if gates_path is not None else None
            lock_sha256 = _sha256_file(lock_path) if lock_path is not None else None
            environ = {"ORIA_ENVIRONMENT": "test"}
            if verification is EvalVerification.COMMUNITY:
                environ["ORIA_EMBEDDING_PROFILE"] = "bge"
            resolved = resolve_runtime_config(environ=environ, data_dir=data_root)
            if verification is EvalVerification.COMMUNITY:
                if (
                    resolved.embedding.model != pinned.embedding.model
                    or resolved.embedding.revision != pinned.embedding.revision
                ):
                    raise RagDatasetError("runtime embedding does not match pinned RAG config")
                reranker: Reranker = CrossEncoderReranker(
                    model=pinned.reranker.model,
                    revision=pinned.reranker.revision,
                    trust_remote_code=pinned.reranker.trust_remote_code,
                )
                reranker_profile = f"{pinned.reranker.model}@{pinned.reranker.revision}"
            else:
                reranker = FixtureReranker()
                reranker_profile = "fixture"
            report = asyncio.run(
                run_rag_eval(
                    manifest_path,
                    config=resolved,
                    reranker=reranker,
                    reranker_profile=reranker_profile,
                    split=cast(Literal["development", "holdout", "all"], split),
                    gates_sha256=gates_sha256,
                    lock_sha256=lock_sha256,
                )
            )
        write_value_model(output_path, report)
    except (AttributionEvalError, RuntimeError, ValueError) as exc:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": {"code": "eval_blocked", "message": str(exc)},
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            err=True,
        )
        raise typer.Exit(code=2) from None
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "suite": report.suite,
                "dataset_version": report.dataset_version,
                "verification_level": report.verification_level,
                "eval_fingerprint": report.eval_fingerprint,
                "report": str(output_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _workflow_config(
    *,
    output: OutputFormat,
    config_path: Path | None,
    data_dir: Path | None,
    runtime_profile: str | None,
    llm_profile: str | None,
    embedding_profile: str | None,
) -> ResolvedRuntimeConfig:
    try:
        return resolve_runtime_config(
            config_path=config_path,
            runtime_profile=runtime_profile,
            llm_profile=llm_profile,
            embedding_profile=embedding_profile,
            data_dir=data_dir,
        )
    except ConfigResolutionError as exc:
        payload = {"ok": False, "error": {"code": "invalid_config", "message": str(exc)}}
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(f"Configuration invalid: {exc}", err=True)
        raise typer.Exit(code=2) from None


async def _memory_items(
    config: ResolvedRuntimeConfig,
    *,
    operation: Literal["view", "delete", "export"],
    memory_id: str | None = None,
) -> dict[str, object]:
    await initialize_data(config)
    runtime = await build_runtime(config)
    try:
        memory = runtime.memory
        if not isinstance(memory, PersistentMemory):
            raise RuntimeError("long-term memory is unavailable")
        invocation_id = uuid.uuid4().hex
        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id=f"memory-cli-{invocation_id}",
            thread_id=f"memory-cli-{invocation_id}",
            run_id=f"memory-cli-{invocation_id}",
        )
        if operation == "delete":
            if memory_id is None:
                raise ValueError("memory_id is required for deletion")
            deleted = await memory.delete(memory_id, ctx)
            return {"schema_version": 1, "memory_id": memory_id, "deleted": deleted}
        if operation == "export":
            items = await memory.export(ctx)
        else:
            items = [
                item.model_dump(mode="json", exclude={"tenant_id", "subject_id"})
                for item in await memory.view(ctx)
            ]
        return {"schema_version": 1, "items": items}
    finally:
        await runtime.aclose()


def _run_memory_command(
    config: ResolvedRuntimeConfig,
    *,
    operation: Literal["view", "delete", "export"],
    output: OutputFormat,
    memory_id: str | None = None,
) -> None:
    try:
        payload = asyncio.run(_memory_items(config, operation=operation, memory_id=memory_id))
    except (DataInitializationError, LookupError, PermissionError, RuntimeError, ValueError) as exc:
        error = {"ok": False, "error": {"code": "memory_operation_failed", "message": str(exc)}}
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(error, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(f"Memory operation failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if output is OutputFormat.JSON or operation == "export":
        typer.echo(json.dumps({"ok": True, "data": payload}, ensure_ascii=False, sort_keys=True))
        return
    if operation == "delete":
        status = "deleted" if payload["deleted"] else "not found"
        typer.echo(f"Memory {payload['memory_id']}: {status}")
        return
    items = cast(list[dict[str, object]], payload["items"])
    if not items:
        typer.echo("No active long-term memories.")
        return
    for item in items:
        typer.echo(
            f"{item['id']} | confidence={item['confidence']} | "
            f"sensitivity={item['sensitivity']} | expires_at={item['expires_at']}"
        )
        typer.echo(str(item["content"]))


def _memory_config(
    *,
    output: OutputFormat,
    config_path: Path | None,
    runtime_profile: str | None,
    embedding_profile: str | None,
    data_dir: Path | None,
) -> ResolvedRuntimeConfig:
    return _workflow_config(
        output=output,
        config_path=config_path,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
        llm_profile=None,
        embedding_profile=embedding_profile,
    )


@memory_app.command("view")
def memory_view(
    output: Annotated[OutputFormat, typer.Option("--output")] = OutputFormat.HUMAN,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    runtime_profile: Annotated[str | None, typer.Option("--runtime-profile")] = None,
    embedding_profile: Annotated[str | None, typer.Option("--embedding-profile")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """List active, unexpired memories in the trusted local user's namespace."""

    config = _memory_config(
        output=output,
        config_path=config_path,
        runtime_profile=runtime_profile,
        embedding_profile=embedding_profile,
        data_dir=data_dir,
    )
    _run_memory_command(config, operation="view", output=output)


@memory_app.command("delete")
def memory_delete(
    memory_id: Annotated[str, typer.Argument(help="Opaque memory object ID.")],
    output: Annotated[OutputFormat, typer.Option("--output")] = OutputFormat.HUMAN,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    runtime_profile: Annotated[str | None, typer.Option("--runtime-profile")] = None,
    embedding_profile: Annotated[str | None, typer.Option("--embedding-profile")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Delete one memory body, vector projection, and cached search result."""

    config = _memory_config(
        output=output,
        config_path=config_path,
        runtime_profile=runtime_profile,
        embedding_profile=embedding_profile,
        data_dir=data_dir,
    )
    _run_memory_command(config, operation="delete", output=output, memory_id=memory_id)


@memory_app.command("export")
def memory_export(
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    runtime_profile: Annotated[str | None, typer.Option("--runtime-profile")] = None,
    embedding_profile: Annotated[str | None, typer.Option("--embedding-profile")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Export active, unexpired memories as redacted JSON."""

    config = _memory_config(
        output=OutputFormat.JSON,
        config_path=config_path,
        runtime_profile=runtime_profile,
        embedding_profile=embedding_profile,
        data_dir=data_dir,
    )
    _run_memory_command(config, operation="export", output=OutputFormat.JSON)


def _run_workflow_operation(
    operation: Coroutine[Any, Any, LocalWorkflowResult],
    *,
    output: OutputFormat,
) -> None:
    try:
        result = asyncio.run(operation)
    except (LookupError, PermissionError, RuntimeError, ValueError) as exc:
        payload = {
            "ok": False,
            "error": {"code": "workflow_operation_failed", "message": str(exc)},
        }
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(f"Workflow operation failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    payload = result.model_dump(mode="json")
    if output is OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        typer.echo(render_workflow(result.view))


def _run_chat_command(
    *,
    config_path: Path | None,
    runtime_profile: str | None,
    llm_profile: str | None,
    embedding_profile: str | None,
    data_dir: Path | None,
) -> None:
    config = _workflow_config(
        output=OutputFormat.HUMAN,
        config_path=config_path,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
        llm_profile=llm_profile,
        embedding_profile=embedding_profile,
    )
    run_chat(config)


@app.command("chat")
def chat(
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Read an explicit YAML configuration file."),
    ] = None,
    runtime_profile: Annotated[
        str | None,
        typer.Option("--runtime-profile", help="Override the runtime profile."),
    ] = None,
    llm_profile: Annotated[
        str | None,
        typer.Option("--llm-profile", help="Override the active LLM profile."),
    ] = None,
    embedding_profile: Annotated[
        str | None,
        typer.Option("--embedding-profile", help="Override the active embedding profile."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Override the runtime data root."),
    ] = None,
) -> None:
    """Enter the local interactive Agent interface."""

    _run_chat_command(
        config_path=config_path,
        runtime_profile=runtime_profile,
        llm_profile=llm_profile,
        embedding_profile=embedding_profile,
        data_dir=data_dir,
    )


@workflow_app.command("start")
def workflow_start(
    thread_id: Annotated[str, typer.Option(help="Opaque local workflow thread ID.")],
    campaign_id: Annotated[str, typer.Option(help="Synthetic campaign business ID.")],
    request: Annotated[
        str,
        typer.Option(help="Scenario A campaign request passed to the research graph."),
    ] = "生成华东餐饮招商活动并完成预定流程",
    output: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json."),
    ] = OutputFormat.HUMAN,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Read an explicit YAML configuration file."),
    ] = None,
    runtime_profile: Annotated[
        str | None,
        typer.Option("--runtime-profile", help="Override the runtime profile."),
    ] = None,
    llm_profile: Annotated[
        str | None,
        typer.Option("--llm-profile", help="Override the active LLM profile."),
    ] = None,
    embedding_profile: Annotated[
        str | None,
        typer.Option("--embedding-profile", help="Override the active embedding profile."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Override the runtime data root."),
    ] = None,
) -> None:
    """Initialize local data and start a checkpointed Scenario A workflow."""

    config = _workflow_config(
        output=output,
        config_path=config_path,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
        llm_profile=llm_profile,
        embedding_profile=embedding_profile,
    )
    _run_workflow_operation(
        start_local_workflow(
            config,
            thread_id=thread_id,
            campaign_id=campaign_id,
            user_request=request,
        ),
        output=output,
    )


@workflow_app.command("resume")
def workflow_resume(
    thread_id: Annotated[str, typer.Option(help="Existing workflow thread ID.")],
    confirmation_task_id: Annotated[
        str,
        typer.Option(help="Active dynamic ConfirmationTask ID."),
    ],
    decision: Annotated[
        Literal["confirm", "reject"],
        typer.Option(help="Confirmation decision."),
    ] = "confirm",
    output: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json."),
    ] = OutputFormat.HUMAN,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    runtime_profile: Annotated[str | None, typer.Option("--runtime-profile")] = None,
    llm_profile: Annotated[str | None, typer.Option("--llm-profile")] = None,
    embedding_profile: Annotated[str | None, typer.Option("--embedding-profile")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Resume one authenticated business-confirmation external wait."""

    config = _workflow_config(
        output=output,
        config_path=config_path,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
        llm_profile=llm_profile,
        embedding_profile=embedding_profile,
    )
    _run_workflow_operation(
        decide_confirmation(
            config,
            thread_id=thread_id,
            confirmation_task_id=confirmation_task_id,
            decision=decision,
        ),
        output=output,
    )


def _approval_decision_command(
    *,
    thread_id: str,
    approval_id: str,
    decision: Literal["approve", "reject"],
    reason: str | None,
    output: OutputFormat,
    config_path: Path | None,
    data_dir: Path | None,
    runtime_profile: str | None,
    llm_profile: str | None,
    embedding_profile: str | None,
) -> None:
    config = _workflow_config(
        output=output,
        config_path=config_path,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
        llm_profile=llm_profile,
        embedding_profile=embedding_profile,
    )
    _run_workflow_operation(
        decide_local_approval(
            config,
            thread_id=thread_id,
            approval_id=approval_id,
            decision=decision,
            reason=reason,
        ),
        output=output,
    )


@approval_app.command("approve")
def approval_approve(
    thread_id: Annotated[str, typer.Option(help="Existing workflow thread ID.")],
    approval_id: Annotated[str, typer.Option(help="Active approval ID.")],
    output: Annotated[OutputFormat, typer.Option("--output")] = OutputFormat.HUMAN,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    runtime_profile: Annotated[str | None, typer.Option("--runtime-profile")] = None,
    llm_profile: Annotated[str | None, typer.Option("--llm-profile")] = None,
    embedding_profile: Annotated[str | None, typer.Option("--embedding-profile")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Approve the active launch or consumer-publish HITL request and resume."""

    _approval_decision_command(
        thread_id=thread_id,
        approval_id=approval_id,
        decision="approve",
        reason=None,
        output=output,
        config_path=config_path,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
        llm_profile=llm_profile,
        embedding_profile=embedding_profile,
    )


@approval_app.command("reject")
def approval_reject(
    thread_id: Annotated[str, typer.Option(help="Existing workflow thread ID.")],
    approval_id: Annotated[str, typer.Option(help="Active approval ID.")],
    reason: Annotated[str, typer.Option(help="Required rejection reason.")],
    output: Annotated[OutputFormat, typer.Option("--output")] = OutputFormat.HUMAN,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    runtime_profile: Annotated[str | None, typer.Option("--runtime-profile")] = None,
    llm_profile: Annotated[str | None, typer.Option("--llm-profile")] = None,
    embedding_profile: Annotated[str | None, typer.Option("--embedding-profile")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Reject the active launch or consumer-publish HITL request and resume."""

    _approval_decision_command(
        thread_id=thread_id,
        approval_id=approval_id,
        decision="reject",
        reason=reason,
        output=output,
        config_path=config_path,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
        llm_profile=llm_profile,
        embedding_profile=embedding_profile,
    )


@mock_app.command("enrollment")
def mock_enrollment(
    thread_id: Annotated[str, typer.Option(help="Existing workflow thread ID.")],
    source_event_id: Annotated[str, typer.Option(help="Synthetic source event ID.")],
    merchant_id: Annotated[str, typer.Option()] = "demo-m001",
    product_ref: Annotated[str, typer.Option()] = "synthetic-product-demo-m001",
    output: Annotated[OutputFormat, typer.Option("--output")] = OutputFormat.HUMAN,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    runtime_profile: Annotated[str | None, typer.Option("--runtime-profile")] = None,
    llm_profile: Annotated[str | None, typer.Option("--llm-profile")] = None,
    embedding_profile: Annotated[str | None, typer.Option("--embedding-profile")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Inject one authenticated Mock merchant-enrollment event without graph resume."""

    config = _workflow_config(
        output=output,
        config_path=config_path,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
        llm_profile=llm_profile,
        embedding_profile=embedding_profile,
    )
    _run_workflow_operation(
        inject_merchant_event(
            config,
            thread_id=thread_id,
            source_event_id=source_event_id,
            merchant_id=merchant_id,
            product_ref=product_ref,
        ),
        output=output,
    )


@mock_app.command("window-close")
def mock_window_close(
    thread_id: Annotated[str, typer.Option(help="Existing workflow thread ID.")],
    source_event_id: Annotated[str, typer.Option(help="Synthetic source event ID.")],
    output: Annotated[OutputFormat, typer.Option("--output")] = OutputFormat.HUMAN,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    runtime_profile: Annotated[str | None, typer.Option("--runtime-profile")] = None,
    llm_profile: Annotated[str | None, typer.Option("--llm-profile")] = None,
    embedding_profile: Annotated[str | None, typer.Option("--embedding-profile")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Inject a trusted close event and resume the enrollment barrier."""

    config = _workflow_config(
        output=output,
        config_path=config_path,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
        llm_profile=llm_profile,
        embedding_profile=embedding_profile,
    )
    _run_workflow_operation(
        close_enrollment_window(
            config,
            thread_id=thread_id,
            source_event_id=source_event_id,
        ),
        output=output,
    )


@mock_app.command("selection-decision")
def mock_selection_decision(
    thread_id: Annotated[str, typer.Option(help="Existing workflow thread ID.")],
    source_event_id: Annotated[str, typer.Option(help="Synthetic source event ID.")],
    selection_version: Annotated[str, typer.Option()] = "selection-v1",
    decision: Annotated[Literal["selected", "rejected"], typer.Option()] = "selected",
    reason_code: Annotated[str | None, typer.Option()] = None,
    output: Annotated[OutputFormat, typer.Option("--output")] = OutputFormat.HUMAN,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    runtime_profile: Annotated[str | None, typer.Option("--runtime-profile")] = None,
    llm_profile: Annotated[str | None, typer.Option("--llm-profile")] = None,
    embedding_profile: Annotated[str | None, typer.Option("--embedding-profile")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Inject an inbox-authenticated Mock selection decision without graph resume."""

    config = _workflow_config(
        output=output,
        config_path=config_path,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
        llm_profile=llm_profile,
        embedding_profile=embedding_profile,
    )
    _run_workflow_operation(
        inject_selection_decision(
            config,
            thread_id=thread_id,
            source_event_id=source_event_id,
            selection_version=selection_version,
            decision=decision,
            reason_code=reason_code,
        ),
        output=output,
    )


@mock_app.command("selection-complete")
def mock_selection_complete(
    thread_id: Annotated[str, typer.Option(help="Existing workflow thread ID.")],
    source_event_id: Annotated[str, typer.Option(help="Synthetic source event ID.")],
    selection_version: Annotated[str, typer.Option()] = "selection-v1",
    output: Annotated[OutputFormat, typer.Option("--output")] = OutputFormat.HUMAN,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    runtime_profile: Annotated[str | None, typer.Option("--runtime-profile")] = None,
    llm_profile: Annotated[str | None, typer.Option("--llm-profile")] = None,
    embedding_profile: Annotated[str | None, typer.Option("--embedding-profile")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Inject a trusted completion event and resume the selection wait."""

    config = _workflow_config(
        output=output,
        config_path=config_path,
        data_dir=data_dir,
        runtime_profile=runtime_profile,
        llm_profile=llm_profile,
        embedding_profile=embedding_profile,
    )
    _run_workflow_operation(
        complete_selection(
            config,
            thread_id=thread_id,
            source_event_id=source_event_id,
            selection_version=selection_version,
        ),
        output=output,
    )
