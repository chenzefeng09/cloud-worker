"""Automatic session-title generation and deterministic fallback tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_bff import session_title
from agent_bff.api.v1 import canvas


@pytest.fixture(autouse=True)
def _clear_vkey_cache() -> None:
    # The model key is cached per tenant (shared with the canvas endpoints).
    canvas._vkey_cache.clear()


def test_fallback_session_title_is_normalized_and_bounded() -> None:
    title = session_title.fallback_session_title(
        "  设计   航天器反作用飞轮 FOC 电机控制端到端验证 " * 4
    )
    assert len(title) == 28
    assert title.endswith("…")
    assert "  " not in title


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("标题：航天反作用飞轮 FOC 控制验证", "航天反作用飞轮 FOC 控制验证"),
        ("“姿态控制系统鲁棒性分析”\n不要输出这一行", "姿态控制系统鲁棒性分析"),
        ("```text\n星敏感器与飞轮联合仿真\n```", "星敏感器与飞轮联合仿真"),
    ],
)
def test_normalize_generated_title(content: str, expected: str) -> None:
    assert session_title.normalize_generated_title(
        content,
        fallback="fallback",
    ) == expected


class _FakeClient:
    def __init__(
        self,
        *,
        completion_status: int = 200,
        completion_content: str = "航天反作用飞轮 FOC 控制验证",
        renamed_during_generation: bool = False,
        mint_response: httpx.Response | None = None,
    ) -> None:
        self.mint_response = mint_response or httpx.Response(201, json={"virtual_key": "vk-eph"})
        self.completions = 0
        self.completion_status = completion_status
        self.completion_content = completion_content
        self.renamed_during_generation = renamed_during_generation
        self.session_reads = 0
        self.patches: list[dict[str, Any]] = []
        self.mint_headers: dict[str, str] | None = None

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        if url.endswith("/tenant-keys"):
            return httpx.Response(200, json=[{"virtual_key": "vk-test", "enabled": True}])
        if "/sessions/" in url:
            self.session_reads += 1
            title = (
                "用户手动改名"
                if self.renamed_during_generation and self.session_reads > 1
                else "新任务"
            )
            return httpx.Response(200, json={"id": "ses_1", "title": title})
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url: str, **_kwargs: Any) -> httpx.Response:
        if url.endswith("/tenant-keys/ephemeral"):
            self.mint_headers = _kwargs.get("headers")
            return self.mint_response
        assert url.endswith("/chat/completions")
        self.completions += 1
        return httpx.Response(
            self.completion_status,
            json={
                "choices": [
                    {"message": {"content": self.completion_content}},
                ]
            },
        )

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        assert "/sessions/" in url
        self.patches.append(kwargs["json"])
        return httpx.Response(200, json={"id": "ses_1", **kwargs["json"]})


# What the gateway answers a key mint for a tenant with no enabled LLM provider.
_NO_PROVIDER = {"code": "FOUNDATION.LLM_GATEWAY.PROVIDER.NOT_FOUND", "status": 404}


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        session_orchestrator_url="http://orch/api/session/v1",
        foundation_llm_gateway_url="http://gateway/api/llm-gateway/v1",
        session_title_model="",
    )


def test_generate_and_update_session_title_uses_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(
        session_title.httpx,
        "AsyncClient",
        lambda **_kwargs: fake,
    )

    asyncio.run(
        session_title.generate_and_update_session_title(
            settings=_settings(),
            session_id="ses_1",
            message="设计一套真实的航天反作用飞轮 FOC 控制验证任务",
            tenant_id="tnt_1",
            user_id="usr_1",
            access_token="token",
            model_hint="deepseek-fast",
        )
    )

    assert fake.patches == [{"title": "航天反作用飞轮 FOC 控制验证"}]


def test_generate_mints_the_title_key_as_a_proven_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway mints keys for a SERVICE caller with its pre-shared token only."""
    fake = _FakeClient()
    monkeypatch.setattr(session_title.httpx, "AsyncClient", lambda **_kwargs: fake)
    settings = _settings()
    settings.foundation_llm_gateway_service_token = "bff-gw-service-token-0123456789"

    asyncio.run(
        session_title.generate_and_update_session_title(
            settings=settings,
            session_id="ses_1",
            message="设计一套真实的航天反作用飞轮 FOC 控制验证任务",
            tenant_id="tnt_1",
            user_id="usr_1",
            access_token="token",
            model_hint="deepseek-fast",
        )
    )

    assert fake.mint_headers is not None
    assert fake.mint_headers["X-Actor"] == "SERVICE"
    assert fake.mint_headers["X-Internal-Service-Token"] == "bff-gw-service-token-0123456789"
    assert fake.patches == [{"title": "航天反作用飞轮 FOC 控制验证"}]


def test_generate_and_update_session_title_falls_back_when_model_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(completion_status=503)
    monkeypatch.setattr(
        session_title.httpx,
        "AsyncClient",
        lambda **_kwargs: fake,
    )
    message = "设计航天器姿态控制闭环验证"

    asyncio.run(
        session_title.generate_and_update_session_title(
            settings=_settings(),
            session_id="ses_1",
            message=message,
            tenant_id="tnt_1",
            user_id="usr_1",
            access_token="token",
            model_hint="deepseek-fast",
        )
    )

    assert fake.patches == [{"title": message}]


def test_generate_does_not_overwrite_manual_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(renamed_during_generation=True)
    monkeypatch.setattr(
        session_title.httpx,
        "AsyncClient",
        lambda **_kwargs: fake,
    )

    asyncio.run(
        session_title.generate_and_update_session_title(
            settings=_settings(),
            session_id="ses_1",
            message="设计航天 FOC 验证",
            tenant_id="tnt_1",
            user_id="usr_1",
            access_token="token",
            model_hint="deepseek-fast",
        )
    )

    assert fake.patches == []


def test_generate_keeps_the_fallback_title_when_no_model_is_set_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(
        mint_response=httpx.Response(404, json=_NO_PROVIDER),
    )
    monkeypatch.setattr(
        session_title.httpx,
        "AsyncClient",
        lambda **_kwargs: fake,
    )
    message = "设计航天器姿态控制闭环验证"

    asyncio.run(
        session_title.generate_and_update_session_title(
            settings=_settings(),
            session_id="ses_1",
            message=message,
            tenant_id="tnt_1",
            user_id="usr_1",
            access_token="token",
            model_hint="deepseek-fast",
        )
    )

    assert fake.completions == 0
    assert fake.patches == [{"title": message}]
