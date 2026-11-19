from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "outreach_invitation_generation_tick.sh"
BLUEPRINT = ROOT / "render.invitation-cron.yaml"


def test_render_blueprint_is_scoped_to_invitation_worker_and_staging_service():
    blueprint = yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))
    assert len(blueprint["services"]) == 1
    service = blueprint["services"][0]
    assert service["type"] == "cron"
    assert service["schedule"] == "*/5 * * * *"
    assert service["plan"] == "0.5c-512mb"
    assert service["startCommand"] == "bash scripts/outreach_invitation_generation_tick.sh"
    assert service["autoDeploy"] is False
    env_vars = {item["key"]: item for item in service["envVars"]}
    assert env_vars["OUTREACH_WORKER_TOKEN"]["fromService"] == {
        "name": "geo-research-outreach-staging",
        "type": "web",
        "envVarKey": "OUTREACH_WORKER_TOKEN",
    }
    assert env_vars["OUTREACH_WORKER_URL"]["fromService"]["envVarKey"] == "RENDER_EXTERNAL_URL"
    assert not any("DATABASE" in key or "GEO_ENGINE" in key or "RESEARCH" in key for key in env_vars)


def _run_with_mock_curl(tmp_path: Path, response: str, url: str = "https://geo-research-outreach-staging.onrender.com"):
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    curl = mock_bin / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "out=''\n"
        "target=''\n"
        "while (($#)); do\n"
        "  if [[ $1 == --output ]]; then out=$2; shift 2;\n"
        "  elif [[ $1 == --request ]]; then [[ $2 == POST ]]; shift 2;\n"
        "  elif [[ $1 == --data ]]; then [[ $2 == '{}' ]]; shift 2;\n"
        "  elif [[ $1 == https://* ]]; then target=$1; shift;\n"
        "  else shift; fi\n"
        "done\n"
        "[[ $target == https://geo-research-outreach-staging.onrender.com/api/internal/invitation-generation-tick ]]\n"
        f"printf '%s' '{response}' > \"$out\"\n"
        "printf '200'\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{mock_bin}:{env['PATH']}",
            "OUTREACH_WORKER_URL": url,
            "OUTREACH_WORKER_TOKEN": "test-token-must-not-be-logged",
        }
    )
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)


def test_empty_queue_tick_reports_zero_chunks_without_logging_token(tmp_path: Path):
    result = _run_with_mock_curl(tmp_path, '{"status":"EMPTY","processed_chunks":0}')
    assert result.returncode == 0
    assert "status=EMPTY" in result.stdout
    assert "processed_chunks=0" in result.stdout
    assert "test-token-must-not-be-logged" not in result.stdout + result.stderr


def test_worker_attention_status_fails_for_admin_recovery(tmp_path: Path):
    result = _run_with_mock_curl(tmp_path, '{"status":"ATTENTION_REQUIRED","processed_chunks":1}')
    assert result.returncode != 0
    assert "inspect the Admin Invitations page" in result.stderr


def test_script_rejects_non_staging_target_before_request(tmp_path: Path):
    result = _run_with_mock_curl(
        tmp_path,
        '{"status":"EMPTY","processed_chunks":0}',
        url="https://example.com",
    )
    assert result.returncode == 2
    assert "approved Outreach staging host" in result.stderr
