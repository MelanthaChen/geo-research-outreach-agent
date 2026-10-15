import csv
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("phase6a_scale_cohort", ROOT / "scripts/phase6a_scale_cohort.py")
phase6a = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(phase6a)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_offline_report_and_validate_use_separate_537_snapshot(monkeypatch, tmp_path):
    baseline = ROOT / "data/phase4f_experimental.db"
    phase6a_copy = tmp_path / "phase6a_experimental.db"
    shutil.copy2(baseline, phase6a_copy)
    frozen_before = digest(ROOT / "data/phase3c1_validation_77.db")
    campaign_before = digest(ROOT / "data/phase5d_campaigns.db")
    monkeypatch.setattr(phase6a, "DB", phase6a_copy)
    monkeypatch.setattr(phase6a, "DATA", tmp_path)
    monkeypatch.setattr(phase6a, "BASELINE_DB", baseline)
    monkeypatch.setattr(phase6a, "FROZEN_DB", ROOT / "data/phase3c1_validation_77.db")
    monkeypatch.setattr(phase6a, "CAMPAIGN_DB", ROOT / "data/phase5d_campaigns.db")
    monkeypatch.setattr(phase6a, "WEBSITE_EXPORT", ROOT / "data/phase4f_website_verification.csv")
    monkeypatch.setattr(phase6a, "PRIOR_WEBSITE_EXPORT", ROOT / "data/phase4e_website_verification.csv")

    phase6a.report()
    outputs = [tmp_path / name for name in (
        "phase6a_expansion_progress.csv", "phase6a_source_yields.csv", "phase6a_company_quality_summary.csv"
    )]
    first_hashes = [digest(path) for path in outputs]
    progress = next(csv.DictReader(outputs[0].open(encoding="utf-8", newline="")))
    assert progress["baseline_companies"] == "537"
    assert progress["new_discovered_candidates"] == "0"
    assert progress["remaining_to_target"] == "4463"
    assert progress["website_identity_not_assessed_in_saved_exports"] == "77"

    phase6a.report()
    assert [digest(path) for path in outputs] == first_hashes
    phase6a.validate()
    assert digest(ROOT / "data/phase3c1_validation_77.db") == frozen_before
    assert digest(ROOT / "data/phase5d_campaigns.db") == campaign_before


@pytest.mark.parametrize("command", ["discover", "verify", "qualify", "discover-contacts", "resume"])
def test_network_processing_commands_fail_closed_until_permission_is_cleared(monkeypatch, command):
    monkeypatch.setattr("sys.argv", ["phase6a_scale_cohort.py", command, "--dry-run"])
    with pytest.raises(SystemExit) as error:
        phase6a.main()
    assert error.value.code == 2
