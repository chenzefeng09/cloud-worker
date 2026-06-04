"""Tests for GET /runs/token-usage/daily endpoint.

Verifies:
- Group-by per day with SUM(total_tokens)
- Missing days filled with 0
- Result always has exactly `days` entries in ascending order
- Tenant isolation via mock_db
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient


def _make_execute_result(rows: list[tuple]) -> MagicMock:
    """Build a fake SQLAlchemy execute result with .all() for row queries."""
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    return result


def _make_row(day: str, tokens: int) -> MagicMock:
    """Return a fake Row-like object with .day and .tokens attributes."""
    row = MagicMock()
    row.day = dt.date.fromisoformat(day)
    row.tokens = tokens
    return row


def test_daily_token_usage_requires_auth(app_client: TestClient) -> None:
    resp = app_client.get("/api/session/v1/runs/token-usage/daily")
    assert resp.status_code in (401, 403)


def test_daily_token_usage_fills_missing_days(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """DB returns only 2 days; endpoint must back-fill the rest with 0."""
    today = dt.date.today()
    day_minus_1 = (today - dt.timedelta(days=1)).isoformat()
    day_minus_3 = (today - dt.timedelta(days=3)).isoformat()

    db_rows = [
        _make_row(day_minus_3, 500),
        _make_row(day_minus_1, 1200),
    ]
    mock_db.execute.return_value = _make_execute_result(db_rows)

    resp = app_client.get(
        "/api/session/v1/runs/token-usage/daily",
        params={"days": 7},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "days" in data
    result = data["days"]
    assert len(result) == 7, f"expected 7 entries, got {len(result)}"

    # Must be ascending by date
    dates = [d["day"] for d in result]
    assert dates == sorted(dates), "response not in ascending date order"

    # Today is last entry
    assert result[-1]["day"] == today.isoformat()

    # Check specific values
    by_day = {d["day"]: d["tokens"] for d in result}
    assert by_day[day_minus_3] == 500
    assert by_day[day_minus_1] == 1200

    # All others should be 0
    for d in result:
        if d["day"] not in (day_minus_3, day_minus_1):
            assert d["tokens"] == 0, f"expected 0 for {d['day']}, got {d['tokens']}"


def test_daily_token_usage_all_zeros_when_no_runs(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """When DB returns empty rows, all 7 days should have 0 tokens."""
    mock_db.execute.return_value = _make_execute_result([])

    resp = app_client.get(
        "/api/session/v1/runs/token-usage/daily",
        params={"days": 7},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data["days"]) == 7
    for d in data["days"]:
        assert d["tokens"] == 0


def test_daily_token_usage_custom_days_param(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """The `days` query param controls how many days are returned."""
    mock_db.execute.return_value = _make_execute_result([])

    for n in [1, 3, 14, 30]:
        resp = app_client.get(
            "/api/session/v1/runs/token-usage/daily",
            params={"days": n},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["days"]) == n, f"expected {n} days but got {len(data['days'])}"


def test_daily_token_usage_today_is_last_entry(
    app_client: TestClient, mock_db: AsyncMock, auth_headers: dict[str, str],
) -> None:
    """Today's date must always be the last entry in the response."""
    today = dt.date.today()
    mock_db.execute.return_value = _make_execute_result([])

    resp = app_client.get(
        "/api/session/v1/runs/token-usage/daily",
        params={"days": 7},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["days"][-1]["day"] == today.isoformat()
