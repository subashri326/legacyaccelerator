"""Command-line interface (`l2c`) for scripted runs and CI/CD pipelines (Section 7.1)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from accelerator.core.secrets import get_vault
from accelerator.db.repository import ensure_project_from_config
from accelerator.db.session import init_db, session_scope
from accelerator.engines.assess import AssessEngine
from accelerator.engines.convert import ConvertEngine
from accelerator.engines.discover import DiscoverEngine
from accelerator.engines.move import MoveEngine
from accelerator.engines.reconcile import ReconcileEngine
from accelerator.engines.track import TrackEngine
from accelerator.orchestration.runner import RunOrchestrator
from accelerator.plugins.registry import get_plugin_registry

app = typer.Typer(help="Legacy-to-Cloud Data Modernization Accelerator CLI")
console = Console()


@app.command()
def init() -> None:
    """Initialize metadata database schema, default RBAC roles, and sync plugin registry."""
    init_db()
    with session_scope() as db:
        entries = get_plugin_registry().sync_to_db(db)
    console.print(f"[green]Initialized metadata database and synced {len(entries)} plugins.[/green]")


@app.command()
def register(config_path: Path = typer.Argument(..., exists=True, help="Path to client YAML config")) -> None:
    """Register or update a client configuration YAML in the metadata database."""
    init_db()
    raw_yaml = config_path.read_text(encoding="utf-8")
    with session_scope() as db:
        project, cfg_ver, cfg = ensure_project_from_config(db, raw_yaml, actor="cli", actor_role="admin")
    console.print(
        f"[green]Registered[/green] client=[bold]{cfg.client}[/bold] "
        f"project_id=[bold]{project.id}[/bold] config_v{cfg_ver.version_num} ({cfg_ver.config_hash[:10]})"
    )


@app.command("set-secret")
def set_secret(reference: str, value: str) -> None:
    """Store a connection credential in the configured Secrets Vault."""
    get_vault().set_secret(reference, value)
    console.print(f"[green]Stored secret[/green] for {reference}")


@app.command()
def discover(project_id: str) -> None:
    """Stage 1: Harvest source catalog and ETL exports."""
    with session_scope() as db:
        res = DiscoverEngine().run(db, project_id, actor="cli", actor_role="engineer")
    console.print(res)


@app.command()
def assess(project_id: str) -> None:
    """Stage 2: Parse into Neutral IR, build lineage, score effort, and plan waves."""
    with session_scope() as db:
        res = AssessEngine().run(db, project_id, actor="cli", actor_role="migration_lead")
    tbl = Table(title="Effort Estimate by Target Platform (Person-Days)")
    tbl.add_column("Platform")
    tbl.add_column("Person-Days", justify="right")
    for p, d in res.effort_by_platform.items():
        tbl.add_row(p, str(d))
    console.print(tbl)
    console.print(
        f"Scored {res.total_objects_scored} objects | {res.lineage_edges} lineage edges | "
        f"{res.waves_created} wave(s) | Excel report: {res.excel_uri}"
    )


@app.command()
def convert(
    project_id: str,
    wave_id: str | None = typer.Option(None, help="Scope conversion to one wave ID"),
    deliver_git: bool = typer.Option(False, help="Commit wave artifacts to Git branch"),
) -> None:
    """Stage 3: Generate target DDL, dbt/SQL/PySpark, orchestration, and residue queue."""
    with session_scope() as db:
        res = ConvertEngine().run(
            db, project_id, wave_id=wave_id, deliver_git=deliver_git, actor="cli", actor_role="engineer"
        )
    console.print(res)


@app.command()
def move(
    project_id: str,
    mode: str = typer.Option("bulk", help="bulk | incremental"),
    wave_id: str | None = typer.Option(None),
) -> None:
    """Stage 4: Execute partitioned Parquet bulk or incremental watermark load."""
    with session_scope() as db:
        res = MoveEngine().run(db, project_id, wave_id=wave_id, mode=mode, actor="cli", actor_role="engineer")
    console.print(res)


@app.command()
def reconcile(
    project_id: str,
    level: str | None = typer.Option(None, help="Override reconciliation level (L1..L4)"),
) -> None:
    """Stage 5: Execute pushed-down reconciliation checks."""
    with session_scope() as db:
        res = ReconcileEngine().run(db, project_id, level_override=level, actor="cli", actor_role="engineer")
    console.print(res)


@app.command("run-pipeline")
def run_pipeline(
    project_id: str,
    deliver_git: bool = typer.Option(False, help="Deliver generated code to Git branch"),
) -> None:
    """Run Stages 1 through 5 end-to-end."""
    with session_scope() as db:
        res = RunOrchestrator().run_pipeline(
            db, project_id, actor="cli", actor_role="migration_lead", deliver_git=deliver_git
        )
    console.print(f"[green]Pipeline completed[/green] for {project_id}")
    console.print(f"Discover: {res.discover}")
    console.print(f"Assess: {res.assess.effort_by_platform}")
    console.print(f"Convert: {res.convert}")
    console.print(f"Move: {res.move}")
    console.print(f"Reconcile: {res.reconcile}")


@app.command()
def track(project_id: str) -> None:
    """Stage 6: Show lifecycle funnel and active blockers."""
    with session_scope() as db:
        dash = TrackEngine().get_dashboard(db, project_id)
    tbl = Table(title=f"Lifecycle Status ({project_id})")
    tbl.add_column("State")
    tbl.add_column("Count", justify="right")
    for state, cnt in dash["by_state"].items():
        tbl.add_row(state, str(cnt))
    console.print(tbl)
    if dash["blockers"]:
        console.print(f"[yellow]Active blockers ({len(dash['blockers'])}):[/yellow] {dash['blockers']}")


@app.command()
def signoff(
    project_id: str,
    object_key: str,
    approver: str = typer.Option("approver"),
    notes: str = typer.Option("Reconciliation verified via CLI"),
) -> None:
    """Record client approver sign-off for a validated object."""
    with session_scope() as db:
        row = TrackEngine().sign_off(
            db, project_id, object_key=object_key, approver=approver, approver_role="client_approver", notes=notes
        )
    console.print(f"[green]Signed off[/green] {object_key} -> {row.evidence_bundle_uri}")


@app.command()
def plugins(run_golden: bool = typer.Option(False, "--run-golden", help="Run plugin golden checks")) -> None:
    """List installed plugins and optionally execute their golden test checks."""
    reg = get_plugin_registry()
    tbl = Table(title="Registered Accelerator Plugins")
    tbl.add_column("Type")
    tbl.add_column("Name")
    tbl.add_column("Version")
    tbl.add_column("Golden Check")
    for p in reg.list_unique():
        status = "-"
        if run_golden:
            ok, msg = p.run_golden_check()
            status = f"[green]PASS[/green]: {msg}" if ok else f"[red]FAIL[/red]: {msg}"
        tbl.add_row(p.manifest.plugin_type, p.manifest.name, p.manifest.version, status)
    console.print(tbl)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the FastAPI control-plane REST API."""
    import uvicorn

    uvicorn.run("accelerator.api.app:app", host=host, port=port, reload=False)


@app.command()
def ui(port: int = 8501) -> None:
    """Launch the Python-native Streamlit Web UI."""
    app_path = Path(__file__).resolve().parent.parent / "ui" / "streamlit_app.py"
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(app_path), "--server.port", str(port)],
        check=True,
    )


if __name__ == "__main__":
    app()
