"""Tests for the BFF public inbound-webhook receiver.

``POST /api/bff/v1/triggers/{id}/webhook`` — verifies a per-webhook secret via
cron-runner, maps the payload against the App input_schema, uploads any files,
and re-enters ``/apps/{id}/run`` with ``source="webhook"``.

Style mirrors ``test_apps_proxy.py``: a single fake ``httpx.AsyncClient`` routes
every outbound call by URL so the receiver runs end-to-end without a real stack.
The receiver is exempt from user-auth, so no session cookie is used.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.config import Settings
from agent_bff.main import create_app

# App the verified trigger points at: one text field + one (multi) file field.
_APP_STUB = {
    "id": "app_test",
    "name": "Webhook App",
    "current_version_id": "ver_test",
    "versions": [
        {
            "id": "ver_test",
            "status": "published",
            "version": 1,
            "system_prompt": "sys",
            "user_prompt_template": "Do /topic with /doc.",
            "model": "test-model",
            "input_schema": [
                {"key": "topic", "label": "Topic", "type": "text", "required": True},
                {
                    "key": "doc",
                    "label": "Doc",
                    "type": "file",
                    "required": False,
                    "file": {"multiple": True},
                },
            ],
        }
    ],
}


class FakeFoundation:
    """Routes every outbound httpx call the receiver makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.verify_status = 200
        self.verify_body: dict = {
            "id": "cron_1",
            "tenant_id": "tnt_hook",
            "app_id": "app_test",
            "field_mapping": {"subject": "topic", "attachment": "doc"},
            "input_values": {},
            "trigger_type": "webhook",
        }
        # The App the verified trigger resolves to; overridable per-test.
        self.app_stub: dict = _APP_STUB
        self._att_seq = 0

    def route(self, method: str, url: str, **kw) -> httpx.Response:
        self.calls.append((method, url, kw.get("json")))
        if "/internal/cron-jobs/" in url and url.endswith("/verify") and method == "POST":
            return httpx.Response(self.verify_status, json=self.verify_body)
        if "/apps/" in url and url.endswith("/run") and method == "POST":
            return httpx.Response(
                201,
                json={"session_id": "ses_run", "run_id": "run_1", "status": "RUNNING"},
            )
        if "/apps/" in url and method == "GET":
            return httpx.Response(200, json=self.app_stub)
        if url.endswith("/sessions") and method == "POST":
            return httpx.Response(201, json={"id": "ses_draft", "status": "ACTIVE"})
        if "/attachments" in url and method == "POST":
            self._att_seq += 1
            return httpx.Response(201, json={"id": f"att_{self._att_seq}"})
        return httpx.Response(200, json={"ok": True})

    def run_call(self) -> dict | None:
        for method, url, body in self.calls:
            if method == "POST" and url.endswith("/run"):
                return body
        return None

    def verify_called(self) -> bool:
        return any(
            m == "POST" and "/verify" in u for m, u, _ in self.calls
        )


@pytest.fixture
def settings_for_hook() -> Settings:
    from tests.conftest import TEST_COOKIE_SECRET

    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        service_token="test_token",
        cron_runner_url="http://cron-test:8013/api/cron/v1",
        app_registry_url="http://registry-test:8013/api/apps/v1",
        session_orchestrator_url="http://orch-test:8006/api/session/v1",
        bff_self_url="http://bff-self:8005",
        cors_origins="http://localhost",
        otel_enabled=False,
    )


