from sqlalchemy.orm import Session

from models import AnalysisJob, Connection
from services import job_service

DOMAIN_KEYWORDS = {
    "cluster": ["cluster", "compute", "node", "worker"],
    "query": ["query", "sql", "queries", "warehouse"],
    "storage": ["storage", "table", "disk", "lakehouse storage"],
}
ALL_KEYWORDS = ["everything", "all domains", "full analysis", "analyze all"]


def detect_intent(prompt: str) -> list[str]:
    """Very small keyword router. Swap for an LLM-based classifier when ready —
    the return shape (a list of domains to run) is what the rest of the system depends on."""
    lowered = prompt.lower()
    if any(k in lowered for k in ALL_KEYWORDS):
        return ["cluster", "query", "storage"]

    matched = [domain for domain, keywords in DOMAIN_KEYWORDS.items() if any(k in lowered for k in keywords)]
    if not matched:
        # Never guess: running every domain for an unclear request mixes domains.
        raise UnclearIntent(
            "Tell ACELO what to optimize: clusters (e.g. \"Check my cluster utilization\"), "
            "queries (\"Find unhealthy queries\") or storage (\"Check storage optimization\")."
        )
    return matched


class UnclearIntent(ValueError):
    """The request names no optimization domain; ACELO asks instead of guessing."""


FILE_ANALYSIS_DOMAINS = {"cluster"}
DEFAULT_FILE_PROMPT = "Analyze this cluster file"
FOCUS_KEYWORDS = {
    "idle": ["idle", "underutil", "unused"],
    "oversized": ["oversiz", "over-siz", "rightsiz", "too large", "too big"],
}


class UnsupportedFileRequest(ValueError):
    """The prompt asks for a domain file analysis does not support."""


def plan_file_request(prompt: str | None) -> tuple[str, str | None]:
    """
    Routes a request that arrived WITH an uploaded file. Such requests always go
    to the file-analysis path — never to a Fabric/Databricks connection.

    Returns (prompt, focus) where focus is "idle" | "oversized" | None. Focus
    only changes what the results view highlights; the full optimizer always runs.
    """
    text = (prompt or "").strip() or DEFAULT_FILE_PROMPT
    lowered = text.lower()
    explicit = [d for d, words in DOMAIN_KEYWORDS.items() if any(w in lowered for w in words)]
    focus = next((f for f, words in FOCUS_KEYWORDS.items() if any(w in lowered for w in words)), None)

    # "Find idle clusters" / "Analyze this file" are cluster requests. Only a
    # prompt that names Query or Storage and NOT Cluster is refused.
    if explicit and not FILE_ANALYSIS_DOMAINS.intersection(explicit) and focus is None:
        raise UnsupportedFileRequest(
            "File analysis currently supports Cluster datasets only. "
            f"This request looks like {', '.join(explicit)} analysis."
        )
    return text, focus


async def start_agent_job(
    db: Session,
    customer_id: str,
    connection: Connection,
    prompt: str,
    delegated_token: str | None = None,
) -> AnalysisJob:
    """
    Intent + orchestration only. Everything else — persistence, platform calls,
    status tracking — belongs to job_service and the adapters. This function
    returns as soon as each JobRun has been *dispatched*, not completed.
    """
    domains = detect_intent(prompt)
    intent = "all" if len(domains) > 1 else domains[0]

    analysis_job = job_service.create_analysis_job(db, customer_id, connection, request=prompt, intent=intent)

    for domain in domains:
        job_run = job_service.create_job_run(db, analysis_job, domain)
        await job_service.start_job_run(db, connection, job_run, delegated_token)

    db.refresh(analysis_job)
    return analysis_job
