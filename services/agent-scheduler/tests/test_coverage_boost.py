"""Targeted tests to push coverage past 80%.

Focuses on:
- health.readyz endpoint
- DockerRuntime.get_run_status with result.json on disk
- artifact_store.upload_run_output_files empty workspace / missing s3 client
- DockerRuntime._make_s3_client failure-open path
- authz_guard B904 paths (unreachable in normal tests)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from agent_scheduler.services import artifact_store
from agent_scheduler.services.docker_runtime import DockerRuntime

# ---------------------------------------------------------------------------
# health.readyz
# ---------------------------------------------------------------------------


def test_readyz_returns_degraded_when_docker_unavailable(
    app_client: TestClient,
) -> None:
    """readyz returns DEGRADED when the docker daemon is unreachable."""
    import docker

    with patch.object(docker, "from_env", side_effect=Exception("daemon down")):
        resp = app_client.get("/api/scheduler/v1/readyz")

    assert resp.status_code == 200
    assert resp.json()["status"] == "DEGRADED"


def test_readyz_returns_ok_when_docker_available(
    app_client: TestClient,
) -> None:
    """readyz returns OK when docker.ping() succeeds."""
    import docker

    mock_client = MagicMock()
    mock_client.ping.return_value = True

    with patch.object(docker, "from_env", return_value=mock_client):
        resp = app_client.get("/api/scheduler/v1/readyz")

    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"


# ---------------------------------------------------------------------------
# DockerRuntime.get_run_status — with result.json on disk
# ---------------------------------------------------------------------------


def test_get_run_status_reads_result_json(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
) -> None:
    """When result.json exists in workspace, summary/usage are returned."""
    container = MagicMock()
    container.status = "exited"
    container.attrs = {
        "State": {"ExitCode": 0, "StartedAt": "2026-01-01", "FinishedAt": "2026-01-02"},
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tenant_id = "tnt_01J5TNT0000000000000000001"
        session_id = "ses_01J5SES0000000000000000001"
        run_id = "run_01J5RUN0000000000000000001"

        container.labels = {
            "agent-platform.tenant-id": tenant_id,
            "agent-platform.session-id": session_id,
            "agent-platform.run-id": run_id,
        }
        mock_docker_client.containers.get.return_value = container

        workspace = Path(tmpdir) / tenant_id / session_id / run_id
        workspace.mkdir(parents=True)
        result_data = {
            "summary": "task done",
            "error": None,
            "status": "COMPLETED",
            "usage": {"input_tokens": 5, "output_tokens": 10, "total_tokens": 15},
            "plan_steps": [{"step": 1, "done": True}],
        }
        (workspace / "result.json").write_text(json.dumps(result_data), encoding="utf-8")

        runtime._settings.workspace_base = tmpdir  # type: ignore[attr-defined]
        result = runtime.get_run_status(run_id)

    assert result["status"] == "COMPLETED"
    assert result["result_summary"] == "task done"
    assert result["plan_steps"] == [{"step": 1, "done": True}]


def test_get_run_status_result_json_failed_status_promotes(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
) -> None:
    """Worker-reported FAILED in result.json overrides docker exit-0."""
    container = MagicMock()
    container.status = "exited"
    container.attrs = {
        "State": {"ExitCode": 0, "StartedAt": "2026-01-01", "FinishedAt": "2026-01-02"},
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tenant_id = "tnt_01J5TNT0000000000000000001"
        session_id = "ses_01J5SES0000000000000000001"
        run_id = "run_01J5RUN0000000000000000002"

        container.labels = {
            "agent-platform.tenant-id": tenant_id,
            "agent-platform.session-id": session_id,
            "agent-platform.run-id": run_id,
        }
        mock_docker_client.containers.get.return_value = container

        workspace = Path(tmpdir) / tenant_id / session_id / run_id
        workspace.mkdir(parents=True)
        (workspace / "result.json").write_text(
            json.dumps({"status": "FAILED", "error": "tool error"}), encoding="utf-8"
        )

        runtime._settings.workspace_base = tmpdir  # type: ignore[attr-defined]
        result = runtime.get_run_status(run_id)

    assert result["status"] == "FAILED"


# ---------------------------------------------------------------------------
# artifact_store.upload_run_output_files — edge cases
# (DockerRuntime._upload_artifacts was folded into this shared helper.)
# ---------------------------------------------------------------------------


def test_upload_output_files_returns_empty_when_workspace_missing() -> None:
    """Non-existent workspace dir → empty lists, no S3 call."""
    s3 = MagicMock()
    result = artifact_store.upload_run_output_files(
        workspace=Path("/nonexistent/path"),
        tenant_id="tnt_test",
        session_id="ses_test",
        run_id="run_test",
        workspace_id="",
        bucket="agent-platform",
        s3_client=s3,
    )
    assert result == {"workspace_files": [], "artifacts": []}
    s3.put_object.assert_not_called()


def test_upload_output_files_returns_empty_when_s3_unavailable() -> None:
    """No S3 client (``make_s3_client`` returned None) → empty lists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "outputs").mkdir()
        (workspace / "outputs" / "output.txt").write_text("hello", encoding="utf-8")

        result = artifact_store.upload_run_output_files(
            workspace=workspace,
            tenant_id="tnt_test",
            session_id="ses_test",
            run_id="run_test",
            workspace_id="",
            bucket="agent-platform",
            s3_client=None,
        )

    assert result == {"workspace_files": [], "artifacts": []}


