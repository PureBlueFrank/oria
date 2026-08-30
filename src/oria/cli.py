import asyncio
import hashlib
import json
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from oria import __version__
from oria.config import ConfigResolutionError, resolve_runtime_config
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
app.add_typer(config_app, name="config")
app.add_typer(data_app, name="data")
app.add_typer(eval_app, name="eval")


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
