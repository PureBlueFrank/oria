import asyncio
import hashlib
import json
from collections.abc import Coroutine
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import typer

from oria import __version__
from oria.config import ConfigResolutionError, resolve_runtime_config
from oria.config.models import ResolvedRuntimeConfig
from oria.core.protocols import Reranker
from oria.data import DataInitializationError, initialize_data
from oria.demo import DemoRunError, run_demo
from oria.eval import (
    RagDatasetError,
    load_rag_dataset,
    load_rag_eval_config,
    run_rag_eval,
    write_value_model,
)
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
from oria.rag.rerank import CrossEncoderReranker, FixtureReranker

app = typer.Typer(
    name="oria",
    help="Oria enterprise AI agent platform.",
    no_args_is_help=True,
    invoke_without_command=True,
)
config_app = typer.Typer(help="Inspect and validate runtime configuration.")
data_app = typer.Typer(help="Initialize versioned local data stores.")
eval_app = typer.Typer(help="Run versioned evaluation suites.")
workflow_app = typer.Typer(help="Start and resume the local Scenario A workflow.")
approval_app = typer.Typer(help="Approve or reject an active workflow HITL request.")
mock_app = typer.Typer(help="Inject authenticated synthetic Scenario A events.")
app.add_typer(config_app, name="config")
app.add_typer(data_app, name="data")
app.add_typer(eval_app, name="eval")
app.add_typer(workflow_app, name="workflow")
app.add_typer(approval_app, name="approval")
app.add_typer(mock_app, name="mock")


class OutputFormat(StrEnum):
    HUMAN = "human"
    JSON = "json"


