"""An upstream failure a route does not translate itself is never a BFF 500.

Routes that call ``resp.raise_for_status()`` used to turn an upstream 403 (a
member without the permission) into a 500; an unreachable service also read
as a BFF crash.  The app-level handlers relay the upstream status instead,
and report an unreachable service as a 502.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient


def _upstream(monkeypatch: pytest.MonkeyPatch, answer: object) -> None:
    """Every upstream call gets ``answer``: a response, or an error raised."""

    class _AsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kw: object) -> httpx.Response:
            if isinstance(answer, Exception):
                raise answer
            assert isinstance(answer, httpx.Response)
            return httpx.Response(
                answer.status_code,
                content=answer.content,
                headers=answer.headers,
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)


@pytest.mark.parametrize(
    "path",
    [
        "/api/bff/v1/library/skills/installed",
        "/api/bff/v1/library/mcp-servers",
        "/api/bff/v1/library/skills",
        "/api/bff/v1/library/folders",
        "/api/bff/v1/schedules",
        "/api/bff/v1/fleet/sandboxes",
    ],
)
def test_an_upstream_403_stays_a_403(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    _upstream(monkeypatch, httpx.Response(403, json={"code": "PERMISSION_DENIED"}))

    resp = client.get(path, cookies=authed_cookie)

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == {"code": "PERMISSION_DENIED"}


def test_an_upstream_404_with_a_text_body_is_relayed(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upstream(monkeypatch, httpx.Response(404, text="no such pool"))

    resp = client.get("/api/bff/v1/fleet/pool", cookies=authed_cookie)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "no such pool"


def test_an_unreachable_service_is_a_502(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upstream(monkeypatch, httpx.ConnectError("connection refused"))

    resp = client.get("/api/bff/v1/library/skills/installed", cookies=authed_cookie)

    assert resp.status_code == 502
    body = resp.json()
    assert body["code"] == "AGENT.BFF.UPSTREAM_ERROR"
    # The internal address of the service is not disclosed.
    assert "refused" not in resp.text
