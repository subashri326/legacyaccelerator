"""Airflow DAG generator for UDIF-compatible wave scheduling (Section 7.1)."""

from __future__ import annotations


def generate_airflow_dag(project_slug: str, project_id: str, wave_number: int) -> str:
    """Render an Apache Airflow DAG script orchestrating one migration wave."""
    dag_id = f"l2c_{project_slug}_wave_{wave_number}".lower().replace("-", "_")
    return f'''"""Auto-generated L2C Accelerator Airflow DAG for {project_slug} Wave {wave_number}."""

from datetime import datetime
from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="{dag_id}",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["l2c-accelerator", "{project_slug}"],
) as dag:
    discover = BashOperator(
        task_id="discover",
        bash_command="l2c discover {project_id}",
    )
    assess = BashOperator(
        task_id="assess",
        bash_command="l2c assess {project_id}",
    )
    convert = BashOperator(
        task_id="convert",
        bash_command="l2c convert {project_id} --deliver-git",
    )
    move = BashOperator(
        task_id="move",
        bash_command="l2c move {project_id}",
    )
    reconcile = BashOperator(
        task_id="reconcile",
        bash_command="l2c reconcile {project_id}",
    )

    discover >> assess >> convert >> move >> reconcile
'''
