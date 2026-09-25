# ACELO Compute Optimization Engine

This is the first migration of the deterministic rules from the working Databricks notebook into ACELO backend code.

Included:
- `models.py` normalized evidence/result models
- `demo_provider.py` demo evidence adapter
- `engine.py` cluster sizing, autoscaling and runtime finding rules

Intentionally excluded from this MVP:
- Groq/LLM credentials or calls
- Databricks mutation APIs
- automatic remediation

Human approval remains required and execution is disabled.

Next integration step: connect `demo_provider.py` to the six `databricks_ws.agent.*` demo tables, then call `analyze_compute_evidence()` from the existing `/api/databricks/agent/analyze` flow and map its result into the existing `DatabricksAgentResult`.
