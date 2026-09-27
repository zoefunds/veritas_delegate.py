"""Regression tests for consequential verdict preservation in bucket jitter."""

import ast
from pathlib import Path


def _load_contract_module():
    path = Path(__file__).parents[1] / "contracts" / "veritas_delegate.py"
    tree = ast.parse(path.read_text())
    wanted = {"_bucket_to_verdict", "_buckets_preserve_verdict"}
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace = {
        "OUTCOME_ALIGNED": "ALIGNED",
        "OUTCOME_MISALIGNED": "MISALIGNED",
        "OUTCOME_INCONCLUSIVE": "INCONCLUSIVE",
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return type("ContractHelpers", (), namespace)


def test_adjacent_bucket_consensus_preserves_consequential_verdict():
    contract = _load_contract_module()

    assert contract._buckets_preserve_verdict(0, 1) is True
    assert contract._buckets_preserve_verdict(3, 4) is True
    assert contract._buckets_preserve_verdict(1, 2) is False
    assert contract._buckets_preserve_verdict(2, 3) is False


def test_non_adjacent_buckets_are_rejected_even_with_same_verdict():
    contract = _load_contract_module()

    assert contract._buckets_preserve_verdict(0, 2) is False
    assert contract._buckets_preserve_verdict(2, 4) is False
