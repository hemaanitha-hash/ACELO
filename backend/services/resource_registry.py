"""
Environment Resource Registry - the per (environment, optimization domain)
execution configuration.

The AI Agent decides only the INTENT (cluster / query / storage). Everything
else - which notebook or pipeline runs, in which workspace, against which
lakehouse, schemas and tables - is loaded here by the backend:

    config = resource_registry.get(db, environment, "query")
    params = resource_registry.build_runtime_parameters(db, environment, "query", run_id)

Guarantees:
  * One row per (environment, domain). A domain only ever reads its own row, so
    a Cluster source table can never reach the Query notebook (or vice versa).
  * Nothing is invented: unset values stay None; notebook/pipeline IDs come from
    the row or from ACELO's own provisioning registrations for THAT domain.
  * Backward compatible: an environment configured before the registry existed
    kept its Cluster settings in connection.auth_metadata (cluster_* keys, plus
    unprefixed keys from the single-domain era). They are migrated - copied,
    never deleted - into the "cluster" row on first use.
  * Identifiers only. Credentials never live here.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Connection, DomainResource, Environment

logger = logging.getLogger("acelo.registry")

DOMAINS = ("cluster", "query", "storage")

# Values stored as their own columns.
COLUMN_KEYS = (
    "execution_type", "pipeline_id", "notebook_id", "workspace_id", "lakehouse_id", "lakehouse_workspace_id",
    "source_lakehouse", "result_lakehouse", "source_schema", "result_schema", "source_table", "result_table",
    "approval_tracking_table",
)
# Other non-secret identifiers, kept in settings_json.
EXTRA_KEYS = (
    "column_mapping", "fabric_environment_id", "model_dir", "llm_key_vault_uri", "llm_secret_name",
    "llm_model_name", "validation_batch_size", "lakehouse_database",
)
KEYS = COLUMN_KEYS + EXTRA_KEYS

# Runtime notebook parameters a domain's notebook receives (from its own row only).
RUNTIME_PARAMETER_KEYS = (
    "source_table", "result_table", "source_lakehouse", "result_lakehouse", "source_schema", "result_schema",
    "column_mapping", "approval_tracking_table", "model_dir", "llm_key_vault_uri", "llm_secret_name",
    "llm_model_name", "validation_batch_size",
)

# Legacy connection.auth_metadata names that differ from "{domain}_{key}".
_LEGACY_ALIASES = {
    ("query", "approval_tracking_table"): ("query_tracking_table",),
}
# Unprefixed keys from the Cluster-only era. They belong to Cluster ONLY.
_LEGACY_UNPREFIXED_CLUSTER = (
    "source_table", "result_table", "source_lakehouse", "result_lakehouse", "source_schema", "result_schema",
    "column_mapping", "approval_tracking_table", "model_dir", "llm_key_vault_uri", "llm_secret_name",
    "llm_model_name", "lakehouse_database",
)


# --- deployment configuration (backend .env / environment variables) --------------
#
# Runtime values for the demo come from configuration, not from the user:
#   LAKEHOUSE, LAKEHOUSE_ID, LAKEHOUSE_WORKSPACE_ID, TABLE_SCHEMA  -> every domain
#   SOURCE_TABLE, RESULT_TABLE                                     -> Cluster only
#   ACELO_<DOMAIN>_<KEY> (e.g. ACELO_QUERY_RESULT_SCHEMA)          -> that domain only
# An administrator's mapping in the registry always takes precedence.

_SHARED_ENV = {
    "LAKEHOUSE": ("lakehouse_database",),
    "LAKEHOUSE_ID": ("lakehouse_id",),
    "LAKEHOUSE_WORKSPACE_ID": ("lakehouse_workspace_id",),
    "TABLE_SCHEMA": ("source_schema", "result_schema"),
}
# Table names are domain data: unprefixed ones are the Cluster dataset's.
_CLUSTER_ENV = {"SOURCE_TABLE": "source_table", "RESULT_TABLE": "result_table"}

# The query notebook's LLM API key: backend configuration only. Never stored in
# the registry, never returned by an API, never logged; sent to the notebook as a
# secure parameter at run time (the notebooks' existing API-key approach, without
# the key living in the notebook source).
LLM_API_KEY_ENV = ("ACELO_QUERY_LLM_API_KEY", "GROQ_API_KEY")
SECRET_RUNTIME_PARAMETERS = frozenset({"llm_api_key"})


def config_defaults(domain: str) -> dict[str, str]:
    """This domain's values from deployment configuration (identifiers only)."""
    values: dict[str, str] = {}
    for env_name, keys in _SHARED_ENV.items():
        value = (os.getenv(env_name) or "").strip()
        if value:
            for key in keys:
                values[key] = value
    if domain == "cluster":
        for env_name, key in _CLUSTER_ENV.items():
            value = (os.getenv(env_name) or "").strip()
            if value:
                values[key] = value
    for key in KEYS:
        value = (os.getenv(f"ACELO_{domain.upper()}_{key.upper()}") or "").strip()
        if value:
            values[key] = value
    return values


