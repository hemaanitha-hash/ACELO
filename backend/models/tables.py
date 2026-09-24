import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from database import Base
from models.enums import JobStatus, LogLevel


def _id() -> str:
    return str(uuid.uuid4())


class Customer(Base):
    __tablename__ = "customers"

    id = Column(String, primary_key=True, default=_id)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    connections = relationship("Connection", back_populates="customer", cascade="all, delete-orphan")


class Connection(Base):
    """
    One connected platform (Databricks or Microsoft Fabric) for a customer.
    `secret_encrypted` holds the encrypted credential blob (PAT, or Fabric client secret) —
    it is NEVER returned to the frontend. See services/crypto.py.
    """

    __tablename__ = "connections"

    id = Column(String, primary_key=True, default=_id)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False)

    platform = Column(String, nullable=False)  # "databricks" | "fabric"
    workspace = Column(String, nullable=False)  # display name, e.g. "Production Lakehouse"
    endpoint = Column(String, nullable=False)  # workspace URL / tenant-scoped base URL

    # auth_method: "pat" (Databricks) | "service_principal" (Fabric / Databricks OAuth M2M)
    auth_method = Column(String, nullable=False)
    auth_metadata = Column(Text, nullable=True)  # non-secret fields as JSON (e.g. tenant_id, client_id)
    secret_encrypted = Column(Text, nullable=True)  # encrypted PAT / client secret

    status = Column(String, default="pending")  # "pending" | "connected" | "failed"
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", back_populates="connections")


class Environment(Base):
    """
    One customer-owned platform environment (a Fabric workspace, a Databricks
    workspace, or - later - a file source).

    This is the Phase 1 abstraction that replaces reading connection details out
    of Connection.auth_metadata JSON. Identity fields the UI needs to filter and
    display (workspace_id, workspace_name, tenant_id) are real columns so they
    are queryable; the CREDENTIAL still lives encrypted on the Connection row
    and is never duplicated here.

    Every environment belongs to exactly one customer. All environment lookups
    resolve through customer_id + environment id, so one customer can never
    reach another customer's environment.
    """

    __tablename__ = "environments"

    id = Column(String, primary_key=True, default=_id)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False, index=True)

    # The credential-bearing row for this environment. Nullable so an environment
    # can be drafted before credentials are supplied.
    connection_id = Column(String, ForeignKey("connections.id"), nullable=True)

    name = Column(String, nullable=False)  # customer-facing label, e.g. "Fabric Production"
    platform = Column(String, nullable=False)  # "fabric" | "databricks" | "file"

    # "service_principal" (backend client-credentials) | "personal" (delegated
    # user token supplied per request by the signed-in browser session).
    auth_mode = Column(String, default="service_principal")

    tenant_id = Column(String, nullable=True)  # Azure AD tenant (Fabric only)
    workspace_id = Column(String, nullable=True)  # platform workspace identifier
    workspace_name = Column(String, nullable=True)  # resolved from the platform at test time

    # "not_configured" | "connected" | "connection_failed" | "environment_ready" | "discovery_failed"
    status = Column(String, default="not_configured")
    last_error_code = Column(String, nullable=True)  # a platforms.errors.ErrorCode value
    last_error_message = Column(Text, nullable=True)  # already user-safe; never a stack trace
    last_verified_at = Column(DateTime, nullable=True)
    last_discovered_at = Column(DateTime, nullable=True)

    # --- ACELO package provisioning (Step 3) ---
    # "NOT_INSTALLED" | "INSTALLING" | "INSTALLED" | "UPDATE_AVAILABLE" | "UPDATING" | "FAILED"
    provisioning_status = Column(String, default="NOT_INSTALLED")
    package_version = Column(String, nullable=True)  # ACELO package version actually deployed
    provisioning_step = Column(String, nullable=True)  # current step, for honest UI progress
    provisioning_detail_json = Column(Text, nullable=True)  # per-domain outcomes; no secrets
    last_provisioned_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer")
    connection = relationship("Connection")
    resources = relationship("Resource", back_populates="environment", cascade="all, delete-orphan")


class Resource(Base):
    """
    One item discovered inside an environment (Notebook, Lakehouse, SQLEndpoint,
    Experiment, Folder, Cluster, Job, ...).

    Discovery replaces the full resource set for an environment each time it
    runs, so this table always reflects what the platform actually reported on
    the last discovery - it is a cache of real state, never a seeded list.
    """

    __tablename__ = "resources"

    id = Column(String, primary_key=True, default=_id)
    environment_id = Column(String, ForeignKey("environments.id"), nullable=False, index=True)

    platform = Column(String, nullable=False)
    resource_type = Column(String, nullable=False)  # normalised, see adapter _ITEM_TYPE_MAP
    display_name = Column(String, nullable=False)
    platform_resource_id = Column(String, nullable=False)  # the GUID/ID in the customer platform
    status = Column(String, default="discovered")
    detail_json = Column(Text, nullable=True)  # non-secret platform metadata

    created_at = Column(DateTime, default=datetime.utcnow)

    environment = relationship("Environment", back_populates="resources")


