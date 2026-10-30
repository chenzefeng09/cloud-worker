"""Tests for the foundation-DLP client (api/dlp_guard.py) and its call sites.

Decision table (the worker's PreToolUse hook, worker-claude-sdk/src/dlp.ts,
applies the same one to tool calls):

  dlp_url empty .......................... allowed, no DLP call, policy logged once
  DLP answers ............................ its verdict applies (download allowed
                                           true/false; an upload-scan hit labels
                                           the file and never rejects it)
  the call cannot complete (network error, timeout, non-2xx, a body that is not
  a JSON object, a missing or ill-typed verdict field):
    DLP_FAILURE_MODE=closed (default) .... DlpUnavailableError, HTTP 503, nothing stored
    DLP_FAILURE_MODE=open ................ proceeds, ERROR log naming the resource
  persisted output (scan_and_tag) ........ never raises in either mode; ERROR log
"""

from __future__ import annotations

import datetime as dt
import io
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_session_orchestrator.api import dlp_guard
from agent_session_orchestrator.api.dlp_guard import (
    DlpDownloadDeniedError,
    DlpUnavailableError,
    FailureMode,
    check_download,
    configured_failure_mode,
    log_dlp_policy,
    parse_failure_mode,
    scan_and_tag,
    scan_upload,
)
from agent_session_orchestrator.api.v1.attachments import _DLP_SCAN_MAX_BYTES, _maybe_text
from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.domain.models import Attachment, Session, Workspace, WorkspaceFile
from tests._helpers import TEST_TENANT, TEST_USER, AuthzAwareClient, make_scalar_result

_LOGGER = "agent_session_orchestrator.api.dlp_guard"
_ENV = "SESSION_ORCHESTRATOR_DLP_FAILURE_MODE"
_DLP = "http://dlp.test/api/dlp/v1"
_NOW = dt.datetime(2026, 6, 3, tzinfo=dt.UTC)
_MODES: tuple[FailureMode, ...] = ("closed", "open")

Answer = Callable[[str], httpx.Response]


@dataclass
class _DlpStub:
    """Answers DLP calls (URLs containing ``/core/``); authz calls are allowed."""

    answer: Answer = field(default=lambda url: httpx.Response(200, json={}))
    calls: list[dict[str, Any]] = field(default_factory=list)
    timeouts: list[Any] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def client_class(self) -> type[AuthzAwareClient]:
        stub = self

        class _Client(AuthzAwareClient):
            def __init__(self, *args: Any, timeout: Any = None, **kw: Any) -> None:
                stub.timeouts.append(timeout)

            async def post(self, url: str, **kw: Any) -> httpx.Response:
                if "/core/" not in url:
                    return await super().post(url, **kw)
                stub.calls.append(
                    {"url": url, "json": kw.get("json"), "headers": kw.get("headers")},
                )
                stub.events.append(f"dlp:{url.rsplit('/', 1)[-1]}")
                return stub.answer(url)

        return _Client


