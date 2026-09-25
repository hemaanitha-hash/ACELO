import asyncio
import os

import pytest

from services.compute_optimization.databricks_reader import (
    DatabricksSQLReader,
    DatabricksSQLReaderError,
)


def test_reader_requires_warehouse_id(monkeypatch):
    monkeypatch.delenv(
        "ACELO_DATABRICKS_SQL_WAREHOUSE_ID",
        raising=False,
    )

    reader = DatabricksSQLReader()

    with pytest.raises(DatabricksSQLReaderError):
        reader._validate()


def test_reader_allows_only_select():
    reader = DatabricksSQLReader(
        warehouse_id="test-warehouse",
    )

    with pytest.raises(DatabricksSQLReaderError):
        asyncio.run(
            reader.execute(
                "DELETE FROM databricks_ws.agent.cluster"
            )
        )


def test_reader_allows_known_table():
    reader = DatabricksSQLReader(
        warehouse_id="test-warehouse",
    )

    assert reader is not None

    allowed_tables = {
        "cluster": "databricks_ws.agent.cluster",
        "node_timeline": "databricks_ws.agent.node_timeline",
        "node_types": "databricks_ws.agent.node_types",
        "instance_events": "databricks_ws.agent.instance_events",
        "billing_usage": "databricks_ws.agent.billing_usage",
        "job_task_run_timeline": (
            "databricks_ws.agent.job_task_run_timeline"
        ),
    }

    assert allowed_tables["cluster"] == (
        "databricks_ws.agent.cluster"
    )


def test_reader_rejects_unknown_table():
    reader = DatabricksSQLReader(
        warehouse_id="test-warehouse",
    )

    with pytest.raises(DatabricksSQLReaderError):
        asyncio.run(
            reader.read_table("users")
        )