class AnalysisJob(Base):
    """
    The parent job for one agent request, e.g. "Analyze everything" creates one
    AnalysisJob with three child JobRuns (cluster/query/storage).
    """

    __tablename__ = "analysis_jobs"

    id = Column(String, primary_key=True, default=_id)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False)
    connection_id = Column(String, ForeignKey("connections.id"), nullable=False)

    request = Column(Text, nullable=False)  # the raw natural-language prompt
    intent = Column(String, nullable=False)  # "cluster" | "query" | "storage" | "all"
    platform = Column(String, nullable=False)  # "databricks" | "fabric"

    status = Column(String, default=JobStatus.QUEUED.value)
    current_step = Column(String, nullable=True)
    progress = Column(Float, nullable=True)  # 0-100, only set from real platform signal; null if unknown
    error = Column(Text, nullable=True)

    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    job_runs = relationship("JobRun", back_populates="analysis_job", cascade="all, delete-orphan")


class JobRun(Base):
    """One domain's run within an AnalysisJob — this is what actually maps to a platform job/run."""

    __tablename__ = "job_runs"

    id = Column(String, primary_key=True, default=_id)
    analysis_job_id = Column(String, ForeignKey("analysis_jobs.id"), nullable=False)

    domain = Column(String, nullable=False)  # "cluster" | "query" | "storage"
    platform = Column(String, nullable=False)
    platform_run_id = Column(String, nullable=True)  # the real Databricks/Fabric run identifier

    status = Column(String, default=JobStatus.QUEUED.value)
    current_step = Column(String, nullable=True)
    progress = Column(Float, nullable=True)
    error = Column(Text, nullable=True)
    result_reference = Column(Text, nullable=True)  # where/how to fetch the full result (e.g. output path, run URL)
    error_code = Column(String, nullable=True)  # a platforms.errors.ErrorCode value, safe for the UI
    result_json = Column(Text, nullable=True)  # normalized result payload, persisted once retrieved
    platform_resource_id = Column(String, nullable=True)  # the notebook/job actually executed
    # "notebook" (direct RunNotebook) | "pipeline" (ACELO pipeline -> same notebook).
    # Null for runs that predate the field and for non-Fabric platforms.
    execution_type = Column(String, nullable=True)
    # Run-history identity: which environment, who started it, and — for a
    # re-run — the original run (history is never overwritten).
    environment_id = Column(String, nullable=True, index=True)
    created_by = Column(String, nullable=True)
    retry_of_run_id = Column(String, nullable=True)

    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    analysis_job = relationship("AnalysisJob", back_populates="job_runs")
    logs = relationship("JobLog", back_populates="job_run", cascade="all, delete-orphan")


class JobLog(Base):
    __tablename__ = "job_logs"

    id = Column(String, primary_key=True, default=_id)
    job_run_id = Column(String, ForeignKey("job_runs.id"), nullable=False)

    timestamp = Column(DateTime, default=datetime.utcnow)
    level = Column(String, default=LogLevel.INFO.value)
    message = Column(Text, nullable=False)
    source = Column(String, nullable=True)  # "acelo" | "databricks" | "fabric"
    # Structured execution-event fields (null on plain log lines).
    event_type = Column(String, nullable=True)  # RUN_CREATED | PLATFORM_RUN_ID_RECEIVED | ...
    stage = Column(String, nullable=True)  # submission | execution | notebook | approval_tracking | results
    platform_run_id = Column(String, nullable=True)
    metadata_json = Column(Text, nullable=True)  # non-secret details only

    job_run = relationship("JobRun", back_populates="logs")


class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(String, primary_key=True, default=_id)
    job_run_id = Column(String, ForeignKey("job_runs.id"), nullable=False)

    resource = Column(String, nullable=False)
    domain = Column(String, nullable=False)
    current_state = Column(Text, nullable=True)
    proposed_change = Column(Text, nullable=False)
    estimated_monthly_savings = Column(Float, default=0)
    current_monthly_cost = Column(Float, default=0)
    confidence = Column(String, default="medium")  # "low" | "medium" | "high"
    status = Column(String, default="open")  # "open" | "approval_pending" | "approved" | "rejected" | "executed"

    created_at = Column(DateTime, default=datetime.utcnow)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id = Column(String, primary_key=True, default=_id)
    recommendation_id = Column(String, ForeignKey("recommendations.id"), nullable=False)

    status = Column(String, default="pending")  # "pending" | "approved" | "rejected"
    requested_by = Column(String, default="ACELO Agent")
    decided_by = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    decided_at = Column(DateTime, nullable=True)


