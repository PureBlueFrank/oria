import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from oria import __version__
from oria.config import ConfigResolutionError, resolve_runtime_config

app = typer.Typer(
    name="oria",
    help="Oria enterprise AI agent platform.",
    no_args_is_help=True,
    invoke_without_command=True,
)
config_app = typer.Typer(help="Inspect and validate runtime configuration.")
app.add_typer(config_app, name="config")


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
