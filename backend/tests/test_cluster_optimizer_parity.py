"""
Drift guard: optimizers/cluster_optimizer.py must stay the SAME algorithm as
optimization_package/cluster/Clusterfabric.ipynb (the Fabric-deployed copy).

The notebook runs on Spark, which is not available to the ACELO backend, so the
two cannot be executed side by side here. Instead this compares what defines
the algorithm: every numeric constant (weights, thresholds, quantiles, model
hyper-parameters), every label, the feature list, the warehouse encoding and
the specialised-workload pattern. Change one without the other and this fails.
"""

import ast
import inspect
import json
import re
from pathlib import Path

from optimizers import cluster_optimizer as opt

NOTEBOOK = Path(__file__).resolve().parents[1] / "optimization_package" / "cluster" / "Clusterfabric.ipynb"


def _notebook_optimizer_source() -> str:
    cells = json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
    source = "\n".join("".join(c["source"]) for c in cells if c["cell_type"] == "code")
    start = source.index("df = df.fillna({")
    end = source.index("def apply_predictive_llm_logic")
    return source[start:end]


def _numbers(tree: ast.AST) -> set[float]:
    return {
        float(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
    }


def _strings(tree: ast.AST) -> set[str]:
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _module_algorithm_tree() -> ast.Module:
    parts = [inspect.getsource(opt.score_clusters), inspect.getsource(opt.predict_savings)]
    constants = (
        f"NOTEBOOK_FILLNA = {opt.NOTEBOOK_FILLNA!r}\n"
        f"XGB_PARAMS = {opt.XGB_PARAMS!r}\n"
        f"WH_ORDER = {opt.WH_ORDER!r}\n"
    )
    return ast.parse(constants + "\n".join(parts))


# Spark-only arguments with no pandas counterpart: approxQuantile's relative
# error (0.01). Everything else must match exactly.
_SPARK_ONLY_NUMBERS = {0.01}


def test_numeric_constants_match_notebook():
    notebook = _numbers(ast.parse(_notebook_optimizer_source())) - _SPARK_ONLY_NUMBERS
    module = _numbers(_module_algorithm_tree())
    assert notebook - module == set(), f"in notebook only: {sorted(notebook - module)}"
    assert module - notebook == set(), f"in module only: {sorted(module - notebook)}"


def test_labels_match_notebook():
    label = re.compile(r"^[A-Z][A-Za-z]+( [A-Za-z()]+)*$")
    notebook = {s for s in _strings(ast.parse(_notebook_optimizer_source())) if label.match(s)}
    module = {s for s in _strings(ast.parse(inspect.getsource(opt.score_clusters))) if label.match(s)}
    # Warehouse sizes are compared in test_features_encoding_and_patterns_match_notebook.
    notebook -= set(opt.WH_ORDER)
    assert notebook == module


def test_features_encoding_and_patterns_match_notebook():
    tree = ast.parse(_notebook_optimizer_source())
    assigned = {
        node.targets[0].id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assert ast.literal_eval(assigned["FEATURES"]) == opt.FEATURES
    assert ast.literal_eval(assigned["WH_ORDER"]) == opt.WH_ORDER
    assert ast.literal_eval(assigned["specialized_workloads"]) == opt.SPECIALIZED_WORKLOADS


def test_required_columns_match_notebook():
    cells = json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
    source = "\n".join("".join(c["source"]) for c in cells if c["cell_type"] == "code")
    block = re.search(r"REQUIRED_COLUMNS = (\[.*?\])", source, re.S).group(1)
    assert ast.literal_eval(block) == opt.REQUIRED_COLUMNS


def test_llm_prompt_matches_notebook():
    source = NOTEBOOK.read_text(encoding="utf-8")
    cells = json.loads(source)["cells"]
    code = "\n".join("".join(c["source"]) for c in cells if c["cell_type"] == "code")
    notebook_prompt = re.search(r'prompt = f"""(.*?)"""', code, re.S).group(1)
    module_prompt = re.search(r'return f"""(.*?)"""', inspect.getsource(opt.remediation_prompt), re.S).group(1)
    assert notebook_prompt == module_prompt
