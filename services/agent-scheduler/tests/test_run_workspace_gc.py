"""Per-run host dir lifecycle: collection marker, cancel removal, reaper sweep.

The dir ``{workspace_base}/{tenant}/{session}/{run}`` holds the prompt, the
conversation history, downloaded attachments, agent output and a credentials
file, so it must go once the run's result is collected (after a short grace),
on cancel, and at the latest after ``workspace_max_age_seconds``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import docker
import httpx
import pytest

from agent_scheduler.config import Settings
from agent_scheduler.errors import ContainerNotFoundError
from agent_scheduler.services import cube_runtime as cr
from agent_scheduler.services.cube_runtime import CubeSandboxRuntime
from agent_scheduler.services.docker_runtime import DockerRuntime

TNT = "tnt_01J5TNT0000000000000000001"
SES = "ses_01J5SES0000000000000000009"
RUN = "run_01J5RUN0000000000000000009"
USR = "usr_01J5USR0000000000000000001"
LABELS = {
    "agent-platform.tenant-id": TNT,
    "agent-platform.session-id": SES,
    "agent-platform.run-id": RUN,
}
DAY = 86400.0
_S3 = "agent_scheduler.services.artifact_store.make_s3_client"


def _run_dir(base: Path, run_id: str = RUN) -> Path:
    run_dir = base / TNT / SES / run_id
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (run_dir / "task.json").write_text('{"task": "private prompt"}', encoding="utf-8")
    (run_dir / "outputs" / "report.md").write_text("# report", encoding="utf-8")
    return run_dir


def _marker(run_dir: Path) -> Path:
    return run_dir.parent / f".{run_dir.name}.collected"


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def _age_tree(root: Path, seconds: float) -> None:
    then = time.time() - seconds
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        os.utime(path, (then, then), follow_symlinks=False)
    os.utime(root, (then, then))


def _resp(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("GET", "/x"))


def _sweep(base: Path, **kw: Any) -> int:
    kw.setdefault("collected_grace_seconds", 600)
    kw.setdefault("max_age_seconds", 8 * DAY)
    return cr.sweep_run_workspaces(base, **kw)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def test_run_workspace_dir_requires_safe_ids(tmp_path: Path) -> None:
    assert cr.run_workspace_dir(tmp_path, TNT, SES, RUN) == tmp_path / TNT / SES / RUN
    for bad in ("..", "a/b", "", "x" * 41, None, 7, MagicMock()):
        assert cr.run_workspace_dir(tmp_path, TNT, SES, bad) is None
        assert cr.run_workspace_dir(tmp_path, bad, SES, RUN) is None


def test_gc_settings_default_and_override() -> None:
    assert cr.workspace_gc_settings(SimpleNamespace()) == (600.0, 8 * DAY)
    custom = SimpleNamespace(
        workspace_collected_grace_seconds=30, workspace_max_age_seconds=3600,
    )
    assert cr.workspace_gc_settings(custom) == (30.0, 3600.0)


def test_marker_is_written_once_outside_the_mount(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    cr.mark_run_collected(run_dir)
    marker = _marker(run_dir)
    assert marker.is_file()
    # Not inside /workspace: the sandbox cannot touch it, the output scan
    # never uploads it.
    assert not [p for p in run_dir.rglob("*") if p.name.endswith(".collected")]

    _age(marker, 1000)
    anchored = marker.stat().st_mtime
    cr.mark_run_collected(run_dir)  # a later poll does not restart the grace
    assert marker.stat().st_mtime == anchored

    cr.mark_run_collected(tmp_path / TNT / SES / "run_missing")
    assert not (tmp_path / TNT / SES / ".run_missing.collected").exists()
    cr.mark_run_collected(None)


def test_remove_never_follows_links(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    base = tmp_path / "ws"
    run_dir = _run_dir(base)
    (run_dir / "escape").symlink_to(outside, target_is_directory=True)
    cr.mark_run_collected(run_dir)

    assert cr.remove_run_workspace(base, run_dir) is True
    assert not run_dir.exists()
    assert not _marker(run_dir).exists()
    assert (outside / "keep.txt").exists()
    assert cr.remove_run_workspace(base, run_dir) is True  # already gone
    assert cr.remove_run_workspace(base, None) is False


def test_remove_refuses_a_dir_that_resolves_outside_base(tmp_path: Path) -> None:
    base = tmp_path / "ws"
    base.mkdir()
    victim = tmp_path / "elsewhere" / SES / RUN
    victim.mkdir(parents=True)
    (base / TNT).symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    assert cr.remove_run_workspace(base, base / TNT / SES / RUN) is False
    assert victim.is_dir()


# --------------------------------------------------------------------------- #
# sweep
# --------------------------------------------------------------------------- #


def test_sweep_removes_collected_after_grace_and_idle_after_max_age(tmp_path: Path) -> None:
    collected_old = _run_dir(tmp_path, "run_collected_old")
    cr.mark_run_collected(collected_old)
    _age(_marker(collected_old), 700)
    collected_new = _run_dir(tmp_path, "run_collected_new")
    cr.mark_run_collected(collected_new)
    idle = _run_dir(tmp_path, "run_idle")
    _age_tree(idle, 10 * DAY)
    paused = _run_dir(tmp_path, "run_paused")  # HITL pause: no marker, in window
    _age_tree(paused, 3 * DAY)
    live = _run_dir(tmp_path, "run_live")
    cr.mark_run_collected(live)
    _age(_marker(live), 700)

    assert _sweep(tmp_path, live_run_ids={"run_live"}) == 2

    remaining = sorted(
        p.name for p in (tmp_path / TNT / SES).iterdir() if not p.name.startswith(".")
    )
    assert remaining == ["run_collected_new", "run_live", "run_paused"]
    assert not _marker(collected_old).exists()
    assert _marker(live).exists()


def test_sweep_honours_disabled_thresholds(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    cr.mark_run_collected(run_dir)
    _age(_marker(run_dir), 10_000)
    _age_tree(run_dir, 30 * DAY)
    assert _sweep(tmp_path, collected_grace_seconds=-1, max_age_seconds=0) == 0
    assert run_dir.is_dir()
    assert _sweep(tmp_path, collected_grace_seconds=0, max_age_seconds=0) == 1


def test_sweep_skips_links_and_foreign_names(tmp_path: Path) -> None:
    base = tmp_path / "ws"
    target = tmp_path / "outside"
    target.mkdir()
    (target / "f.txt").write_text("x", encoding="utf-8")
    session = base / TNT / SES
    session.mkdir(parents=True)
    (session / "run_link").symlink_to(target, target_is_directory=True)
    foreign = session / "not a run id"
    foreign.mkdir()
    _age_tree(base, 30 * DAY)

    assert _sweep(base, collected_grace_seconds=0, max_age_seconds=1) == 0
    assert (target / "f.txt").exists()
    assert (session / "run_link").is_symlink()
    assert foreign.is_dir()


def test_sweep_tidies_orphan_markers_and_idle_empty_dirs(tmp_path: Path) -> None:
    session = tmp_path / TNT / SES
    session.mkdir(parents=True)
    orphan = session / ".run_gone.collected"
    orphan.touch()
    fresh = tmp_path / "tnt_fresh" / "ses_fresh"  # a spawn may be filling it
    fresh.mkdir(parents=True)

    _sweep(tmp_path)
    assert not orphan.exists()

    _age(session, 120)
    _sweep(tmp_path)
    assert not session.exists()

    _age(tmp_path / TNT, 120)
    _sweep(tmp_path)
    assert not (tmp_path / TNT).exists()
    assert fresh.is_dir()


def test_sweep_of_missing_base_is_a_no_op(tmp_path: Path) -> None:
    assert _sweep(tmp_path / "missing") == 0


# --------------------------------------------------------------------------- #
# docker runtime
# --------------------------------------------------------------------------- #


@pytest.fixture
def docker_rt(runtime: DockerRuntime, tmp_path: Path) -> DockerRuntime:
    runtime._settings.workspace_base = str(tmp_path)  # noqa: SLF001
    return runtime


def _container(status: str = "exited", exit_code: int = 0, **labels: str) -> MagicMock:
    return MagicMock(
        status=status,
        labels={**LABELS, **labels},
        attrs={"State": {"ExitCode": exit_code, "StartedAt": "a", "FinishedAt": "b"}},
    )


@pytest.mark.parametrize(
    ("status", "worker_status", "marked"),
    [
        ("exited", "OK", True),
        ("exited", "FAILED", True),
        ("exited", "paused", False),  # resume reuses the dir
        ("running", "RUNNING", False),
    ],
)
def test_docker_poll_marks_collection_only_when_final(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
    status: str, worker_status: str, marked: bool,
) -> None:
    run_dir = _run_dir(tmp_path)
    (run_dir / "result.json").write_text(
        json.dumps({"status": worker_status, "summary": "done"}), encoding="utf-8",
    )
    mock_docker_client.containers.get.return_value = _container(status)
    with patch(_S3, return_value=MagicMock()):
        docker_rt.get_run_status("c1")
    assert _marker(run_dir).exists() is marked
    assert run_dir.is_dir()  # removal waits for the grace


def test_docker_poll_with_unlabelled_container_marks_nothing(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    mock_docker_client.containers.get.return_value = _container(
        **{"agent-platform.run-id": "../escape"},
    )
    with patch(_S3, return_value=MagicMock()):
        docker_rt.get_run_status("c1")
    assert not list(run_dir.parent.glob(".*.collected"))


def test_docker_cancel_removes_the_run_workspace(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    cr.mark_run_collected(run_dir)
    container = _container("running")
    # The labels go with the container: they must be read before removal.
    container.remove.side_effect = lambda: setattr(container, "labels", None)
    mock_docker_client.containers.get.return_value = container
    mock_docker_client.containers.list.return_value = [
        MagicMock(status="exited", labels={"agent-platform.run-id": RUN}),
    ]

    docker_rt.cancel_run("c1")

    container.remove.assert_called_once()
    assert not run_dir.exists()
    assert not _marker(run_dir).exists()


def test_docker_cancel_can_leave_the_workspace_to_the_reaper(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    container = _container("exited")
    mock_docker_client.containers.get.return_value = container
    mock_docker_client.containers.list.return_value = []

    docker_rt.cancel_run("c1", remove_workspace=False)

    container.remove.assert_called_once()
    assert run_dir.exists()


def test_docker_cancel_keeps_a_workspace_another_container_uses(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    mock_docker_client.containers.get.return_value = _container("exited")
    mock_docker_client.containers.list.return_value = [
        MagicMock(status="running", labels={"agent-platform.run-id": RUN}),
    ]
    docker_rt.cancel_run("stale-container")
    assert run_dir.is_dir()


def test_docker_cancel_keeps_the_workspace_when_liveness_is_unknown(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    mock_docker_client.containers.get.return_value = _container("running")
    mock_docker_client.containers.list.side_effect = docker.errors.APIError("down")
    docker_rt.cancel_run("c1")
    assert run_dir.is_dir()


def test_docker_cancel_ignores_malformed_labels(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    mock_docker_client.containers.get.return_value = _container(
        "running", **{"agent-platform.session-id": "../.."},
    )
    docker_rt.cancel_run("c1")
    mock_docker_client.containers.list.assert_not_called()
    assert run_dir.is_dir()


def test_docker_reaper_sweeps_and_spares_live_runs(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    done = _run_dir(tmp_path, "run_done")
    cr.mark_run_collected(done)
    _age(_marker(done), 700)
    live = _run_dir(tmp_path, "run_live")
    cr.mark_run_collected(live)
    _age(_marker(live), 700)
    mock_docker_client.containers.list.return_value = [
        MagicMock(status="running", labels={"agent-platform.run-id": "run_live"}),
        MagicMock(status="exited", labels={"agent-platform.run-id": "run_done"}),
        MagicMock(status="running", labels=MagicMock()),  # tolerated
    ]

    assert docker_rt.reap_expired(max_age_seconds=3600) == 0  # no containers

    assert not done.exists()
    assert live.is_dir()
    _, kwargs = mock_docker_client.containers.list.call_args
    assert kwargs == {"all": True, "filters": {"label": "agent-platform.run-id"}}


def _stopped(seconds_ago: float, *, status: str = "exited", run_id: str = RUN) -> MagicMock:
    """A finished container whose Docker timestamps say it stopped that long ago."""
    from datetime import UTC, datetime, timedelta

    moment = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"  # docker: nanoseconds
    finished = "0001-01-01T00:00:00Z" if status == "created" else stamp
    return MagicMock(
        id=f"ctr_{run_id}",
        status=status,
        labels={**LABELS, "agent-platform.run-id": run_id},
        attrs={"Created": stamp, "State": {"ExitCode": 0, "FinishedAt": finished}},
    )


def test_docker_reaper_removes_collected_containers_with_their_dir(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    # The stopped container's layer holds the SDK transcript (~/.claude), so it
    # goes on the run dir's schedule: collected and past the grace.
    done = _run_dir(tmp_path, "run_done")
    cr.mark_run_collected(done)
    _age(_marker(done), 700)
    fresh = _run_dir(tmp_path, "run_fresh")
    cr.mark_run_collected(fresh)  # collected just now: still in its grace
    finished, recent = _stopped(700, run_id="run_done"), _stopped(700, run_id="run_fresh")
    mock_docker_client.containers.list.return_value = [finished, recent]

    assert docker_rt.reap_expired(max_age_seconds=3600) == 1

    finished.remove.assert_called_once_with(force=True)
    recent.remove.assert_not_called()
    assert not done.exists()
    assert fresh.is_dir()


def test_docker_reaper_keeps_a_paused_runs_container_until_max_age(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    _run_dir(tmp_path, "run_paused")  # never collected: the resume reuses it
    _run_dir(tmp_path, "run_stale")
    paused = _stopped(2 * 3600, run_id="run_paused")
    stale = _stopped(9 * DAY, run_id="run_stale")
    running = _stopped(9 * DAY, status="running", run_id="run_live")
    mock_docker_client.containers.list.return_value = [paused, stale, running]

    assert docker_rt.reap_expired(max_age_seconds=3600) == 1

    paused.remove.assert_not_called()
    stale.remove.assert_called_once_with(force=True)
    running.remove.assert_not_called()


def test_docker_reaper_removes_containers_whose_dir_is_gone(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock,
) -> None:
    gone, just_stopped = _stopped(700, run_id="run_gone"), _stopped(30, run_id="run_new")
    never_started = _stopped(700, status="created", run_id="run_created")
    mock_docker_client.containers.list.return_value = [gone, just_stopped, never_started]

    assert docker_rt.reap_expired(max_age_seconds=3600) == 2

    gone.remove.assert_called_once_with(force=True)
    never_started.remove.assert_called_once_with(force=True)
    just_stopped.remove.assert_not_called()


def test_docker_reaper_tolerates_unreadable_and_vanishing_containers(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock,
) -> None:
    unreadable = _stopped(9 * DAY, run_id="run_odd")
    unreadable.attrs = {"State": {"FinishedAt": "yesterday"}}
    vanished = _stopped(9 * DAY, run_id="run_vanished")
    vanished.remove.side_effect = docker.errors.NotFound("gone")
    refused = _stopped(9 * DAY, run_id="run_refused")
    refused.remove.side_effect = docker.errors.APIError("busy")
    removable = _stopped(9 * DAY, run_id="run_ok")
    mock_docker_client.containers.list.return_value = [unreadable, vanished, refused, removable]

    assert docker_rt.reap_expired(max_age_seconds=3600) == 1

    unreadable.remove.assert_not_called()
    removable.remove.assert_called_once_with(force=True)


def test_docker_reaper_skips_the_sweep_when_the_daemon_is_unreachable(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    done = _run_dir(tmp_path)
    cr.mark_run_collected(done)
    _age(_marker(done), 700)
    mock_docker_client.containers.list.side_effect = docker.errors.APIError("down")
    assert docker_rt.reap_expired(max_age_seconds=3600) == 0
    assert done.is_dir()


def test_docker_spawn_clears_a_stale_marker(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    run_dir = tmp_path / TNT / SES / RUN
    run_dir.mkdir(parents=True)
    cr.mark_run_collected(run_dir)
    mock_docker_client.containers.run.return_value = MagicMock(id="container-xyz")
    docker_rt.spawn_run(
        run_id=RUN, session_id=SES, task="t", tenant_id=TNT, user_id=USR,
        foundation_token="tok", llm_virtual_key="vk-test",
    )
    assert (run_dir / "task.json").is_file()
    assert not _marker(run_dir).exists()


_USAGE = {"input_tokens": 900, "output_tokens": 300, "total_tokens": 1200}


def _write_result(run_dir: Path, status: str, usage: dict[str, int] | None = None) -> None:
    payload = {"status": status, "summary": "", "usage": usage or _USAGE, "plan_steps": []}
    (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")


def test_docker_cancel_returns_the_usage_the_worker_saved_on_sigterm(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    _write_result(run_dir, "RUNNING", {**_USAGE, "input_tokens": 100})
    container = _container("running")
    # The worker writes its final counts while it handles the stop's SIGTERM.
    container.stop.side_effect = lambda **kw: _write_result(run_dir, "FAILED")
    mock_docker_client.containers.get.return_value = container
    mock_docker_client.containers.list.return_value = []

    assert docker_rt.cancel_run("c1") == _USAGE
    # ...read before the workspace (and the file with it) is removed.
    assert not run_dir.exists()


@pytest.mark.parametrize("content", [None, "{not json", '{"usage": "lots"}', "[]"])
def test_docker_cancel_without_readable_usage_returns_none(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
    content: str | None,
) -> None:
    run_dir = _run_dir(tmp_path)
    if content is not None:
        (run_dir / "result.json").write_text(content, encoding="utf-8")
    mock_docker_client.containers.get.return_value = _container("running")
    mock_docker_client.containers.list.return_value = []
    assert docker_rt.cancel_run("c1") is None


def test_docker_cancel_kept_workspace_still_reports_usage(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    _write_result(run_dir, "paused")
    mock_docker_client.containers.get.return_value = _container("exited")
    assert docker_rt.cancel_run("c1", remove_workspace=False) == _USAGE
    assert run_dir.is_dir()


def test_docker_spawn_drops_the_previous_legs_result(
    docker_rt: DockerRuntime, mock_docker_client: MagicMock, tmp_path: Path,
) -> None:
    # A resume spawns into the paused leg's dir; its result.json must not pass
    # for the new leg's.
    run_dir = _run_dir(tmp_path)
    _write_result(run_dir, "paused")
    mock_docker_client.containers.run.return_value = MagicMock(id="container-xyz")
    docker_rt.spawn_run(
        run_id=RUN, session_id=SES, task="t", tenant_id=TNT, user_id=USR,
        foundation_token="tok", llm_virtual_key="vk-test",
    )
    assert not (run_dir / "result.json").exists()
    assert (run_dir / "outputs" / "report.md").is_file()


# --------------------------------------------------------------------------- #
# cube runtime
# --------------------------------------------------------------------------- #


def _cube(client: object, base: Path) -> CubeSandboxRuntime:
    settings = Settings(
        env="test",
        log_level="WARNING",
        otel_enabled=False,
        runtime="cube",
        cube_controller_url="http://cube-test:8443",
        sandbox_egress_mode="foundation",
        cube_sandbox_allow_out="",
        workspace_base=str(base),
    )
    rt = CubeSandboxRuntime.__new__(CubeSandboxRuntime)
    rt._settings = settings  # noqa: SLF001
    rt._client = client  # noqa: SLF001
    rt._skill_cache = None  # noqa: SLF001
    rt._public_host = "203.0.113.10"  # noqa: SLF001
    rt._exec_start_worker = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    return rt


def _vm(sandbox_id: str, started_at: str | None, **meta: str) -> dict[str, Any]:
    vm: dict[str, Any] = {"sandboxID": sandbox_id, "metadata": {**LABELS, **meta}}
    if started_at is not None:
        vm["startedAt"] = started_at
    return vm


@pytest.mark.parametrize(
    ("worker_status", "marked"),
    [("OK", True), ("FAILED", True), ("paused", False), ("RUNNING", False)],
)
def test_cube_poll_marks_collection_only_when_final(
    tmp_path: Path, worker_status: str, marked: bool,
) -> None:
    client = MagicMock()
    client.get.return_value = _resp({"state": "running", "metadata": LABELS})
    rt = _cube(client, tmp_path)
    run_dir = _run_dir(tmp_path)
    (run_dir / "result.json").write_text(
        json.dumps({"status": worker_status, "summary": "s"}), encoding="utf-8",
    )
    with patch(_S3, return_value=MagicMock()):
        rt.get_run_status("sb_1")
    assert _marker(run_dir).exists() is marked
    assert run_dir.is_dir()


@pytest.mark.parametrize(
    ("other_started", "kept"),
    [
        (None, False),  # only the cancelled VM
        ("2026-10-30T09:00:00Z", False),  # an older VM (pre-resume) does not own it
        ("2026-10-30T11:00:00Z", True),  # the resumed VM owns it now
    ],
)
def test_cube_cancel_resolves_first_then_removes_the_workspace(
    tmp_path: Path, other_started: str | None, kept: bool,
) -> None:
    cancelled = _vm("sb_full_1", "2026-10-30T10:00:00Z")
    listing = [cancelled]
    if other_started is not None:
        listing.append(_vm("sb_full_2", other_started))
    calls: list[tuple[str, str]] = []

    def _get(url: str, **kw: Any) -> httpx.Response:
        calls.append(("GET", url))
        return _resp(cancelled) if url == "/sandboxes/sb_1" else _resp(listing)

    def _delete(url: str, **kw: Any) -> httpx.Response:
        calls.append(("DELETE", url))
        return _resp({}, status=204)

    client = MagicMock()
    client.get.side_effect = _get
    client.delete.side_effect = _delete
    rt = _cube(client, tmp_path)
    run_dir = _run_dir(tmp_path)

    rt.cancel_run("sb_1")

    assert calls.index(("GET", "/sandboxes/sb_1")) < calls.index(("DELETE", "/sandboxes/sb_1"))
    assert run_dir.is_dir() is kept


@pytest.mark.parametrize(
    ("cancelled", "listing"),
    [
        (_vm("sb_full_1", "2026-10-30T10:00:00Z"), _resp({}, status=500)),
        (_vm("sb_full_1", None), _resp([])),  # age of the cancelled VM unknown
    ],
)
def test_cube_cancel_keeps_the_workspace_when_ownership_is_unknown(
    tmp_path: Path, cancelled: dict[str, Any], listing: httpx.Response,
) -> None:
    client = MagicMock()
    client.get.side_effect = (
        lambda url, **kw: _resp(cancelled) if url == "/sandboxes/sb_1" else listing
    )
    client.delete.return_value = _resp({}, status=204)
    rt = _cube(client, tmp_path)
    run_dir = _run_dir(tmp_path)
    rt.cancel_run("sb_1")
    assert run_dir.is_dir()


def test_cube_cancel_of_unknown_vm_still_raises_and_keeps_files(tmp_path: Path) -> None:
    client = MagicMock()
    client.get.return_value = _resp(_vm("sb_full_1", "2026-10-30T10:00:00Z"))
    client.delete.return_value = _resp({}, status=404)
    rt = _cube(client, tmp_path)
    run_dir = _run_dir(tmp_path)
    with pytest.raises(ContainerNotFoundError):
        rt.cancel_run("sb_1")
    assert run_dir.is_dir()


def test_cube_reaper_counts_vms_and_sweeps_dirs(tmp_path: Path) -> None:
    client = MagicMock()
    client.get.return_value = _resp([_vm("sb_old", "2020-01-01T00:00:00Z")])
    client.delete.return_value = _resp({}, status=204)
    rt = _cube(client, tmp_path)
    done = _run_dir(tmp_path)
    cr.mark_run_collected(done)
    _age(_marker(done), 700)

    assert rt.reap_expired(max_age_seconds=900) == 1
    client.delete.assert_called_once_with("/sandboxes/sb_old")
    assert not done.exists()


def test_cube_reaper_sweeps_dirs_even_when_the_controller_is_down(tmp_path: Path) -> None:
    client = MagicMock()
    client.get.side_effect = httpx.ConnectError("down")
    rt = _cube(client, tmp_path)
    done = _run_dir(tmp_path)
    cr.mark_run_collected(done)
    _age(_marker(done), 700)
    with pytest.raises(httpx.ConnectError):
        rt.reap_expired(max_age_seconds=900)
    assert not done.exists()


def test_cube_spawn_clears_a_stale_marker(tmp_path: Path) -> None:
    client = MagicMock()
    client.post.return_value = _resp({"sandboxID": "sb_new"}, status=201)
    rt = _cube(client, tmp_path)
    run_dir = tmp_path / TNT / SES / RUN
    run_dir.mkdir(parents=True)
    cr.mark_run_collected(run_dir)
    rt.spawn_run(
        run_id=RUN, session_id=SES, task="t", tenant_id=TNT, user_id=USR,
        foundation_token="tok", llm_virtual_key="vk-test",
    )
    assert (run_dir / ".secrets.json").is_file()
    assert not _marker(run_dir).exists()


def test_cube_spawn_drops_the_previous_legs_result(tmp_path: Path) -> None:
    # Otherwise the first poll of the resumed VM reads the paused leg's file
    # and reports the run paused again.
    client = MagicMock()
    client.post.return_value = _resp({"sandboxID": "sb_new"}, status=201)
    rt = _cube(client, tmp_path)
    run_dir = _run_dir(tmp_path)
    _write_result(run_dir, "paused")
    rt.spawn_run(
        run_id=RUN, session_id=SES, task="t", tenant_id=TNT, user_id=USR,
        foundation_token="tok", llm_virtual_key="vk-test",
    )
    assert not (run_dir / "result.json").exists()


@pytest.mark.parametrize("remove_workspace", [True, False])
def test_cube_cancel_returns_the_reported_usage_before_removing_the_dir(
    tmp_path: Path, remove_workspace: bool,
) -> None:
    cancelled = _vm("sb_full_1", "2026-10-30T10:00:00Z")
    client = MagicMock()
    client.get.side_effect = (
        lambda url, **kw: _resp(cancelled) if url == "/sandboxes/sb_1" else _resp([cancelled])
    )
    client.delete.return_value = _resp({}, status=204)
    rt = _cube(client, tmp_path)
    run_dir = _run_dir(tmp_path)
    _write_result(run_dir, "RUNNING")

    assert rt.cancel_run("sb_1", remove_workspace=remove_workspace) == _USAGE
    assert run_dir.is_dir() is not remove_workspace


def test_cube_cancel_of_a_vm_without_metadata_returns_no_usage(tmp_path: Path) -> None:
    client = MagicMock()
    client.get.return_value = _resp({"sandboxID": "sb_full_1", "metadata": {}})
    client.delete.return_value = _resp({}, status=204)
    rt = _cube(client, tmp_path)
    _write_result(_run_dir(tmp_path), "RUNNING")
    assert rt.cancel_run("sb_1") is None
