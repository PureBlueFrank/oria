import asyncio
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from oria import __version__
from oria.config import ConfigResolutionError, resolve_runtime_config
from oria.data import DataInitializationError, initialize_data
from oria.demo import DemoRunError, run_demo

app = typer.Typer(
    name="oria",
    help="Oria enterprise AI agent platform.",
    no_args_is_help=True,
    invoke_without_command=True,
)
config_app = typer.Typer(help="Inspect and validate runtime configuration.")
data_app = typer.Typer(help="Initialize versioned local data stores.")
app.add_typer(config_app, name="config")
app.add_typer(data_app, name="data")


class OutputFormat(StrEnum):
    HUMAN = "human"
    JSON = "json"


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