@pytest.fixture(autouse=True)
def _fresh_policy(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts with an unset failure mode and nothing logged yet."""
    monkeypatch.delenv(_ENV, raising=False)
    configured_failure_mode.cache_clear()
    dlp_guard._logged_policies.clear()
    yield
    configured_failure_mode.cache_clear()
    dlp_guard._logged_policies.clear()


@pytest.fixture
def dlp(monkeypatch: pytest.MonkeyPatch, mock_authz_allowed: None) -> _DlpStub:
    """Install the stub as ``httpx.AsyncClient`` (after conftest's authz fake)."""
    stub = _DlpStub()
    monkeypatch.setattr(httpx, "AsyncClient", stub.client_class())
    return stub


@pytest.fixture
def settings() -> Settings:
    """Overrides conftest: the call-site tests below run with DLP configured."""
    return Settings(
        env="test", log_level="WARNING", otel_enabled=False, foundation_dlp_url=_DLP,
    )


def _json(status: int, body: Any) -> Answer:
    return lambda url: httpx.Response(status, json=body)


def _raw(status: int, content: bytes) -> Answer:
    return lambda url: httpx.Response(status, content=content)


def _raise(exc: Exception) -> Answer:
    def answer(url: str) -> httpx.Response:
        raise exc

    return answer


def _configure_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """As if the service had been started with ``DLP_FAILURE_MODE=mode``.

    The mode is read once per process, and an ``app_client`` has already read
    it at startup, so the cached value is dropped as well.
    """
    monkeypatch.setenv(_ENV, mode)
    configured_failure_mode.cache_clear()


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.name == _LOGGER and r.levelno >= logging.ERROR
    ]


# Failures shared by every DLP call: (answer, fragment expected in the log).
_TRANSPORT_FAILURES: dict[str, tuple[Answer, str]] = {
    "network error": (_raise(httpx.ConnectError("connection refused")), "ConnectError"),
    "timeout": (_raise(httpx.ReadTimeout("timed out")), "no answer within 2s"),
    "403 problem": (
        _json(403, {"code": "FOUNDATION.DLP.POLICY.DLP_DENIED", "detail": "nope"}),
        "HTTP 403 FOUNDATION.DLP.POLICY.DLP_DENIED",
    ),
    "401 problem without code": (
        _json(401, {"detail": "tenant context missing"}),
        "HTTP 401 tenant context missing",
    ),
    "502 not JSON": (_raw(502, b"<html>bad gateway</html>"), "HTTP 502"),
    "2xx not JSON": (_raw(200, b"not json"), "not JSON"),
    "2xx not an object": (_json(200, ["ALLOW"]), "not an object"),
}

_DOWNLOAD_FAILURES: dict[str, tuple[Answer, str]] = {
    **_TRANSPORT_FAILURES,
    "2xx without allowed": (_json(200, {"sensitivity": "L3"}), "boolean 'allowed'"),
    "allowed not a boolean": (_json(200, {"allowed": "true"}), "boolean 'allowed'"),
}

_SCAN_FAILURES: dict[str, tuple[Answer, str]] = {
    "2xx without action": (_json(200, {"hits": []}), "string 'action'"),
    "action not a string": (_json(200, {"action": 1}), "string 'action'"),
    "empty action": (_json(200, {"action": ""}), "string 'action'"),
    **_TRANSPORT_FAILURES,
}


# --------------------------------------------------------------------------- #
# Failure-mode setting and the once-only policy log
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("open", "open"),
        (" OPEN ", "open"),
        ("closed", "closed"),
        ("Closed", "closed"),
        ("", "closed"),
        (None, "closed"),
        ("fail-open", None),
        ("true", None),
    ],
)
def test_parse_failure_mode(raw: str | None, expected: str | None) -> None:
    assert parse_failure_mode(raw) == expected


