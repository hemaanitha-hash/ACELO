"""
The deployable query notebooks are the product team's notebooks with only
integration changes: no secret, no email, same detection/validation logic.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "optimization_package"
QUERY = ROOT / "query"


ORIGINALS = [QUERY / "UnhealtyQuery_detection.ipynb", QUERY / "Validation (1).ipynb", QUERY / "Email.ipynb"]
# The originals hold hard-coded credentials and are git-ignored; on a clone
# without them only the deployed (secret-free) notebooks can be checked.
needs_originals = pytest.mark.skipif(
    not all(p.exists() for p in ORIGINALS), reason="product-team originals not present (git-ignored)"
)


def _source(path: Path) -> str:
    return "\n".join("".join(c["source"]) for c in json.loads(path.read_text(encoding="utf-8"))["cells"])


@pytest.fixture(scope="module")
def optimization():
    return _source(QUERY / "deploy" / "QueryOptimization.ipynb")


@pytest.fixture(scope="module")
def apply():
    return _source(QUERY / "deploy" / "ApplyApprovedQuery.ipynb")


@needs_originals
def test_generated_notebooks_are_up_to_date(tmp_path, monkeypatch):
    """Rebuilding from the originals reproduces exactly what is deployed."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("build", ROOT.parent / "scripts" / "build_query_package.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    assert build.build_query_optimization() == json.loads((QUERY / "deploy" / "QueryOptimization.ipynb").read_text("utf-8"))
    assert build.build_apply() == json.loads((QUERY / "deploy" / "ApplyApprovedQuery.ipynb").read_text("utf-8"))


def test_no_secret_is_deployed(optimization, apply):
    for text in (optimization, apply):
        assert not re.search(r"gsk_[A-Za-z0-9]{20,}", text)
        assert "llm_api_key.strip()" in optimization  # the key arrives as a runtime parameter
    assert "%pip install" not in optimization.replace("`%pip install groq` removed", "")


def test_no_email_is_deployed(optimization, apply):
    for text in (optimization.lower(), apply.lower()):
        for token in ("smtplib", "imaplib", "gmail", "sendmail", "mimetext", "app_password"):
            assert token not in text, token
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    deployed = [a["source"] for a in manifest["assets"]]
    assert "query/Email.ipynb" not in deployed
    assert "query/deploy/QueryOptimization.ipynb" in deployed and "query/deploy/ApplyApprovedQuery.ipynb" in deployed


@needs_originals
def test_detection_and_validation_logic_is_preserved(optimization):
    detection = _source(QUERY / "UnhealtyQuery_detection.ipynb")
    validation = _source(QUERY / "Validation (1).ipynb")
    # The scoring, model, threshold and validation code paths are carried verbatim.
    for snippet in (
        "UNIT_PRICE_USD = 0.40",
        '"savings_pct_label",\n        F.when(',
        "PERCENTILE_THRESHOLD = 0.95",
        "def classify_bottleneck(row):",
        'model="openai/gpt-oss-120b"',
        "orig_df\n                        .exceptAll(opt_df)",
        'final_status = "verified"',
        "COST_PER_SECOND = CLUSTER_COST_PER_HOUR / 3600.0",
        "WHERE UPPER(TRIM(q.confidence)) = 'HIGH'",
    ):
        assert snippet in (detection + validation), snippet
        assert snippet in optimization, snippet
    # Batch size is a parameter whose default is the original LIMIT 1.
    assert 'validation_batch_size = "1"' in optimization
    assert "LIMIT {max(1, int(validation_batch_size or 1))}" in optimization


def test_new_opportunities_never_overwrite_decisions(optimization):
    merge = optimization.split("MERGE INTO {_TRACKING} t")[1]
    matched = merge.split("WHEN MATCHED THEN UPDATE SET")[1].split("WHEN NOT MATCHED")[0]
    assert "workflow_status" not in matched  # decisions are never touched
    assert "'pending'" in merge.split("WHEN NOT MATCHED")[1]


@needs_originals
def test_apply_notebook_is_the_email_notebooks_approved_branch(apply):
    email = _source(QUERY / "Email.ipynb")
    for snippet in ('saveAsTable(UPDATED_QUERY_TABLE)', "SET workflow_status = 'APPLIED'",
                    "SET workflow_status = 'FAILED'", "deployment_status STRING"):
        assert snippet in email, snippet
        assert snippet in apply, snippet
    assert 'query_id = ""' in apply and 'approved_by = ""' in apply


@needs_originals
def test_originals_are_left_untouched():
    """The deploy step never edits the product team's files (they still hold their own logic)."""
    detection = _source(QUERY / "UnhealtyQuery_detection.ipynb")
    assert "%pip install -q groq" in detection
    assert "LIMIT 1" in _source(QUERY / "Validation (1).ipynb")