class EvalVerification(StrEnum):
    FIXTURE = "fixture"
    COMMUNITY = "community"


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


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the installed Oria version and exit."),
    ] = False,
) -> None:
    """Run the Oria command-line interface."""
    if version:
        typer.echo(__version__)
        raise typer.Exit()


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
        payload = {
            "ok": False,
            "correlation_id": exc.correlation_id,
            "error": {"code": exc.code, "message": "Oria demo failed closed"},
        }
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(
                f"Demo failed ({exc.code}, correlation={exc.correlation_id})",
                err=True,
            )
        raise typer.Exit(code=1) from None

    payload = result.model_dump(mode="json")
    if output is OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        typer.echo("Oria offline demo completed")
        typer.echo(f"Correlation: {result.correlation_id}")
        typer.echo(f"Eligible merchants: {result.validation.eligible_merchant_count}")
        typer.echo(f"Proposal report: {result.report_path}")


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
        typer.Option("--suite", help="Evaluation suite; currently only rag."),
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
        Path,
        typer.Option("--manifest", help="Versioned RAG dataset manifest."),
    ] = _DEFAULT_RAG_MANIFEST,
    eval_config: Annotated[
        Path,
        typer.Option("--eval-config", help="Pinned community model configuration."),
    ] = _DEFAULT_RAG_CONFIG,
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", help="Fresh evaluation runtime data root."),
    ] = Path(".artifacts/eval/rag-data"),
    report_path: Annotated[
        Path,
        typer.Option("--report", help="Machine-readable evaluation report."),
    ] = Path(".artifacts/eval/rag_v1.json"),
    gates_path: Annotated[
        Path | None,
        typer.Option("--gates", help="Optional gate file bound into eval_fingerprint."),
    ] = None,
    lock_path: Annotated[
        Path | None,
        typer.Option("--lock", help="Optional dependency lock bound into eval_fingerprint."),
    ] = None,
) -> None:
    """Run the reviewed RAG suite without invoking an LLM provider."""

    if suite != "rag" or split not in {"development", "holdout", "all"}:
        typer.echo(
            json.dumps(
                {"ok": False, "error": {"code": "invalid_eval_selection"}},
                sort_keys=True,
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        pinned = load_rag_eval_config(eval_config)
        dataset = load_rag_dataset(manifest)
        if dataset.manifest.dataset_version != pinned.dataset_version:
            raise RagDatasetError("RAG dataset version does not match pinned eval config")
        if (gates_path is None) != (lock_path is None):
            raise RagDatasetError("RAG gates and dependency lock must be bound together")
        gates_sha256 = _sha256_file(gates_path) if gates_path is not None else None
        lock_sha256 = _sha256_file(lock_path) if lock_path is not None else None
        environ = {"ORIA_ENVIRONMENT": "test"}
        if verification is EvalVerification.COMMUNITY:
            environ["ORIA_EMBEDDING_PROFILE"] = "bge"
        resolved = resolve_runtime_config(environ=environ, data_dir=data_dir)
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
                manifest,
                config=resolved,
                reranker=reranker,
                reranker_profile=reranker_profile,
                split=cast(Literal["development", "holdout", "all"], split),
                gates_sha256=gates_sha256,
                lock_sha256=lock_sha256,
            )
        )
        write_value_model(report_path, report)
    except (RuntimeError, ValueError) as exc:
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
                "report": str(report_path),
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
) -> ResolvedRuntimeConfig:
    try:
        return resolve_runtime_config(config_path=config_path, data_dir=data_dir)
    except ConfigResolutionError as exc:
        payload = {"ok": False, "error": {"code": "invalid_config", "message": str(exc)}}
        if output is OutputFormat.JSON:
            typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            typer.echo(f"Configuration invalid: {exc}", err=True)
        raise typer.Exit(code=2) from None


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
        typer.echo(f"Workflow {result.status}: {result.thread_id}")
        for interruption in result.interrupts:
            typer.echo(
                f"Waiting: {interruption.get('kind')} "
                f"({interruption.get('approval_id') or interruption.get('confirmation_task_id')})"
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
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Override the runtime data root."),
    ] = None,
) -> None:
    """Initialize local data and start a checkpointed Scenario A workflow."""

    config = _workflow_config(output=output, config_path=config_path, data_dir=data_dir)
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
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Resume one authenticated business-confirmation external wait."""

    config = _workflow_config(output=output, config_path=config_path, data_dir=data_dir)
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
) -> None:
    config = _workflow_config(output=output, config_path=config_path, data_dir=data_dir)
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
    )


@approval_app.command("reject")
def approval_reject(
    thread_id: Annotated[str, typer.Option(help="Existing workflow thread ID.")],
    approval_id: Annotated[str, typer.Option(help="Active approval ID.")],
    reason: Annotated[str, typer.Option(help="Required rejection reason.")],
    output: Annotated[OutputFormat, typer.Option("--output")] = OutputFormat.HUMAN,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
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
    )


@mock_app.command("enrollment")
def mock_enrollment(
    thread_id: Annotated[str, typer.Option(help="Existing workflow thread ID.")],
    source_event_id: Annotated[str, typer.Option(help="Synthetic source event ID.")],
    merchant_id: Annotated[str, typer.Option()] = "demo-m001",
    product_ref: Annotated[str, typer.Option()] = "synthetic-product-demo-m001",
    output: Annotated[OutputFormat, typer.Option("--output")] = OutputFormat.HUMAN,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Inject one authenticated Mock merchant-enrollment event without graph resume."""

    config = _workflow_config(output=output, config_path=config_path, data_dir=data_dir)
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
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Inject a trusted close event and resume the enrollment barrier."""

    config = _workflow_config(output=output, config_path=config_path, data_dir=data_dir)
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
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Inject an inbox-authenticated Mock selection decision without graph resume."""

    config = _workflow_config(output=output, config_path=config_path, data_dir=data_dir)
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
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Inject a trusted completion event and resume the selection wait."""

    config = _workflow_config(output=output, config_path=config_path, data_dir=data_dir)
    _run_workflow_operation(
        complete_selection(
            config,
            thread_id=thread_id,
            source_event_id=source_event_id,
            selection_version=selection_version,
        ),
        output=output,
    )
