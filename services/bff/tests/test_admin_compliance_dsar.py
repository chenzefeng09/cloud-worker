"""Tests: DSAR creation normalises ``request_type`` (``admin_compliance.py``).

foundation-compliance only accepts its upper-case ``DsarRequestType`` values;
the BFF accepts any case and "-" / " " separators, forwards the canonical
value, and rejects anything else with a readable 422 before calling upstream.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.api.v1.admin_compliance import DSAR_REQUEST_TYPES
from agent_bff.config import Settings
from agent_bff.main import create_app
from tests.privacy_fakes import COOKIE_SECRET, PREFIX, USER_A, make_cookie

COMPL_URL = "http://compl-test/api/compliance/v1"
DSAR = PREFIX + "/admin/compliance/dsar"


class FakeCompliance:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.dsar_bodies: list[Any] = []

    def route(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if url.endswith("/check"):
            return httpx.Response(200, json={"allowed": self.allowed})
        if method == "POST" and url == f"{COMPL_URL}/dsar":
            body = kw.get("json")
            self.dsar_bodies.append(body)
            return httpx.Response(201, json={"id": "dsar_1", **body, "status": "OPEN"})
        return httpx.Response(404, json={"detail": "unexpected call"})


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=COOKIE_SECRET,
        cookie_secure=False,
        foundation_compliance_url=COMPL_URL,
        cors_origins="http://localhost",
        otel_enabled=False,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def cookie(settings: Settings) -> dict[str, str]:
    return make_cookie(settings, USER_A)


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeCompliance) -> FakeCompliance:
    class _AC:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> _AC:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("POST", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


@pytest.fixture
def compliance(monkeypatch: pytest.MonkeyPatch) -> FakeCompliance:
    return _install(monkeypatch, FakeCompliance())


def _body(request_type: Any) -> dict[str, Any]:
    return {"user_id": USER_A, "request_type": request_type, "details": "Export my data"}


@pytest.mark.parametrize(
    "given,expected",
    [
        ("access", "ACCESS"),
        ("Erasure", "ERASURE"),
        ("  portability ", "PORTABILITY"),
        ("automated-decision", "AUTOMATED_DECISION"),
        ("automated decision", "AUTOMATED_DECISION"),
        ("AUTOMATED_DECISION", "AUTOMATED_DECISION"),
        *[(t.lower(), t) for t in DSAR_REQUEST_TYPES],
    ],
)
def test_request_type_is_normalised(
    client: TestClient, cookie: dict[str, str], compliance: FakeCompliance,
    given: str, expected: str,
) -> None:
    resp = client.post(DSAR, json=_body(given), cookies=cookie)

    assert resp.status_code == 201, resp.text
    assert compliance.dsar_bodies == [_body(expected)]
    assert resp.json()["request_type"] == expected


def test_accepted_types_match_the_upstream_enum() -> None:
    assert set(DSAR_REQUEST_TYPES) == {
        "ACCESS", "RECTIFICATION", "ERASURE", "RESTRICTION", "PORTABILITY", "OBJECTION",
        "AUTOMATED_DECISION",
    }


@pytest.mark.parametrize("request_type", ["delete", "access_all", "", "   ", 123, None])
def test_unknown_request_type_is_rejected_before_upstream(
    client: TestClient, cookie: dict[str, str], compliance: FakeCompliance,
    request_type: Any,
) -> None:
    resp = client.post(DSAR, json=_body(request_type), cookies=cookie)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["field"] == "request_type"
    assert "ACCESS" in detail["user_message"]
    assert "AUTOMATED_DECISION" in detail["user_message"]
    assert compliance.dsar_bodies == []


def test_missing_request_type_is_rejected(
    client: TestClient, cookie: dict[str, str], compliance: FakeCompliance,
) -> None:
    resp = client.post(DSAR, json={"user_id": USER_A, "details": "x"}, cookies=cookie)
    assert resp.status_code == 422
    assert resp.json()["detail"]["user_message"].startswith("request_type is required")
    assert compliance.dsar_bodies == []


@pytest.mark.parametrize("raw", ['["ACCESS"]', '"ACCESS"', "{not json"])
def test_body_must_be_a_json_object(
    client: TestClient, cookie: dict[str, str], compliance: FakeCompliance, raw: str,
) -> None:
    resp = client.post(
        DSAR, content=raw, headers={"Content-Type": "application/json"}, cookies=cookie,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["user_message"] == "The request body must be a JSON object."
    assert compliance.dsar_bodies == []


def test_authz_is_checked_before_validation(
    client: TestClient, cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, FakeCompliance(allowed=False))
    resp = client.post(DSAR, json=_body("not-a-type"), cookies=cookie)
    assert resp.status_code == 403
    assert fake.dsar_bodies == []