def llm_api_key() -> str | None:
    for name in LLM_API_KEY_ENV:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def _metadata(connection: Connection | None) -> dict:
    if connection is None or not connection.auth_metadata:
        return {}
    try:
        return json.loads(connection.auth_metadata)
    except ValueError:
        return {}


def legacy_values(metadata: dict, domain: str) -> dict[str, str]:
    """What connection.auth_metadata holds for ONE domain (no other domain's keys)."""
    values: dict[str, str] = {}
    for key in KEYS:
        if key == "notebook_id":
            continue  # notebooks always came from ACELO provisioning; a legacy id never overrides it
        names = (f"{domain}_{key}",) + _LEGACY_ALIASES.get((domain, key), ())
        for name in names:
            if metadata.get(name):
                values[key] = str(metadata[name])
                break
        if key not in values and domain == "cluster" and key in _LEGACY_UNPREFIXED_CLUSTER and metadata.get(key):
            values[key] = str(metadata[key])
    return values


def _row_values(row: DomainResource) -> dict[str, str | None]:
    extra = json.loads(row.settings_json) if row.settings_json else {}
    values = {key: getattr(row, key) for key in COLUMN_KEYS}
    values.update({key: extra.get(key) for key in EXTRA_KEYS})
    return values


def _environment(db: Session, environment: Environment | str) -> Environment | None:
    if isinstance(environment, Environment):
        return environment
    return db.query(Environment).filter(Environment.id == environment).first()


def _row(db: Session, environment: Environment, domain: str, create: bool) -> DomainResource | None:
    row = (
        db.query(DomainResource)
        .filter(DomainResource.environment_id == environment.id, DomainResource.domain == domain)
        .first()
    )
    if row is not None or not create:
        return row
    connection = db.query(Connection).filter(Connection.id == environment.connection_id).first()
    legacy = legacy_values(_metadata(connection), domain)
    row = DomainResource(
        customer_id=environment.customer_id, environment_id=environment.id, domain=domain,
        platform=environment.platform, migrated_from="connection.auth_metadata" if legacy else None,
    )
    _assign(row, legacy)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:  # created concurrently; use that one
        db.rollback()
        return _row(db, environment, domain, create=False)
    if legacy:
        logger.info("[RESOURCE_REGISTRY] event=migrated environment_id=%s domain=%s keys=%s",
                    environment.id, domain, sorted(legacy))
    return row


def _assign(row: DomainResource, values: dict[str, Any]) -> None:
    extra = json.loads(row.settings_json) if row.settings_json else {}
    for key, value in values.items():
        value = None if value in (None, "") else str(value)
        if key in COLUMN_KEYS:
            setattr(row, key, value)
        elif key in EXTRA_KEYS:
            if value is None:
                extra.pop(key, None)
            else:
                extra[key] = value
    row.settings_json = json.dumps(extra) if extra else None


def stored(db: Session, environment: Environment | str, domain: str) -> dict[str, str | None]:
    """Only the administrator's mapping for one domain (migrating legacy Cluster settings once)."""
    _check(domain)
    env = _environment(db, environment)
    if env is None:
        return {key: None for key in KEYS}
    row = _row(db, env, domain, create=True)
    return _row_values(row) if row else {key: None for key in KEYS}


def configured(db: Session, environment: Environment | str, domain: str) -> dict[str, str | None]:
    """
    The effective values for one domain: the administrator's mapping, then this
    domain's deployment configuration for anything the mapping leaves unset.
    """
    values = stored(db, environment, domain)
    for key, value in config_defaults(domain).items():
        if not values.get(key):
            values[key] = value
    return values


def sources(db: Session, environment: Environment, domain: str) -> dict[str, str]:
    """Where each effective value comes from: "admin" (registry) or "config" (environment)."""
    admin = stored(db, environment, domain)
    config = config_defaults(domain)
    return {key: ("admin" if admin.get(key) else "config") for key in KEYS if admin.get(key) or config.get(key)}