def _seed_scratch_and_output(workspace: Path) -> None:
    (workspace / "outputs").mkdir()
    (workspace / "outputs" / "report.md").write_text("# report", encoding="utf-8")
    (workspace / "scratch").mkdir()
    (workspace / "scratch" / "notes.txt").write_text("working notes", encoding="utf-8")


def test_upload_without_a_workspace_ships_only_outputs() -> None:
    """Nothing references a workspace-less run's other files, so they would sit
    in the bucket for good: only ``outputs/`` is uploaded."""
    s3 = MagicMock()
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        _seed_scratch_and_output(workspace)
        result = artifact_store.upload_run_output_files(
            workspace=workspace, tenant_id="tnt_test", session_id="ses_test",
            run_id="run_test", workspace_id="", bucket="agent-platform", s3_client=s3,
        )
    keys = [c.kwargs["Key"] for c in s3.put_object.call_args_list]
    assert keys == ["tenant/tnt_test/sessions/ses_test/runs/run_test/artifacts/outputs/report.md"]
    assert [a["filename"] for a in result["artifacts"]] == ["report.md"]
    assert result["workspace_files"] == []


def test_upload_with_a_workspace_ships_every_file_for_writeback() -> None:
    s3 = MagicMock()
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        _seed_scratch_and_output(workspace)
        result = artifact_store.upload_run_output_files(
            workspace=workspace, tenant_id="tnt_test", session_id="ses_test",
            run_id="run_test", workspace_id="wsp_test", bucket="agent-platform", s3_client=s3,
        )
    assert sorted(f["filename"] for f in result["workspace_files"]) == [
        "outputs/report.md", "scratch/notes.txt",
    ]
    assert [a["filename"] for a in result["artifacts"]] == ["report.md"]
    assert s3.put_object.call_count == 2


# ---------------------------------------------------------------------------
# DockerRuntime._make_s3_client — failure-open
# ---------------------------------------------------------------------------


def test_make_s3_client_returns_none_on_error(
    runtime: DockerRuntime,
) -> None:
    """boto3.client raising an exception → returns None (failure-open)."""
    import boto3

    with patch.object(boto3, "client", side_effect=Exception("no creds")):
        result = runtime._make_s3_client()  # type: ignore[attr-defined]

    assert result is None