class Execution(Base):
    """
    Deliberately separate from AnalysisJob/JobRun — analysis never modifies the
    customer's environment, execution does, and only after approval. Phase 1
    only defines the shape; the workflow itself is Phase 7/8.
    """

    __tablename__ = "executions"

    id = Column(String, primary_key=True, default=_id)
    approval_id = Column(String, ForeignKey("approval_requests.id"), nullable=False)

    platform = Column(String, nullable=True)
    platform_run_id = Column(String, nullable=True)
    status = Column(String, default=JobStatus.QUEUED.value)
    validation_status = Column(String, nullable=True)  # "pending" | "passed" | "failed"
    progress_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)

    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(String, primary_key=True, default=_id)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False)

    type = Column(String, nullable=False)  # "analysis_complete" | "approval_required" | "execution_complete" | ...
    title = Column(String, nullable=False)
    body = Column(Text, nullable=True)
    link = Column(String, nullable=True)
    read = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)


class AuditHistory(Base):
    __tablename__ = "audit_history"

    id = Column(String, primary_key=True, default=_id)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False)

    event_type = Column(String, nullable=False)
    summary = Column(String, nullable=False)
    payload_json = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


class OptimizationApproval(Base):
    """
    One recommendation from a REAL optimization result awaiting (or past) human
    review in ACELO. Created only from result rows the optimizer produced —
    never seeded, never emailed.

    CURRENT STATE, not history: exactly one record per business key
    (environment_id, domain, resource_id), stored in `source_ref` as
    "current:<environment>|<domain>|<resource>" and unique per customer. Every
    run that produces the same cluster UPDATES that record; run history lives
    in job_runs / job_logs, decision history in approval_audit.
    Numeric fields are nullable so a value the result did not carry stays
    "unknown" rather than becoming 0.
    """

    __tablename__ = "optimization_approvals"
    __table_args__ = (
        UniqueConstraint("customer_id", "acelo_run_id", "resource_id", name="uq_approval_run_resource"),
        # Stable identity for EVERY source: "run:<run>:<resource>" for ACELO run
        # results, "tracking:<environment>:<table>:<cluster>" for the Fabric
        # cluster_optimization_tracking Delta table. Re-reading never duplicates.
        UniqueConstraint("customer_id", "source_ref", name="uq_approval_source_ref"),
    )

    id = Column(String, primary_key=True, default=_id)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False, index=True)
    environment_id = Column(String, nullable=True, index=True)  # null for uploaded-file runs
    # The ACELO run that produced the recommendation. Null for candidates read
    # from the Fabric tracking table, which carries no ACELO run id.
    acelo_run_id = Column(String, ForeignKey("job_runs.id"), nullable=True, index=True)
    source = Column(String, nullable=False, default="run")  # "run" | "tracking_table"
    source_ref = Column(String, nullable=True)
    tracking_status = Column(String, nullable=True)  # the Delta row's own status, informational only
    platform = Column(String, nullable=False)  # "fabric" | "databricks" | "file"
    domain = Column(String, nullable=False, default="cluster")

    resource_id = Column(String, nullable=False)  # cluster_id (falls back to cluster_name)
    resource_name = Column(String, nullable=False)
    optimization_label = Column(String, nullable=True)
    status = Column(String, nullable=False, default="PENDING")

    current_workers = Column(Float, nullable=True)
    recommended_max_workers = Column(Float, nullable=True)
    total_dbus_cost_usd = Column(Float, nullable=True)
    potential_monthly_savings = Column(Float, nullable=True)
    llm_optimization = Column(Text, nullable=True)
    evidence_json = Column(Text, nullable=True)  # the optimizer's own scoring fields

    approved_by = Column(String, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejected_by = Column(String, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    rejection_reason = Column(Text, nullable=True)

    execution_id = Column(String, nullable=True)  # platform run id of an approved execution
    validation_status = Column(String, nullable=True)  # "pending" | "passed" | "failed"
    execution_error = Column(Text, nullable=True)

    # Current-state bookkeeping. One record per business key (see source_ref);
    # every execution that produced this recommendation is recorded here and in
    # approval_audit, never as a second record.
    last_seen_run_id = Column(String, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    # A decided record whose latest run produced a DIFFERENT recommendation:
    # the decision is kept, the new values wait here until a reviewer reopens it.
    requires_new_approval = Column(Boolean, default=False)
    latest_recommendation_json = Column(Text, nullable=True)

    # Query optimization items (domain="query"): the SQL under review and the
    # Validation notebook's own verdict (verified / review_required).
    original_sql = Column(Text, nullable=True)
    optimized_sql = Column(Text, nullable=True)
    platform_validation_status = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApprovalAudit(Base):
    """Immutable trail of every action taken on an OptimizationApproval."""

    __tablename__ = "approval_audit"

    id = Column(String, primary_key=True, default=_id)
    approval_id = Column(String, ForeignKey("optimization_approvals.id"), nullable=False, index=True)
    acelo_run_id = Column(String, nullable=True)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False)
    environment_id = Column(String, nullable=True)
    user_id = Column(String, nullable=True)  # null only for ACELO-system events (record created)
    user_name = Column(String, nullable=True)
    action = Column(String, nullable=False)  # CREATED | APPROVED | REJECTED | EXECUTION_STARTED | ...
    previous_status = Column(String, nullable=True)
    new_status = Column(String, nullable=False)
    reason = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class ClusterRecommendation(Base):
    """
    CURRENT cluster optimization state — analysis/reporting only, no approval.

    One row per (customer, environment scope, cluster). Every cluster run
    UPDATES the rows of the clusters it analysed and INSERTS new ones; the runs
    themselves stay separately in job_runs (append-only history), each with its
    own full result in job_runs.result_json.
    """

    __tablename__ = "cluster_recommendations"
    __table_args__ = (
        UniqueConstraint("customer_id", "environment_key", "cluster_id", name="uq_cluster_rec_entity"),
    )

    id = Column(String, primary_key=True, default=_id)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False, index=True)
    environment_id = Column(String, nullable=True, index=True)
    # environment_id, or "file-run:<run id>" for uploaded files (each upload is its own scope).
    environment_key = Column(String, nullable=False)
    platform = Column(String, nullable=False)
    cluster_id = Column(String, nullable=False)
    cluster_name = Column(String, nullable=True)
    optimization_label = Column(String, nullable=True)  # Optimized / Moderately Optimized / Risky
    current_workers = Column(Float, nullable=True)
    recommended_max_workers = Column(Float, nullable=True)
    avg_cpu_util = Column(Float, nullable=True)
    avg_memory_util = Column(Float, nullable=True)
    total_dbus_cost_usd = Column(Float, nullable=True)
    predicted_cost_usd = Column(Float, nullable=True)
    predicted_savings_pct = Column(Float, nullable=True)
    potential_monthly_savings = Column(Float, nullable=True)
    llm_optimization = Column(Text, nullable=True)
    evidence_json = Column(Text, nullable=True)
    first_run_id = Column(String, nullable=True)
    last_run_id = Column(String, nullable=True, index=True)
    last_platform_run_id = Column(String, nullable=True)
    analyzed_at = Column(String, nullable=True)  # the notebook's acelo_analyzed_at
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DomainResource(Base):
    """
    Environment Resource Registry: WHAT runs, and WITH WHICH tables, for one
    optimization domain in one environment.

    One row per (environment, domain). The AI Agent only decides the domain
    (intent); the backend loads this row to find the notebook/pipeline, the
    lakehouse, schemas and tables. Cluster, Query and Storage never share a row,
    so one domain's settings can never reach another domain's notebook.
    Identifiers only - never credentials.
    """

    __tablename__ = "environment_domain_resources"
    __table_args__ = (UniqueConstraint("environment_id", "domain", name="uq_domain_resource"),)

    id = Column(String, primary_key=True, default=_id)
    customer_id = Column(String, ForeignKey("customers.id"), nullable=False, index=True)
    environment_id = Column(String, ForeignKey("environments.id"), nullable=False, index=True)
    domain = Column(String, nullable=False)  # cluster | query | storage
    platform = Column(String, nullable=True)
    execution_type = Column(String, nullable=True)  # notebook | pipeline
    pipeline_id = Column(String, nullable=True)  # explicit override; else the ACELO-deployed one
    notebook_id = Column(String, nullable=True)  # explicit override; else the ACELO-deployed one
    workspace_id = Column(String, nullable=True)  # blank = the environment's workspace
    lakehouse_id = Column(String, nullable=True)
    lakehouse_workspace_id = Column(String, nullable=True)
    source_lakehouse = Column(String, nullable=True)
    result_lakehouse = Column(String, nullable=True)
    source_schema = Column(String, nullable=True)
    result_schema = Column(String, nullable=True)
    source_table = Column(String, nullable=True)
    result_table = Column(String, nullable=True)
    approval_tracking_table = Column(String, nullable=True)
    settings_json = Column(Text, nullable=True)  # other non-secret identifiers (column_mapping, key vault names...)
    migrated_from = Column(String, nullable=True)  # "connection.auth_metadata" when created by migration
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
