"""Entrypoint: `blockade-api run`."""

from __future__ import annotations

import logging

import typer
import uvicorn
from blockade.config import get_settings

app = typer.Typer(help="The Blockade serving layer.", no_args_is_help=True)


@app.callback()
def cli() -> None:
    """Keeps `run` a real subcommand: with a single command and no callback,
    typer collapses the app into the root command and `blockade-api run`
    fails with "unexpected extra argument" - which is exactly how the first
    deployed pod died."""


@app.command()
def run(port: int = typer.Option(8000, help="HTTP port for API, site, and probes.")) -> None:
    """Serve the live board, frames, and the site."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    if not settings.kafka_bootstrap:
        typer.secho("BLOCKADE_KAFKA_BOOTSTRAP is not set.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    from api.app import build_app

    uvicorn.run(build_app(settings), host="0.0.0.0", port=port, log_level="info")  # noqa: S104


if __name__ == "__main__":
    app()
