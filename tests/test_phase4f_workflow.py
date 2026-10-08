import json
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "phase4f_scale_cohort", Path(__file__).resolve().parents[1] / "scripts" / "phase4f_scale_cohort.py",
)
workflow = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(workflow)
chunked = workflow.chunked
write_checkpoint = workflow.write_checkpoint


def test_chunked_respects_batch_boundaries_and_keeps_tail():
    assert list(chunked(range(7), 3)) == [[0, 1, 2], [3, 4, 5], [6]]


def test_chunked_rejects_unbounded_or_invalid_batch_size():
    with pytest.raises(ValueError, match="positive"):
        list(chunked([1], 0))


def test_discovery_checkpoint_is_valid_json_and_replaced_atomically(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    monkeypatch.setattr(workflow, "CHECKPOINT", checkpoint)
    state = {"completed_categories": ["b2b"], "categories": {"b2b": {"status": "complete"}}}
    write_checkpoint(state)
    assert json.loads(checkpoint.read_text()) == state
    assert not checkpoint.with_suffix(".json.tmp").exists()
