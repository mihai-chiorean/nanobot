"""CLI for the runtime's Work schedules (SR-24 migration aid).

``nanobot work-schedules export`` prints every ``work_task`` cron job as JSON so
the schedules can be imported into ziggy-work before ``work.scheduleOwner`` is
switched to ``"ziggy-work"``.  It changes nothing; ``--disable`` additionally
sets those jobs ``enabled=false`` so nothing fires twice after the import.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob
from nanobot.cron.work_runner import work_routing

work_schedules_app = typer.Typer(
    help="Inspect Work schedules held in this runtime's cron store."
)


@work_schedules_app.callback()
def _root() -> None:
    """Keep ``export`` a named subcommand instead of collapsing to a single command."""


def export_work_task_entries(jobs: list[CronJob]) -> list[dict[str, Any]]:
    """The export shape for *jobs*: every ``work_task`` job, nothing else.

    Reads ``title``/``skill`` from the ``work_*`` routing hints (post-migration
    they live in ``origin_metadata``, pre-migration in ``channel_meta``) and
    falls back to the job name for the title.
    """
    entries: list[dict[str, Any]] = []
    for job in jobs:
        if job.payload.kind != "work_task":
            continue
        routing = work_routing(job)
        at: str | None = None
        if job.schedule.kind == "at" and job.schedule.at_ms:
            at = datetime.fromtimestamp(
                job.schedule.at_ms / 1000, tz=timezone.utc
            ).isoformat()
        entries.append(
            {
                "title": str(routing.get("work_title") or job.name),
                "content": job.payload.message,
                "schedule": {
                    "kind": job.schedule.kind,
                    "expr": job.schedule.expr,
                    "tz": job.schedule.tz,
                    "at": at,
                },
                "skill": routing.get("work_skill") or routing.get("skill") or None,
                "enabled": bool(job.enabled),
            }
        )
    return entries


def _work_task_jobs(service: CronService) -> list[CronJob]:
    return [job for job in service.list_jobs(include_disabled=True) if job.payload.kind == "work_task"]


@work_schedules_app.command("export")
def export(
    config: str | None = typer.Option(
        None, "--config", "-c", help="Path to config file"
    ),
    disable: bool = typer.Option(
        False,
        "--disable",
        help="Also set the exported work_task jobs enabled=false in the cron store",
    ),
) -> None:
    """Print JSON of every work_task cron job this runtime holds."""
    from nanobot.config.errors import ConfigLoadError
    from nanobot.config.loader import load_config, set_config_path

    config_path = Path(config).expanduser().resolve(strict=False) if config else None
    if config_path is not None:
        if not config_path.exists():
            typer.secho(f"Error: Config file not found: {config_path}", err=True, fg="red")
            raise typer.Exit(1)
        set_config_path(config_path)
    try:
        loaded = load_config(config_path)
    except (ConfigLoadError, ValueError) as exc:
        typer.secho(f"Error: {exc}", err=True, fg="red")
        raise typer.Exit(1) from exc

    store_path = loaded.workspace_path / "cron" / "jobs.json"
    service = CronService(store_path)
    try:
        jobs = _work_task_jobs(service)
    except RuntimeError as exc:
        typer.secho(f"Error: {exc}", err=True, fg="red")
        raise typer.Exit(1) from exc

    # Captured before any disabling so ``--disable`` prints the same JSON plain
    # ``export`` does (the pre-change state is what the import needs).
    entries = export_work_task_entries(jobs)
    if disable:
        for job in jobs:
            if not job.enabled:
                continue
            if service.enable_job(job.id, enabled=False) is None:
                typer.secho(
                    f"Error: failed to disable work_task job {job.id} ({job.name})",
                    err=True,
                    fg="red",
                )
                raise typer.Exit(1)
    typer.echo(json.dumps(entries, indent=2, ensure_ascii=False))
