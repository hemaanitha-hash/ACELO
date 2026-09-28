import asyncio
import os
from unittest.mock import AsyncMock

import pytest

from services.compute_optimization.databricks_reader import (
    DatabricksSQLReader,
    DatabricksSQLReaderError,
)
from services.compute_optimization.models import ComputeEvidence, EvidenceQuality


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


def test_compute_evidence_keeps_successful_tables_when_one_read_fails(monkeypatch):
    reader = DatabricksSQLReader(warehouse_id="test-warehouse")

    async def read_table(table_name):
        if table_name == "node_timeline":
            raise DatabricksSQLReaderError("source unavailable")
        return [{"table": table_name}]

    monkeypatch.setattr(reader, "read_table", AsyncMock(side_effect=read_table))

    evidence = asyncio.run(reader.read_compute_evidence())

    assert evidence["cluster"] == [{"table": "cluster"}]
    assert evidence["node_timeline"] == []
    assert evidence["billing_usage"] == [{"table": "billing_usage"}]
    assert evidence["errors"] == {"node_timeline": "READ_FAILED"}


def test_compute_evidence_requires_quality_metadata():
    evidence = ComputeEvidence(
        cluster={"cluster_id": "abc"},
        utilization=[],
        billing=[],
        quality=EvidenceQuality(
            source_available=True,
            completeness="PARTIAL",
            freshness="STALE",
            timestamp_valid=True,
        ),
    )

    assert evidence.quality.source_available is True
    assert evidence.quality.completeness == "PARTIAL"
    assert evidence.quality.freshness == "STALE"


def test_compute_evidence_tracks_observation_window_and_lineage():
    evidence = ComputeEvidence(
        cluster={"cluster_id": "abc", "cluster_name": "demo"},
        utilization=[{"start_time": "2026-09-01T00:00:00Z", "end_time": "2026-09-01T01:00:00Z"}],
        billing=[],
        observation_start="2026-09-01T00:00:00Z",
        observation_end="2026-09-01T01:00:00Z",
        lineage={
            "resource_id": "abc",
            "resource_type": "cluster",
            "environment_id": "env-123",
            "customer_id": "cust-123",
        },
    )

    assert evidence.observation_start == "2026-09-01T00:00:00Z"
    assert evidence.observation_end == "2026-09-01T01:00:00Z"
    assert evidence.lineage["environment_id"] == "env-123"
    assert evidence.lineage["resource_id"] == "abc"