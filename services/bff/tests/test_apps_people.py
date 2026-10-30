"""App responses name the people behind their user ids.

The BFF resolves ``author_id`` / ``created_by`` / ``submitted_by`` /
``reviewed_by`` to display names through foundation-identity and passes on the
name alone — never the email, roles or clearance of the identity profile.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.api import _people
from agent_bff.config import Settings
from agent_bff.main import create_app

ALICE = "usr_01J5TEST000000000000ALICE1"
BOB = "usr_01J5TEST00000000000000BOB1"
GONE = "usr_01J5TEST0000000000000GONE1"
CALLER = "usr_01J5TEST0000000000000USER1"
TENANT = "tnt_01J5TEST00000000000000TNT1"

_VERSION = {
    "id": "ver_1",
    "version": "1",
    "status": "published",
    "created_by": ALICE,
    "submitted_by": ALICE,
    "reviewed_by": BOB,
}
_APP = {"id": "app_1", "name": "Contract review", "author_id": ALICE, "versions": [_VERSION]}


class FakeUpstreams:
    """app-registry + identity stub; records every identity lookup."""

    def __init__(self) -> None:
        self.profiles = {
            ALICE: {"id": ALICE, "display_name": "Alice Chen", "email": "alice@example.com",
                    "clearance": "L3", "roles": ["member"]},
            BOB: {"id": BOB, "display_name": "Bob Li", "email": "bob@example.com"},
        }
        self.identity_status: int | None = None
        self.lookups: list[str] = []
        self.registry: dict[str, object] = {
            "/apps": {"items": [_APP, {**_APP, "id": "app_2", "versions": []},
                                {**_APP, "id": "app_3", "author_id": "erased-user",
                                 "versions": []}],
                      "total": 3},
            "/apps/app_1": _APP,
            "/apps/review/pending": {
                "items": [{"app_id": "app_1", "app_name": "Contract review", "author_id": GONE,
                           "version": {**_VERSION, "reviewed_by": None}}],
                "total": 1,
            },
        }

    def route(self, method: str, url: str, **kw: object) -> httpx.Response:
        if "/users/" in url:
            uid = url.rsplit("/", 1)[-1]
            self.lookups.append(uid)
            headers = kw.get("headers") or {}
            assert isinstance(headers, dict)
            assert headers.get("Authorization") == "Bearer fake-jwt-for-test"
            if self.identity_status is not None:
                return httpx.Response(self.identity_status, json={"detail": "nope"})
            profile = self.profiles.get(uid)
            return httpx.Response(200, json=profile) if profile else httpx.Response(404)
        for path, body in self.registry.items():
            if url.endswith(path):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"detail": "unknown"})


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    _people.clear()
    yield
    _people.clear()


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> FakeUpstreams:
    fake = FakeUpstreams()

    class _AsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kw: object) -> httpx.Response:
            return fake.route("GET", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    return fake


@pytest.fixture
def client(stub: FakeUpstreams) -> Iterator[TestClient]:
    from tests.conftest import TEST_COOKIE_SECRET

    settings = Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        foundation_identity_url="http://identity-test/api/identity/v1",
        cors_origins="http://localhost",
        otel_enabled=False,
    )
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=settings.cookie_max_age,
        ),
    )
    cookie = signer.sign(
        {"user_id": CALLER, "tenant_id": TENANT, "access_token": "fake-jwt-for-test"},
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        c.cookies.set(settings.cookie_name, cookie)
        yield c


def test_list_names_each_author_once(client: TestClient, stub: FakeUpstreams) -> None:
    resp = client.get("/api/bff/v1/apps")

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["author_name"] for i in items] == ["Alice Chen", "Alice Chen", None]
    # Two apps by one author are one lookup; the erased placeholder is none.
    assert sorted(stub.lookups) == sorted({ALICE, BOB})


def test_only_the_display_name_leaves_the_bff(client: TestClient) -> None:
    body = client.get("/api/bff/v1/apps/app_1").text

    assert "Alice Chen" in body
    assert "alice@example.com" not in body
    assert "clearance" not in body


def test_detail_names_every_step_of_a_version(client: TestClient) -> None:
    app = client.get("/api/bff/v1/apps/app_1").json()

    assert app["author_name"] == "Alice Chen"
    version = app["versions"][0]
    assert version["created_by_name"] == "Alice Chen"
    assert version["submitted_by_name"] == "Alice Chen"
    assert version["reviewed_by_name"] == "Bob Li"


def test_review_queue_names_authors_and_leaves_unknown_ones_null(
    client: TestClient, stub: FakeUpstreams,
) -> None:
    item = client.get("/api/bff/v1/apps/review/pending").json()["items"][0]

    assert item["author_name"] is None  # identity says 404
    assert item["version"]["submitted_by_name"] == "Alice Chen"
    assert item["version"]["reviewed_by_name"] is None
    # A user identity does not know is remembered too.
    stub.lookups.clear()
    client.get("/api/bff/v1/apps/review/pending")
    assert stub.lookups == []


def test_names_are_cached_between_requests(client: TestClient, stub: FakeUpstreams) -> None:
    client.get("/api/bff/v1/apps")
    stub.lookups.clear()

    resp = client.get("/api/bff/v1/apps/app_1")

    assert resp.json()["author_name"] == "Alice Chen"
    assert stub.lookups == []


def test_identity_outage_leaves_names_null_and_the_page_working(
    client: TestClient, stub: FakeUpstreams,
) -> None:
    stub.identity_status = 503

    resp = client.get("/api/bff/v1/apps/app_1")

    assert resp.status_code == 200
    assert resp.json()["author_name"] is None
    # Not retried on every request while identity is down.
    stub.lookups.clear()
    client.get("/api/bff/v1/apps/app_1")
    assert stub.lookups == []


def test_a_forgotten_name_is_looked_up_again(client: TestClient, stub: FakeUpstreams) -> None:
    client.get("/api/bff/v1/apps/app_1")
    stub.profiles[ALICE] = {**stub.profiles[ALICE], "display_name": "Alice Wang"}

    _people.forget(TENANT, ALICE)

    assert client.get("/api/bff/v1/apps/app_1").json()["author_name"] == "Alice Wang"


async def test_a_slow_identity_never_holds_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_people, "_BUDGET_SECONDS", 0.05)

    class _SlowClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _SlowClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kw: object) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200, json={"display_name": "late"})

    monkeypatch.setattr(httpx, "AsyncClient", _SlowClient)
    loop = asyncio.get_running_loop()
    started = loop.time()

    names = await _people.display_names(
        "http://identity-test", (TENANT, CALLER, "tok"), [ALICE, BOB],
    )

    assert names == {}
    assert loop.time() - started < 1