def _make_client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, FakeFoundation]:
    fake = FakeFoundation()

    class _AsyncClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def post(self, url: str, **kw) -> httpx.Response:
            return fake.route("POST", url, **kw)

        async def get(self, url: str, **kw) -> httpx.Response:
            return fake.route("GET", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    return client, fake


@pytest.fixture
def client_and_fake(
    settings_for_hook: Settings, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, FakeFoundation]:
    return _make_client(settings_for_hook, monkeypatch)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def test_missing_secret_returns_401(client_and_fake: tuple) -> None:
    client, fake = client_and_fake
    resp = client.post(
        "/api/bff/v1/triggers/cron_1/webhook",
        json={"subject": "hello"},
    )
    assert resp.status_code == 401
    # Secret extraction fails before any verification call.
    assert not fake.verify_called()


def test_bad_secret_returns_401(client_and_fake: tuple) -> None:
    client, fake = client_and_fake
    fake.verify_status = 401
    fake.verify_body = {"detail": "invalid secret"}
    resp = client.post(
        "/api/bff/v1/triggers/cron_1/webhook",
        headers={"Authorization": "Bearer wrong-secret"},
        json={"subject": "hello"},
    )
    assert resp.status_code == 401
    assert fake.verify_called()
    # A rejected secret must never reach run_app.
    assert fake.run_call() is None


# --------------------------------------------------------------------------- #
# Multipart happy path
# --------------------------------------------------------------------------- #

def test_multipart_maps_inputs_and_files_and_launches_webhook_run(
    client_and_fake: tuple,
) -> None:
    client, fake = client_and_fake
    resp = client.post(
        "/api/bff/v1/triggers/cron_1/webhook",
        headers={"Authorization": "Bearer good-secret"},
        data={"subject": "climate"},
        files={"attachment": ("report.txt", b"file-bytes", "text/plain")},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["session_id"] == "ses_run"
    assert body["run_id"] == "run_1"

    run = fake.run_call()
    assert run is not None, "expected a POST .../run call"
    assert run["source"] == "webhook"
    # 'subject' → 'topic' (text), 'attachment' → 'doc' (file, uploaded → att id).
    assert run["inputs"] == {"topic": "climate"}
    assert run["file_inputs"] == {"doc": ["att_1"]}
    # Draft session was created + reused for the run.
    assert run["session_id"] == "ses_draft"


def test_multipart_text_value_routed_to_file_field_400(
    client_and_fake: tuple,
) -> None:
    """A text value mapped onto a file field is a 400 here, not a 422 downstream."""
    client, fake = client_and_fake
    # Map incoming 'attachment' text onto the 'doc' file field.
    resp = client.post(
        "/api/bff/v1/triggers/cron_1/webhook",
        headers={"Authorization": "Bearer good-secret"},
        data={"subject": "climate", "attachment": "not-a-file"},
    )
    assert resp.status_code == 400
    assert fake.run_call() is None


def test_json_file_field_is_400_url_mode_not_supported(
    client_and_fake: tuple,
) -> None:
    """URL-mode file references (JSON) are TODO — a file field via JSON 400s."""
    client, fake = client_and_fake
    resp = client.post(
        "/api/bff/v1/triggers/cron_1/webhook",
        headers={"Authorization": "Bearer good-secret"},
        json={"subject": "climate", "attachment": "https://example.com/f.pdf"},
    )
    assert resp.status_code == 400
    assert fake.run_call() is None


def test_json_text_only_launches_webhook_run(client_and_fake: tuple) -> None:
    client, fake = client_and_fake
    resp = client.post(
        "/api/bff/v1/triggers/cron_1/webhook",
        headers={"X-Webhook-Secret": "good-secret"},
        json={"subject": "climate"},
    )
    assert resp.status_code == 202, resp.text
    run = fake.run_call()
    assert run is not None
    assert run["source"] == "webhook"
    assert run["inputs"] == {"topic": "climate"}
    assert run["file_inputs"] == {}
    # No files → no draft session pre-created; run_app makes its own.
    assert "session_id" not in run


# --------------------------------------------------------------------------- #
# Mixed input mode — fixed (input_values) + from-event (payload) fields
# --------------------------------------------------------------------------- #

# App with two text fields: 'topic' will be a fixed field, 'summary' from-event.
_APP_TWO_TEXT = {
    "id": "app_test",
    "name": "Mixed App",
    "current_version_id": "ver_test",
    "versions": [
        {
            "id": "ver_test",
            "status": "published",
            "version": 1,
            "system_prompt": "sys",
            "user_prompt_template": "Do /topic — /summary.",
            "model": "test-model",
            "input_schema": [
                {"key": "topic", "label": "Topic", "type": "text", "required": True},
                {"key": "summary", "label": "Summary", "type": "text", "required": False},
            ],
        }
    ],
}


def test_fixed_input_value_seeds_run_inputs_when_payload_omits(
    client_and_fake: tuple,
) -> None:
    """A fixed field (input_values) reaches run_app even when absent from the payload;
    a from-event field for a DIFFERENT key overlays alongside it."""
    client, fake = client_and_fake
    fake.app_stub = _APP_TWO_TEXT
    # 'topic' fixed via input_values; only 'summary' is mapped from-event.
    fake.verify_body = {
        "id": "cron_1",
        "tenant_id": "tnt_hook",
        "app_id": "app_test",
        "field_mapping": {"sub": "summary"},
        "input_values": {"text": {"topic": "fixed-topic"}, "multi": {}},
        "trigger_type": "webhook",
    }
    resp = client.post(
        "/api/bff/v1/triggers/cron_1/webhook",
        headers={"Authorization": "Bearer good-secret"},
        json={"sub": "from-event-summary"},  # payload omits 'topic'
    )
    assert resp.status_code == 202, resp.text
    run = fake.run_call()
    assert run is not None
    # Fixed 'topic' present despite the payload omitting it; 'summary' overlaid.
    assert run["inputs"] == {"topic": "fixed-topic", "summary": "from-event-summary"}


def test_payload_overlays_fixed_input_value(client_and_fake: tuple) -> None:
    """When a payload key maps to a fixed field, the payload value wins (overlay)."""
    client, fake = client_and_fake
    fake.app_stub = _APP_TWO_TEXT
    # 'topic' is fixed AND also mapped from-event via 'subject' → 'topic'.
    fake.verify_body = {
        "id": "cron_1",
        "tenant_id": "tnt_hook",
        "app_id": "app_test",
        "field_mapping": {"subject": "topic"},
        "input_values": {"text": {"topic": "fixed-topic"}, "multi": {}},
        "trigger_type": "webhook",
    }
    resp = client.post(
        "/api/bff/v1/triggers/cron_1/webhook",
        headers={"Authorization": "Bearer good-secret"},
        json={"subject": "payload-topic"},
    )
    assert resp.status_code == 202, resp.text
    run = fake.run_call()
    assert run is not None
    # Payload wins the overlapping 'topic' key.
    assert run["inputs"] == {"topic": "payload-topic"}


# --------------------------------------------------------------------------- #
# Hardening
# --------------------------------------------------------------------------- #

def test_body_size_limit_returns_413(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import TEST_COOKIE_SECRET

    settings = Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        service_token="test_token",
        webhook_max_body_bytes=8,  # tiny cap
        cors_origins="http://localhost",
        otel_enabled=False,
    )
    client, fake = _make_client(settings, monkeypatch)
    resp = client.post(
        "/api/bff/v1/triggers/cron_1/webhook",
        headers={"Authorization": "Bearer good-secret"},
        json={"subject": "a much longer body than eight bytes"},
    )
    assert resp.status_code == 413
    assert not fake.verify_called()


def test_unsupported_content_type_returns_415(client_and_fake: tuple) -> None:
    client, fake = client_and_fake
    resp = client.post(
        "/api/bff/v1/triggers/cron_1/webhook",
        headers={
            "Authorization": "Bearer good-secret",
            "Content-Type": "text/plain",
        },
        content=b"raw text",
    )
    assert resp.status_code == 415
    assert fake.run_call() is None
