"""Service-layer tests for RegistryService against in-memory SQLite."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_registry.errors import McpServerNameConflictError, McpServerNotFoundError
from mcp_registry.services.registry_service import RegistryService
from tests.conftest import TEST_TENANT, TEST_TENANT_OTHER


@pytest.fixture
def svc(session: AsyncSession) -> RegistryService:
    return RegistryService(session, tenant_id=TEST_TENANT)


@pytest.mark.asyncio
async def test_create_assigns_tenant_and_id(svc: RegistryService) -> None:
    server = await svc.create(
        name="weather",
        transport="http",
        endpoint="https://x/y",
        command=None,
        env={},
        scopes=[],
        enabled=True,
    )
    assert server.tenant_id == TEST_TENANT
    assert server.id.startswith("mcp_")


@pytest.mark.asyncio
async def test_duplicate_name_raises_conflict(svc: RegistryService) -> None:
    await svc.create(
        name="weather",
        transport="http",
        endpoint="https://x",
        command=None,
        env={},
        scopes=[],
        enabled=True,
    )
    with pytest.raises(McpServerNameConflictError):
        await svc.create(
            name="weather",
            transport="http",
            endpoint="https://other",
            command=None,
            env={},
            scopes=[],
            enabled=True,
        )


@pytest.mark.asyncio
async def test_get_missing_raises(svc: RegistryService) -> None:
    with pytest.raises(McpServerNotFoundError):
        await svc.get("mcp_unknown")


@pytest.mark.asyncio
async def test_update_changes_fields(svc: RegistryService) -> None:
    created = await svc.create(
        name="weather",
        transport="http",
        endpoint="https://x",
        command=None,
        env={},
        scopes=[],
        enabled=True,
    )
    updated = await svc.update(created.id, enabled=False, scopes=["read:weather"])
    assert updated.enabled is False
    assert updated.scopes == ["read:weather"]


@pytest.mark.asyncio
async def test_delete_removes_row(svc: RegistryService) -> None:
    created = await svc.create(
        name="weather",
        transport="http",
        endpoint="https://x",
        command=None,
        env={},
        scopes=[],
        enabled=True,
    )
    await svc.delete(created.id)
    with pytest.raises(McpServerNotFoundError):
        await svc.get(created.id)


@pytest.mark.asyncio
async def test_list_for_tenant_filters_enabled(session: AsyncSession) -> None:
    svc = RegistryService(session, tenant_id=TEST_TENANT)
    await svc.create(
        name="on",
        transport="http",
        endpoint="https://a",
        command=None,
        env={},
        scopes=[],
        enabled=True,
    )
    await svc.create(
        name="off",
        transport="http",
        endpoint="https://b",
        command=None,
        env={},
        scopes=[],
        enabled=False,
    )
    # Other tenant — must not leak.
    other = RegistryService(session, tenant_id=TEST_TENANT_OTHER)
    await other.create(
        name="elsewhere",
        transport="http",
        endpoint="https://c",
        command=None,
        env={},
        scopes=[],
        enabled=True,
    )

    items = await svc.list_for_tenant(TEST_TENANT)
    names = sorted(s.name for s in items)
    assert names == ["on"]
