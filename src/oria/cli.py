from typing import Annotated

import typer

from oria import __version__

app = typer.Typer(
    name="oria",
    help="Oria enterprise AI agent platform.",
    no_args_is_help=True,
    invoke_without_command=True,
)


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
