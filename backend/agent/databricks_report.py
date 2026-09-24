"""
Renders a Databricks compute analysis into the ACELO agent's answer format.

Section order is fixed on purpose — facts first, then what was NOT observed,
and only then recommendations. A reader reaches every opportunity already
knowing which evidence is missing, so no recommendation can be mistaken for a
measured finding.
"""

from typing import Any

from agent.databricks_analysis import Analysis
from platforms.databricks_resources import ResourceStatus

_TYPE_LABELS = {
    "CLASSIC_CLUSTER": "Classic cluster",
    "SERVERLESS_COMPUTE": "Serverless compute",
    "SQL_WAREHOUSE": "SQL warehouse",
}


def _escape_cell(value: Any) -> str:
    """Keeps a pipe inside a value from breaking the markdown table."""
    return str(value if value is not None else "—").replace("|", "\\|")


def render_markdown(analysis: Analysis) -> str:
    data = analysis.to_dict()
    lines: list[str] = []

    # --- Observed Facts ---
    lines.append("## Observed Facts")
    lines.append("")
    workspace = analysis.workspace_name or "the connected workspace"
    if analysis.resources:
        counts = data["observed_facts"]["by_type"]
        described = ", ".join(
            f"{count} × {_TYPE_LABELS.get(type_name, type_name)}" for type_name, count in sorted(counts.items())
        )
        lines.append(f"Discovered {len(analysis.resources)} compute resource(s) in **{workspace}**: {described}.")
    else:
        lines.append(f"No compute resources were discovered in **{workspace}**.")

    unreadable = [s for s in analysis.statuses if s.get("status") != ResourceStatus.OK]
    if unreadable:
        lines.append("")
        for status in unreadable:
            label = _TYPE_LABELS.get(status["resource_type"], status["resource_type"])
            lines.append(
                f"- {label}: **{status['status']}** ({status.get('reason', 'UNKNOWN')}) — not included below."
            )

    # --- Resource Classification ---
    lines.append("")
    lines.append("## Resource Classification")
    lines.append("")
    if analysis.resources:
        lines.append("| Resource | Type | State | Relevant Configuration |")
        lines.append("| --- | --- | --- | --- |")
        for row in data["classification"]:
            lines.append(
                f"| {_escape_cell(row['resource'])} "
                f"| {_escape_cell(_TYPE_LABELS.get(row['type'], row['type']))} "
                f"| {_escape_cell(row['state'])} "
                f"| {_escape_cell(row['configuration'])} |"
            )
    else:
        lines.append("_Nothing to classify._")

    # --- Missing Evidence ---
    lines.append("")
    lines.append("## Missing Evidence")
    lines.append("")
    lines.append("Discovery reads configuration, not behaviour. The following were **not** observed:")
    lines.append("")
    for item in analysis.missing_evidence:
        lines.append(f"- {item}")

    # --- Potential Optimization Opportunities ---
    lines.append("")
    lines.append("## Potential Optimization Opportunities")
    lines.append("")
    if not analysis.findings:
        lines.append(
            "None raised from the configuration visible to discovery. This is **not** a finding that the "
            "workspace is optimally configured — the evidence listed above was never measured."
        )
    else:
        for index, finding in enumerate(analysis.findings, start=1):
            lines.append(f"### {index}. {finding.resource}")
            lines.append("")
            lines.append(f"- **Resource:** {finding.resource} ({_TYPE_LABELS.get(finding.resource_type, finding.resource_type)})")
            lines.append(f"- **Observed evidence:** `{finding.observed_evidence}`")
            lines.append(f"- **Potential issue:** {finding.potential_issue}")
            lines.append(f"- **Recommendation:** {finding.recommendation}")
            lines.append(f"- **Evidence required before execution:** {finding.evidence_required}")
            lines.append(f"- **Expected impact:** {finding.expected_impact}")
            lines.append("")

    lines.append("")
    lines.append(
        "_Discovery only. Nothing was created, modified, started or stopped, and no cost or savings "
        "figure is stated because none was measured._"
    )

    return "\n".join(lines).strip() + "\n"