@pytest.mark.parametrize(
    ("env", "expected", "logs_error"),
    [
        (None, "closed", False),
        ("closed", "closed", False),
        ("open", "open", False),
        ("bogus", "closed", True),
    ],
)
def test_configured_failure_mode_reads_the_env(
    env: str | None,
    expected: str,
    logs_error: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    if env is not None:
        monkeypatch.setenv(_ENV, env)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert configured_failure_mode() == expected
    errors = _errors(caplog)
    assert bool(errors) is logs_error
    if logs_error:
        assert _ENV in errors[0] and "'closed'" in errors[0]


@pytest.mark.parametrize(
    ("dlp_url", "mode", "level", "fragment"),
    [
        ("", "closed", logging.WARNING, "DLP disabled"),
        (_DLP, "closed", logging.INFO, "refused with HTTP 503"),
        (_DLP, "open", logging.WARNING, "UNCHECKED"),
    ],
)
def test_policy_is_logged_once(
    dlp_url: str,
    mode: FailureMode,
    level: int,
    fragment: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        log_dlp_policy(dlp_url, mode)
        log_dlp_policy(dlp_url, mode)
    records = [r for r in caplog.records if r.name == _LOGGER]
    assert len(records) == 1
    assert records[0].levelno == level
    assert fragment in records[0].getMessage()


@pytest.mark.asyncio
async def test_dlp_disabled_allows_everything_without_calling_dlp(
    dlp: _DlpStub, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await check_download(
            dlp_url="", tenant_id=TEST_TENANT, user_id=TEST_USER, resource_id="att_1",
        )
        assert await scan_upload(
            dlp_url="", tenant_id=TEST_TENANT, user_id=TEST_USER,
            content="ID 110101199003077777", resource_id="att_1", resource_type="attachment",
        ) is None
        await scan_and_tag(
            dlp_url="", tenant_id=TEST_TENANT, content="x", resource_id="msg_1",
            resource_type="transcript",
        )
    assert dlp.calls == []
    assert dlp.timeouts == []  # no client was even built
    messages = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
    assert len(messages) == 1  # logged once, not per call
    assert "DLP disabled" in messages[0]


# --------------------------------------------------------------------------- #
# check_download
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_check_download_allowed_sends_trusted_identity_only(dlp: _DlpStub) -> None:
    dlp.answer = _json(200, {"allowed": True, "sensitivity": "L3", "reason": None})
    await check_download(
        dlp_url=_DLP + "/", tenant_id=TEST_TENANT, user_id=TEST_USER,
        resource_id="att_1", resource_type="workspace_file",
    )
    assert dlp.calls == [{
        "url": f"{_DLP}/core/check:download",
        # DLP resolves the clearance itself and rejects any extra field.
        "json": {
            "resource_type": "workspace_file",
            "resource_id": "att_1",
            "destination": "laptop",
        },
        "headers": {"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER},
    }]
    assert dlp.timeouts == [2.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _MODES)
async def test_check_download_denied_raises_in_both_modes(
    mode: FailureMode, dlp: _DlpStub,
) -> None:
    dlp.answer = _json(200, {"allowed": False, "sensitivity": "L4", "reason": "Clearance L2 < L4"})
    with pytest.raises(DlpDownloadDeniedError) as ei:
        await check_download(
            dlp_url=_DLP, tenant_id=TEST_TENANT, user_id=TEST_USER,
            resource_id="att_1", failure_mode=mode,
        )
    assert ei.value.reason == "Clearance L2 < L4"


@pytest.mark.asyncio
async def test_check_download_denied_without_reason_gets_a_generic_one(dlp: _DlpStub) -> None:
    dlp.answer = _json(200, {"allowed": False})
    with pytest.raises(DlpDownloadDeniedError) as ei:
        await check_download(
            dlp_url=_DLP, tenant_id=TEST_TENANT, user_id=TEST_USER, resource_id="att_1",
        )
    assert ei.value.reason == "blocked by data-loss-prevention policy"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_DOWNLOAD_FAILURES))
async def test_check_download_cannot_complete_closed_refuses(
    case: str, dlp: _DlpStub, caplog: pytest.LogCaptureFixture,
) -> None:
    dlp.answer, fragment = _DOWNLOAD_FAILURES[case]
    with caplog.at_level(logging.INFO, logger=_LOGGER), pytest.raises(DlpUnavailableError) as ei:
        await check_download(
            dlp_url=_DLP, tenant_id=TEST_TENANT, user_id=TEST_USER,
            resource_id="att_42", failure_mode="closed",
        )
    err = ei.value
    assert (err.status, err.code, err.operation) == (503, "AGENT.DLP.UNAVAILABLE", "download")
    # What the user sees carries no technical detail.
    for text in (err.detail, err.user_message):
        assert "dlp.test" not in text and fragment not in text
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "att_42" in errors[0] and fragment in errors[0]
    assert "DLP_FAILURE_MODE=closed" in errors[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_DOWNLOAD_FAILURES))
async def test_check_download_cannot_complete_open_allows_loudly(
    case: str, dlp: _DlpStub, caplog: pytest.LogCaptureFixture,
) -> None:
    dlp.answer, fragment = _DOWNLOAD_FAILURES[case]
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await check_download(
            dlp_url=_DLP, tenant_id=TEST_TENANT, user_id=TEST_USER,
            resource_id="att_42", failure_mode="open",
        )
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "att_42" in errors[0] and fragment in errors[0]
    assert "UNCHECKED" in errors[0] and "DLP_FAILURE_MODE=open" in errors[0]


@pytest.mark.asyncio
async def test_check_download_defaults_to_the_configured_mode(
    dlp: _DlpStub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dlp.answer = _raise(httpx.ConnectError("refused"))
    with pytest.raises(DlpUnavailableError):  # unset → closed
        await check_download(
            dlp_url=_DLP, tenant_id=TEST_TENANT, user_id=TEST_USER, resource_id="att_1",
        )
    _configure_mode(monkeypatch, "open")
    await check_download(
        dlp_url=_DLP, tenant_id=TEST_TENANT, user_id=TEST_USER, resource_id="att_1",
    )


# --------------------------------------------------------------------------- #
# scan_upload
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_scan_upload_clean_sends_a_service_scan(dlp: _DlpStub) -> None:
    dlp.answer = _json(200, {"hits": [], "max_sensitivity": "L1", "action": "ALLOW"})
    action = await scan_upload(
        dlp_url=_DLP, tenant_id=TEST_TENANT, user_id=TEST_USER,
        content="hello", resource_id="att_1", resource_type="attachment",
    )
    assert action == "ALLOW"
    assert dlp.calls == [{
        "url": f"{_DLP}/core/scan",
        "json": {"content": "hello", "resource_type": "attachment", "resource_id": "att_1"},
        # SERVICE: DLP persists the hit level as the resource's label itself.
        "headers": {"X-Tenant-Id": TEST_TENANT, "X-Actor": "SERVICE", "X-User-Id": TEST_USER},
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _MODES)
async def test_scan_upload_hit_is_kept_and_labelled_by_dlp(
    mode: FailureMode, dlp: _DlpStub, caplog: pytest.LogCaptureFixture,
) -> None:
    dlp.answer = _json(200, {
        "hits": [
            {"rule_name": "身份证号", "sensitivity": "L4"},
            {"rule_name": "身份证号", "sensitivity": "L4"},
            {"rule_name": "中国手机号", "sensitivity": "L3"},
        ],
        "max_sensitivity": "L4",
        "action": "QUARANTINE",
    })
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        action = await scan_upload(
            dlp_url=_DLP, tenant_id=TEST_TENANT, user_id=TEST_USER,
            content="secret", resource_id="att_1", resource_type="attachment",
            failure_mode=mode,
        )
    assert action == "QUARANTINE"
    assert len(dlp.calls) == 1  # no separate classification:tag call
    warnings = [
        r.getMessage() for r in caplog.records
        if r.name == _LOGGER and r.levelno == logging.WARNING and "flagged" in r.getMessage()
    ]
    assert warnings == [
        "DLP flagged attachment att_1: action=QUARANTINE max_sensitivity=L4 "
        "rules=身份证号, 中国手机号; DLP has labelled it",
    ]
    assert _errors(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_SCAN_FAILURES))
async def test_scan_upload_cannot_complete_closed_refuses(
    case: str, dlp: _DlpStub, caplog: pytest.LogCaptureFixture,
) -> None:
    dlp.answer, fragment = _SCAN_FAILURES[case]
    with caplog.at_level(logging.INFO, logger=_LOGGER), pytest.raises(DlpUnavailableError) as ei:
        await scan_upload(
            dlp_url=_DLP, tenant_id=TEST_TENANT, user_id=TEST_USER,
            content="hello", resource_id="att_7", resource_type="attachment",
            failure_mode="closed",
        )
    err = ei.value
    assert (err.status, err.code, err.operation) == (503, "AGENT.DLP.UNAVAILABLE", "upload")
    for text in (err.detail, err.user_message):
        assert "dlp.test" not in text and fragment not in text
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "att_7" in errors[0] and fragment in errors[0]
    assert "DLP_FAILURE_MODE=closed" in errors[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_SCAN_FAILURES))
async def test_scan_upload_cannot_complete_open_proceeds_loudly(
    case: str, dlp: _DlpStub, caplog: pytest.LogCaptureFixture,
) -> None:
    dlp.answer, fragment = _SCAN_FAILURES[case]
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        action = await scan_upload(
            dlp_url=_DLP, tenant_id=TEST_TENANT, user_id=TEST_USER,
            content="hello", resource_id="att_7", resource_type="attachment",
            failure_mode="open",
        )
    assert action is None
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "att_7" in errors[0] and fragment in errors[0]
    assert "UNCHECKED" in errors[0]


# --------------------------------------------------------------------------- #
# scan_and_tag (persisted assistant output)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_transcript_scan_is_a_service_scan_without_a_user(dlp: _DlpStub) -> None:
    dlp.answer = _json(200, {"hits": [], "max_sensitivity": "L1", "action": "ALLOW"})
    await scan_and_tag(
        dlp_url=_DLP, tenant_id=TEST_TENANT, content="the reply",
        resource_id="msg_1", resource_type="transcript",
    )
    assert dlp.calls == [{
        "url": f"{_DLP}/core/scan",
        "json": {"content": "the reply", "resource_type": "transcript", "resource_id": "msg_1"},
        "headers": {"X-Tenant-Id": TEST_TENANT, "X-Actor": "SERVICE"},
    }]


@pytest.mark.asyncio
async def test_transcript_scan_skips_empty_content(dlp: _DlpStub) -> None:
    await scan_and_tag(
        dlp_url=_DLP, tenant_id=TEST_TENANT, content="",
        resource_id="msg_1", resource_type="transcript",
    )
    assert dlp.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("case", ["network error", "timeout", "403 problem", "2xx without action"])
async def test_transcript_scan_failure_keeps_the_message_in_both_modes(
    case: str,
    mode: FailureMode,
    dlp: _DlpStub,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(_ENV, mode)
    dlp.answer, fragment = _SCAN_FAILURES[case]
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await scan_and_tag(  # must not raise
            dlp_url=_DLP, tenant_id=TEST_TENANT, content="the reply",
            resource_id="msg_9", resource_type="transcript",
        )
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "msg_9" in errors[0] and fragment in errors[0] and "UNLABELLED" in errors[0]


# --------------------------------------------------------------------------- #
# _maybe_text (what the upload call sites hand to the scan)
# --------------------------------------------------------------------------- #

def test_maybe_text_scans_text_whose_sample_cut_splits_a_character() -> None:
    data = ("中" * (_DLP_SCAN_MAX_BYTES // 3 + 10)).encode()
    assert _DLP_SCAN_MAX_BYTES % 3  # the cut really lands inside a character
    text = _maybe_text(data)
    assert text == "中" * (_DLP_SCAN_MAX_BYTES // 3)


def test_maybe_text_still_skips_binary_and_invalid_utf8() -> None:
    assert _maybe_text(b"\x89PNG\r\n\x1a\n\x00\x00") is None
    assert _maybe_text(b"\xff\xfe not utf-8") is None
    assert _maybe_text(b"") is None


# --------------------------------------------------------------------------- #
# Call sites (HTTP layer, DLP configured via the ``settings`` override)
# --------------------------------------------------------------------------- #

def _session() -> Session:
    return Session(
        id="ses_001", tenant_id=TEST_TENANT, user_id=TEST_USER,
        status="ACTIVE", created_at=_NOW, updated_at=_NOW,
    )


def _attachment() -> Attachment:
    return Attachment(
        id="att_001", tenant_id=TEST_TENANT, session_id="ses_001", user_id=TEST_USER,
        filename="hello.txt", mime="text/plain", size_bytes=5,
        blob_key=f"tenant/{TEST_TENANT}/sessions/ses_001/attachments/att_001/hello.txt",
        created_at=_NOW,
    )


def _workspace() -> Workspace:
    return Workspace(
        id="wsp_001", tenant_id=TEST_TENANT, user_id=TEST_USER, name="Workspace",
        description=None, instructions=None, skills=[], is_default=False, meta={},
        sensitivity_level="L2", created_at=_NOW, updated_at=_NOW,
    )


def _workspace_file() -> WorkspaceFile:
    return WorkspaceFile(
        id="wsf_001", tenant_id=TEST_TENANT, workspace_id="wsp_001", user_id=TEST_USER,
        filename="notes.txt", mime="text/plain", size_bytes=5,
        blob_key=f"tenant/{TEST_TENANT}/workspaces/wsp_001/files/wsf_001/notes.txt",
        description=None, sensitivity_level="L2", created_at=_NOW,
    )


def _blob(dlp: _DlpStub) -> MagicMock:
    blob = MagicMock()
    blob.put = MagicMock(side_effect=lambda *a, **kw: dlp.events.append("blob.put"))
    blob.get = MagicMock(return_value=b"hello")
    return blob


def _upload_attachment(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, blob: MagicMock,
    content: bytes = b"hello",
) -> httpx.Response:
    async def _refresh(obj: Attachment) -> None:
        obj.id = "att_001"
        obj.created_at = _NOW

    mock_db.execute.return_value = make_scalar_result(_session())
    mock_db.refresh.side_effect = _refresh
    with patch.object(app_client.app.state, "blob_store", blob, create=True):
        return app_client.post(
            "/api/session/v1/sessions/ses_001/attachments",
            headers=auth_headers,
            files={"file": ("hello.txt", io.BytesIO(content), "text/plain")},
        )


def _download_attachment(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, blob: MagicMock,
) -> httpx.Response:
    mock_db.execute.side_effect = [
        make_scalar_result(_session()),  # get_attachment → get_session
        make_scalar_result(_attachment()),
    ]
    with patch.object(app_client.app.state, "blob_store", blob, create=True):
        return app_client.get(
            "/api/session/v1/sessions/ses_001/files/att_001/content", headers=auth_headers,
        )


def test_attachment_upload_scans_before_storing_and_keeps_a_hit(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, dlp: _DlpStub,
) -> None:
    dlp.answer = _json(200, {"hits": [], "max_sensitivity": "L4", "action": "QUARANTINE"})
    blob = _blob(dlp)
    resp = _upload_attachment(app_client, mock_db, auth_headers, blob)
    assert resp.status_code == 201, resp.text
    assert dlp.events == ["dlp:scan", "blob.put"]
    assert dlp.calls[0]["json"] == {
        "content": "hello", "resource_type": "attachment", "resource_id": "att_001",
    }
    assert dlp.calls[0]["headers"]["X-User-Id"] == TEST_USER


def test_attachment_upload_refused_and_not_stored_when_dlp_is_down(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, dlp: _DlpStub,
) -> None:
    dlp.answer = _raise(httpx.ConnectError("connection refused"))
    blob = _blob(dlp)
    resp = _upload_attachment(app_client, mock_db, auth_headers, blob)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "AGENT.DLP.UNAVAILABLE"
    assert "can't be uploaded right now" in body["user_message"]
    assert "dlp.test" not in resp.text and "ConnectError" not in resp.text
    blob.put.assert_not_called()


def test_attachment_upload_stored_unscanned_in_open_mode(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, dlp: _DlpStub,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_mode(monkeypatch, "open")
    dlp.answer = _json(503, {"code": "FOUNDATION.DLP.UNAVAILABLE"})
    blob = _blob(dlp)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        resp = _upload_attachment(app_client, mock_db, auth_headers, blob)
    assert resp.status_code == 201, resp.text
    blob.put.assert_called_once()
    errors = _errors(caplog)
    assert len(errors) == 1 and "att_001" in errors[0] and "UNCHECKED" in errors[0]


def test_binary_attachment_upload_is_not_scanned(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, dlp: _DlpStub,
) -> None:
    dlp.answer = _raise(httpx.ConnectError("connection refused"))
    blob = _blob(dlp)
    resp = _upload_attachment(app_client, mock_db, auth_headers, blob, content=b"\x00\x01\x02")
    assert resp.status_code == 201, resp.text
    assert dlp.calls == []


def test_attachment_download_checked_with_trusted_identity_only(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, dlp: _DlpStub,
) -> None:
    dlp.answer = _json(200, {"allowed": True, "sensitivity": "L3"})
    resp = _download_attachment(app_client, mock_db, auth_headers, _blob(dlp))
    assert resp.status_code == 200, resp.text
    assert resp.content == b"hello"
    assert dlp.calls == [{
        "url": f"{_DLP}/core/check:download",
        "json": {"resource_type": "attachment", "resource_id": "att_001", "destination": "laptop"},
        "headers": {"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER},
    }]


def test_attachment_download_denied_by_dlp_is_403(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, dlp: _DlpStub,
) -> None:
    dlp.answer = _json(200, {"allowed": False, "reason": "Clearance L2 < L4"})
    blob = _blob(dlp)
    resp = _download_attachment(app_client, mock_db, auth_headers, blob)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "DLP: Clearance L2 < L4"
    blob.get.assert_not_called()


def test_attachment_download_refused_when_dlp_is_down(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, dlp: _DlpStub,
) -> None:
    dlp.answer = _raise(httpx.ReadTimeout("timed out"))
    blob = _blob(dlp)
    resp = _download_attachment(app_client, mock_db, auth_headers, blob)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "AGENT.DLP.UNAVAILABLE"
    assert "can't be downloaded right now" in body["user_message"]
    blob.get.assert_not_called()


def test_attachment_download_served_unchecked_in_open_mode(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, dlp: _DlpStub,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_mode(monkeypatch, "open")
    dlp.answer = _raise(httpx.ConnectError("connection refused"))
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        resp = _download_attachment(app_client, mock_db, auth_headers, _blob(dlp))
    assert resp.status_code == 200, resp.text
    errors = _errors(caplog)
    assert len(errors) == 1 and "att_001" in errors[0] and "UNCHECKED" in errors[0]


def test_workspace_upload_scans_before_storing(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, dlp: _DlpStub,
) -> None:
    async def _refresh(obj: WorkspaceFile) -> None:
        obj.id = "wsf_001"
        obj.created_at = _NOW

    dlp.answer = _json(200, {"hits": [], "max_sensitivity": "L1", "action": "ALLOW"})
    mock_db.execute.side_effect = [make_scalar_result(_workspace())]
    mock_db.refresh.side_effect = _refresh
    blob = _blob(dlp)
    with patch.object(app_client.app.state, "blob_store", blob, create=True):
        resp = app_client.post(
            "/api/session/v1/workspaces/wsp_001/files",
            headers=auth_headers,
            files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
            data={"sensitivity_level": "L1"},
        )
    assert resp.status_code == 201, resp.text
    assert dlp.events == ["dlp:scan", "blob.put"]
    assert dlp.calls[0]["json"] == {
        "content": "hello", "resource_type": "workspace_file", "resource_id": "wsf_001",
    }


def test_workspace_upload_refused_and_not_stored_when_dlp_is_down(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, dlp: _DlpStub,
) -> None:
    async def _refresh(obj: WorkspaceFile) -> None:
        obj.id = "wsf_001"
        obj.created_at = _NOW

    dlp.answer = _raise(httpx.ConnectError("connection refused"))
    mock_db.execute.side_effect = [make_scalar_result(_workspace())]
    mock_db.refresh.side_effect = _refresh
    blob = _blob(dlp)
    with patch.object(app_client.app.state, "blob_store", blob, create=True):
        resp = app_client.post(
            "/api/session/v1/workspaces/wsp_001/files",
            headers=auth_headers,
            files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
        )
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "AGENT.DLP.UNAVAILABLE"
    blob.put.assert_not_called()


def test_workspace_download_refused_when_dlp_is_down(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict, dlp: _DlpStub,
) -> None:
    dlp.answer = _json(500, {"code": "FOUNDATION.COMMON.INTERNAL"})
    mock_db.execute.side_effect = [
        make_scalar_result(_workspace()),       # ownership check
        make_scalar_result(_workspace_file()),  # get_file
    ]
    blob = _blob(dlp)
    with patch.object(app_client.app.state, "blob_store", blob, create=True):
        resp = app_client.get(
            "/api/session/v1/workspaces/wsp_001/files/wsf_001/content", headers=auth_headers,
        )
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "AGENT.DLP.UNAVAILABLE"
    assert dlp.calls[0]["json"] == {
        "resource_type": "workspace_file", "resource_id": "wsf_001", "destination": "laptop",
    }
    blob.get.assert_not_called()