def get(db: Session, environment: Environment | str, domain: str) -> dict[str, Any]:
    """
    Effective configuration for one domain: stored values plus the notebook /
    pipeline ACELO provisioned for THAT domain when no explicit one is set.
    """
    from services import provisioning_service

    env = _environment(db, environment)
    values = dict(configured(db, env, domain)) if env else {key: None for key in KEYS}
    if env is None:
        return {**values, "domain": domain, "environment_id": None}
    provisioned_notebook = provisioning_service.resolved_domains(db, env).get(domain)
    provisioned_pipeline = provisioning_service.resolved_pipelines(db, env).get(domain)
    values["notebook_source"] = "configured" if values["notebook_id"] else ("acelo-managed" if provisioned_notebook else None)
    values["pipeline_source"] = "configured" if values["pipeline_id"] else ("acelo-managed" if provisioned_pipeline else None)
    values["notebook_id"] = values["notebook_id"] or provisioned_notebook
    values["pipeline_id"] = values["pipeline_id"] or provisioned_pipeline
    if not values["notebook_id"] and not values["pipeline_id"]:
        # A domain ACELO did not deploy (e.g. Storage): an ACELO-named resource
        # already in the workspace is selected automatically.
        found = _discovered_resource(db, env, domain)
        if found:
            kind, item_id = found
            values[f"{kind}_id"] = item_id
            values[f"{kind}_source"] = "discovered"
            values["execution_type"] = values.get("execution_type") or kind
    if not values.get("execution_type"):
        # Default execution resource: Query runs through its ACELO pipeline when
        # one is deployed; Cluster keeps its established default (notebook) unless mapped.
        values["execution_type"] = "pipeline" if (domain != "cluster" and values["pipeline_id"]) else "notebook"
    values["execution_type"] = "pipeline" if values.get("execution_type") == "pipeline" else "notebook"
    values["workspace_id"] = values["workspace_id"] or env.workspace_id
    values["domain"] = domain
    values["environment_id"] = env.id
    values["platform"] = env.platform
    return values


_DISCOVERY_NAMES = {
    "storage": (("pipeline", "DataPipeline", "ACELO_Storage_Optimization_Pipeline"),
                ("notebook", "Notebook", "ACELO Storage Optimization")),
    "query": (("pipeline", "DataPipeline", "ACELO_Query_Optimization_Pipeline"),),
    "cluster": (("pipeline", "DataPipeline", "ACELO_Cluster_Optimization_Pipeline"),),
}


def _discovered_resource(db: Session, environment: Environment, domain: str) -> tuple[str, str] | None:
    from models import Resource

    for kind, resource_type, name in _DISCOVERY_NAMES.get(domain, ()):
        match = (
            db.query(Resource)
            .filter(Resource.environment_id == environment.id, Resource.resource_type == resource_type,
                    Resource.display_name == name)
            .first()
        )
        if match:
            return kind, match.platform_resource_id
    return None


def update(db: Session, environment: Environment, domain: str, changes: dict[str, Any]) -> dict[str, str | None]:
    """Stores changes for ONE domain. Omitted keys are unchanged; empty clears."""
    _check(domain)
    unknown = sorted(set(changes) - set(KEYS))
    if unknown:
        raise ValueError(f"Unknown {domain} resource settings: {', '.join(unknown)}.")
    row = _row(db, environment, domain, create=True)
    _assign(row, changes)
    db.commit()
    return _row_values(row)


def build_runtime_parameters(
    db: Session, environment: Environment | str | None, domain: str, acelo_run_id: str,
    legacy_metadata: dict | None = None,
) -> dict[str, str]:
    """
    The parameters handed to the selected notebook/pipeline for this run -
    built ONLY from this domain's registry row. `legacy_metadata` is used only
    when a run has no environment at all (pre-registry connections).
    """
    _check(domain)
    env = _environment(db, environment) if (db is not None and environment is not None) else None
    if env is not None:
        values = configured(db, env, domain)
    else:
        values = legacy_values(legacy_metadata or {}, domain)
    parameters = {"acelo_run_id": acelo_run_id, "environment_id": env.id if env else ""}
    for key in RUNTIME_PARAMETER_KEYS:
        if values.get(key):
            parameters[key] = str(values[key])
    # Cluster-era default: the configured Lakehouse doubles as source/result location.
    lakehouse = values.get("lakehouse_database")
    if lakehouse:
        parameters.setdefault("source_lakehouse", str(lakehouse))
        parameters.setdefault("result_lakehouse", str(lakehouse))
    if domain == "query":
        key = llm_api_key()
        if key:
            parameters["llm_api_key"] = key  # secure parameter; redacted everywhere it is logged
    return parameters


