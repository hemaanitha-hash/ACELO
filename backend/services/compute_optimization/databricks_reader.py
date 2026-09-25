from __future__ import annotations

import os
from typing import Any

import httpx

from platforms import databricks_auth


class DatabricksSQLReaderError(RuntimeError):
    """Raised when ACELO cannot read evidence through Databricks SQL."""


class DatabricksSQLReader:
    """
    Read ACELO compute evidence through the Databricks SQL Statement
    Execution API.

    Read-only:
      - does not create resources
      - does not modify resources
      - does not execute jobs
      - only executes SELECT statements against the configured SQL warehouse
    """

    def __init__(
        self,
        warehouse_id: str | None = None,
        timeout: float = 45.0,
    ) -> None:
        self.warehouse_id = (
            warehouse_id
            or os.getenv("ACELO_DATABRICKS_SQL_WAREHOUSE_ID", "")
        ).strip()

        self.timeout = timeout

        self.endpoint = (
            databricks_auth.app_host()
            or os.getenv("DATABRICKS_HOST", "")
        ).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = databricks_auth.app_auth_headers()

        if not headers:
            raise DatabricksSQLReaderError(
                "Databricks App authentication is not configured."
            )

        return {
            **headers,
            "Content-Type": "application/json",
        }

    def _validate(self) -> None:
        if not self.endpoint:
            raise DatabricksSQLReaderError(
                "Databricks workspace endpoint is not configured."
            )

        if not self.warehouse_id:
            raise DatabricksSQLReaderError(
                "ACELO_DATABRICKS_SQL_WAREHOUSE_ID is not configured."
            )

    async def execute(
        self,
        statement: str,
    ) -> list[dict[str, Any]]:
        """
        Execute a read-only SQL statement and return rows as dictionaries.
        """

        self._validate()

        sql = statement.strip()

        if not sql:
            raise DatabricksSQLReaderError(
                "SQL statement cannot be empty."
            )

        # This reader is intentionally restricted to SELECT queries.
        normalized = sql.lstrip().lower()

        if not normalized.startswith("select"):
            raise DatabricksSQLReaderError(
                "Compute evidence reader only permits SELECT statements."
            )

        url = f"{self.endpoint}/api/2.0/sql/statements"

        payload = {
            "statement": sql,
            "warehouse_id": self.warehouse_id,
            "wait_timeout": "30s",
            "disposition": "INLINE",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.RequestError as exc:
            raise DatabricksSQLReaderError(
                f"Could not reach Databricks SQL API: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            raise DatabricksSQLReaderError(
                "Databricks SQL authorization failed for the configured "
                "SQL warehouse."
            )

        if response.status_code >= 400:
            detail = response.text[:500]
            raise DatabricksSQLReaderError(
                f"Databricks SQL API returned HTTP "
                f"{response.status_code}: {detail}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise DatabricksSQLReaderError(
                "Databricks SQL API returned an invalid JSON response."
            ) from exc

        status = data.get("status", {})
        state = status.get("state")

        if state and state != "SUCCEEDED":
            error = status.get("error", {})
            message = (
                error.get("message")
                or status.get("error_message")
                or f"Statement execution state: {state}"
            )
            raise DatabricksSQLReaderError(message)

        manifest = data.get("manifest", {})
        schema = manifest.get("schema", {})
        columns = schema.get("columns", [])

        column_names = [
            column.get("name")
            for column in columns
        ]

        result = data.get("result", {})
        rows = result.get("data_array", [])

        if not column_names:
            return []

        return [
            dict(zip(column_names, row))
            for row in rows
        ]

    async def read_table(
        self,
        table_name: str,
    ) -> list[dict[str, Any]]:
        """
        Read one of the approved ACELO evidence tables.
        """

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

        table = allowed_tables.get(table_name)

        if table is None:
            raise DatabricksSQLReaderError(
                f"Unknown ACELO evidence table: {table_name}"
            )

        return await self.execute(
            f"SELECT * FROM {table}"
        )

    async def read_compute_evidence(self) -> dict[str, list[dict[str, Any]]]:
        """
        Read the complete evidence set required by the Compute Optimization
        engine.
        """

        return {
            "cluster": await self.read_table("cluster"),
            "node_timeline": await self.read_table("node_timeline"),
            "node_types": await self.read_table("node_types"),
            "instance_events": await self.read_table("instance_events"),
            "billing_usage": await self.read_table("billing_usage"),
            "job_task_run_timeline": await self.read_table(
                "job_task_run_timeline"
            ),
        }