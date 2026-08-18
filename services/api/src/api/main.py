"""Entrypoints: `blockade-api run` and `blockade-api backfill`."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

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


@app.command()
def backfill(
    observations: Path = typer.Argument(
        ..., help="Observations JSONL from `blockade-detect scan`."
    ),
    dry_run: bool = typer.Option(False, help="Print the plan without touching the database."),
    allow_empty_window: bool = typer.Option(
        False,
        "--allow-empty-window",
        help=(
            "Load a window whose rebuild is not backed by every witness of the "
            "crossing: a scan missing one of its scoring cameras, or a derivation "
            "that would leave the window with no sessions at all."
        ),
    ),
) -> None:
    """Load a re-scored history window into Postgres.

    The follow-through on a better detector: scan re-scores the kept frames,
    this rebuilds the sessions those observations imply and loads both. The
    timeline keeps every version's word per instant; sessions inside the
    window are replaced by the new derivation. Safe to re-run.

    Re-score every camera on a crossing together: the sessions are derived
    from all of them at once, so a partial scan is refused rather than allowed
    to delete the witnesses it did not look at.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from blockade.config import load_roster
    from blockade.schemas import ObservationRecord

    from api import backfill as bf
    from api import db

    settings = get_settings()
    if not dry_run and not settings.database_url:
        typer.secho("BLOCKADE_DATABASE_URL is not set.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        roster = load_roster(settings.camera_config_path)
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(
            f"{exc}\n\nThe roster is how this command knows which cameras witness a "
            "crossing, and a scan missing one of them rebuilds its window from part "
            "of the evidence. Point BLOCKADE_CAMERA_CONFIG_PATH at the roster this "
            "history was captured with.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc

    records = [
        ObservationRecord.model_validate_json(line)
        for line in observations.read_text().splitlines()
        if line.strip()
    ]
    try:
        p = bf.plan(records, roster=roster, allow_empty_window=allow_empty_window)
    except bf.BackfillError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(p.summary())
    if dry_run:
        typer.echo("dry run: nothing loaded")
        return

    async def load() -> None:
        pool = await db.connect(settings.database_url)
        try:
            await db.load_backfill(pool, *bf.plan_rows(p), allow_empty_window=allow_empty_window)
        finally:
            await pool.close()

    try:
        asyncio.run(load())
    except db.EmptyWindowError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("loaded")


if __name__ == "__main__":
    app()
