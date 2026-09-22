"""
The deployed Cluster notebook must prove, in its own run output, which
parameters it received and whether its reads/writes succeeded. These checks run
the notebook's CONFIGURATION cell for real (it needs no Spark) and inspect the
optimizer cell's source for the read/write trace.
"""

import contextlib
import io
import json
from pathlib import Path

import pytest

NOTEBOOK = Path(__file__).resolve().parents[1] / "optimization_package" / "cluster" / "Clusterfabric.ipynb"


def _cells():
    return ["".join(c["source"]) for c in json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
            if c["cell_type"] == "code"]


def _run_config(**params):
    """Executes the parameters + configuration cells with ACELO-style injected values."""
    cells = _cells()
    parameters_cell = next(c for c in cells if "acelo_run_id = \"\"" in c)
    config_cell = next(c for c in cells if "_missing_params = [" in c)
    scope: dict = {}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(parameters_cell, scope)  # noqa: S102 - our own notebook source
        scope.update(params)          # what Fabric's parameter injection does
        try:
            exec(config_cell, scope)  # noqa: S102
            error = None
        except ValueError as exc:
            error = exc
    return out.getvalue(), scope, error


def test_received_parameters_are_logged_and_validation_passes():
    out, scope, error = _run_config(
        acelo_run_id="run-123", environment_id="env-9", source_table="realistic_cluster_dataset",
        result_table="acelo_results", source_lakehouse="Data", result_lakehouse="Data",
        llm_key_vault_uri="https://kv.vault.azure.net/", llm_secret_name="groq-key",
    )
    assert error is None
    assert "[NOTEBOOK_RUNTIME_PARAMETERS]" in out
    for line in ("acelo_run_id=run-123", "environment_id=env-9", "source_table=realistic_cluster_dataset",
                 "result_table=acelo_results", "source_lakehouse=Data", "result_lakehouse=Data"):
        assert line in out
    assert "notebook_started=true" in out and "parameter_validation=passed" in out
    assert "[NOTEBOOK_DATA_PATH]" in out
    assert "source_table=Data.realistic_cluster_dataset" in out
    # Credential pointers are acknowledged, never printed.
    assert "llm_key_vault_uri=<set>" in out and "kv.vault.azure.net" not in out
    assert "groq-key" not in out
    # What the result rows will carry for ACELO's comparison.
    assert scope["ACELO_RECEIVED_PARAMETERS"]["acelo_run_id"] == "run-123"
    assert "llm_key_vault_uri" not in scope["ACELO_RECEIVED_PARAMETERS"]


def test_missing_parameter_stops_the_notebook():
    out, _, error = _run_config(acelo_run_id="run-123", source_table="", result_table="acelo_results")
    assert error is not None
    assert "parameter_validation=failed" in out
    assert "missing_parameter=source_table" in out
    assert "notebook_started=true" not in out


def test_read_and_write_are_traced_only_after_success():
    optimizer = next(c for c in _cells() if "spark.read.table(SOURCE_TABLE)" in c)
    read = optimizer.index("spark.read.table(SOURCE_TABLE)")
    # STARTED before the read; SUCCESS only after the empty-table check passes.
    assert optimizer.index('print("STATUS=STARTED")') < read
    read_ok = optimizer.index('print("STATUS=SUCCESS")')
    assert read_ok > optimizer.index("if _source_rows == 0:")
    assert 'print(f"row_count={_source_rows}")' in optimizer
    assert 'print(f"column_count={len(df.columns)}")' in optimizer

    write = optimizer.index(".saveAsTable(RESULT_TABLE)")
    write_started = optimizer.rindex('print("STATUS=STARTED")')
    write_ok = optimizer.rindex('print("STATUS=SUCCESS")')
    assert write_started < write < write_ok
    assert 'print(f"row_count={_rows_written}")' in optimizer
    # Failures expose the ORIGINAL exception and traceback.
    assert optimizer.count("traceback.print_exc()") >= 2
    assert 'pdf_inference["acelo_received_parameters"]' in optimizer
    assert '"received_parameters": ACELO_RECEIVED_PARAMETERS' in optimizer


# --- dependencies come from the Fabric Environment, never from run-time pip --------

def _dependency_cell():
    return next(c for c in _cells() if "ACELO_REQUIRED_LIBRARIES" in c)


def test_notebook_never_installs_packages_at_runtime():
    source = "\n".join(_cells())
    for forbidden in ("%pip", "!pip", "pip install", "subprocess", "os.system", "ensurepip"):
        assert forbidden not in source, forbidden


def test_dependency_check_names_exactly_the_libraries_the_optimizer_imports():
    scope: dict = {"acelo_run_id": "run-1"}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(_dependency_cell(), scope)  # noqa: S102 - our own notebook source
    assert set(scope["ACELO_REQUIRED_LIBRARIES"]) == {"numpy", "pandas", "sklearn", "xgboost"}
    assert "[NOTEBOOK_DEPENDENCIES]" in out.getvalue()
    assert "dependency_check=passed" in out.getvalue()
    assert "xgboost=" in out.getvalue()


def test_missing_xgboost_is_a_clear_environment_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "xgboost":
            raise ModuleNotFoundError("No module named 'xgboost'")
        return real_import(name, *args, **kwargs)

    import importlib
    import sys

    monkeypatch.delitem(sys.modules, "xgboost", raising=False)
    monkeypatch.setattr(importlib, "import_module", lambda n: fake_import(n) if n == "xgboost" else real_import(n))
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        with pytest.raises(ImportError) as exc:
            exec(_dependency_cell(), {"acelo_run_id": "run-1"})  # noqa: S102
    assert "Fabric Environment" in str(exc.value)
    assert "xgboost" in str(exc.value)
    assert "missing_library=xgboost" in out.getvalue()
    assert "dependency_check=failed" in out.getvalue()