def adapter_metadata(db: Session, environment: Environment) -> dict[str, str]:
    """
    Registry values for EVERY domain as the adapter's "{domain}_{key}" names.
    Each key is written from its own domain's row only, and an unset value is
    absent - so a stale legacy key can never fill a gap for another domain.
    """
    flat: dict[str, str] = {}
    for domain in DOMAINS:
        for key, value in configured(db, environment, domain).items():
            if value:
                flat[f"{domain}_{key}"] = value
    return flat


def overlay(auth_metadata: dict, flat: dict[str, str]) -> dict:
    """Replaces every domain-scoped key with the registry's (clearing absent ones)."""
    for domain in DOMAINS:
        for key in KEYS:
            auth_metadata.pop(f"{domain}_{key}", None)
    auth_metadata.pop("query_tracking_table", None)
    auth_metadata.update(flat)
    return auth_metadata


# Package assets that belong to a registry domain.
ASSET_DOMAIN = {"approval_tracking": "cluster", "query_apply": "query"}


def registry_domain(asset_domain: str) -> str:
    return ASSET_DOMAIN.get(asset_domain, asset_domain)


def apply_to_adapter(db: Session, environment: Environment, auth_metadata: dict) -> dict:
    """
    Builds the adapter's domain-scoped configuration from the registry:
      1. every "{domain}_*" key comes from that domain's row (legacy keys cleared),
      2. notebooks/pipelines ACELO provisioned for a domain fill what the row
         does not set explicitly,
      3. each domain's OneLake result location comes from its own lakehouse.
    """
    from services import provisioning_service

    overlay(auth_metadata, adapter_metadata(db, environment))
    for domain, item_id in provisioning_service.resolved_domains(db, environment).items():
        auth_metadata.setdefault(f"{domain}_notebook_id", item_id)  # e.g. query_apply
    for domain in DOMAINS:
        effective = get(db, environment, domain)
        for key in ("notebook_id", "pipeline_id"):
            if effective.get(key):
                auth_metadata[f"{domain}_{key}"] = effective[key]
        auth_metadata[f"{domain}_execution_type"] = effective["execution_type"]
    for key, value in provisioning_service.result_location(db, environment).items():
        auth_metadata[key] = value
    if environment.id:
        auth_metadata["environment_id"] = environment.id
    return auth_metadata


def summary(db: Session, environment: Environment) -> list[dict[str, Any]]:
    """Per-domain status for Environment Setup. Names and IDs only, never credentials."""
    from services import job_service
    from models import Resource

    names = {
        r.platform_resource_id: r.display_name
        for r in db.query(Resource).filter(Resource.environment_id == environment.id)
    }
    result = []
    for domain in DOMAINS:
        cfg = get(db, environment, domain)
        is_pipeline = cfg["execution_type"] == "pipeline"
        item_id = cfg["pipeline_id"] if is_pipeline else cfg["notebook_id"]
        missing = job_service.missing_required_configuration(domain, build_runtime_parameters(db, environment, domain, "-"))
        if not item_id:
            missing.append("pipeline" if is_pipeline else "notebook")
        from services import provisioning_service

        if environment.platform == "fabric" and provisioning_service.default_lakehouse(db, environment, domain) is None:
            missing.append("lakehouse")  # results are read from THIS domain's Lakehouse on OneLake
        result.append({
            "domain": domain,
            "configured": not missing,
            "missing": missing,
            "execution_type": cfg["execution_type"],
            "resource": (
                {"type": "pipeline" if is_pipeline else "notebook", "id": item_id,
                 "name": names.get(item_id), "source": cfg["pipeline_source" if is_pipeline else "notebook_source"]}
                if item_id else None
            ),
            "settings": {k: cfg.get(k) for k in KEYS},
            "sources": sources(db, environment, domain),
            "llm_api_key_configured": domain == "query" and llm_api_key() is not None,
        })
    return result


def _check(domain: str) -> None:
    if domain not in DOMAINS:
        raise ValueError(f"Unknown optimization domain: {domain}")
