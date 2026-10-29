"""Unit tests for cron-runner.

Covers:
* CronTrigger validation (valid + invalid expressions)
* CronJob model import sanity (new_id + field defaults)
* Config import + defaults
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# CronTrigger expression validation
# --------------------------------------------------------------------------- #

def test_valid_cron_expression() -> None:
    """Standard 5-field cron expressions must be accepted without error."""
    from apscheduler.triggers.cron import CronTrigger

    # Every hour at minute 0
    CronTrigger.from_crontab("0 * * * *")
    # Every day at 08:30
    CronTrigger.from_crontab("30 8 * * *")
    # Every weekday at 09:00
    CronTrigger.from_crontab("0 9 * * 1-5")
    # Every 5 minutes
    CronTrigger.from_crontab("*/5 * * * *")


def test_invalid_cron_expression_raises() -> None:
    """Invalid expressions must raise (any exception — APScheduler uses ValueError)."""
    from apscheduler.triggers.cron import CronTrigger

    with pytest.raises(Exception):
        CronTrigger.from_crontab("not-a-cron")

    with pytest.raises(Exception):
        CronTrigger.from_crontab("99 * * * *")  # minute 99 out of range


def test_validate_cron_helper_accepts_valid() -> None:
    from cron_runner.api.v1.cron_jobs import _validate_cron

    _validate_cron("0 * * * *")   # should not raise
    _validate_cron("*/10 * * * *")


def test_validate_cron_helper_rejects_invalid() -> None:
    from cron_runner.api.v1.cron_jobs import _validate_cron
    from cron_runner.errors import InvalidCronExpressionError

    with pytest.raises(InvalidCronExpressionError):
        _validate_cron("not-valid-at-all")


# --------------------------------------------------------------------------- #
# Model import + new_id sanity
# --------------------------------------------------------------------------- #

def test_model_import_and_new_id() -> None:
    """CronJob ORM class must be importable; new_id must produce expected prefix."""
    from cron_runner.domain.models import CronJob, new_id

    job_id = new_id("cron")
    assert job_id.startswith("cron_"), f"Expected 'cron_' prefix, got {job_id!r}"
    # ID length: "cron_" (5) + 26 upper-hex chars = 31 total
    assert len(job_id) == 31

    # Verify all expected columns exist on the mapped class.
    expected_cols = {
        "id", "tenant_id", "app_id", "name", "cron_expression",
        "timezone", "input_values", "enabled", "source",
        "trigger_type", "field_mapping", "webhook_secret_hash", "created_by_user_id",
        "last_run_at", "next_run_at", "last_status", "last_error",
        "created_at", "updated_at",
    }
    mapper_cols = {c.key for c in CronJob.__table__.columns}
    assert expected_cols <= mapper_cols, f"Missing columns: {expected_cols - mapper_cols}"


def test_config_defaults() -> None:
    """Settings must be importable with expected default values."""
    from cron_runner.config import Settings

    s = Settings(
        db_dsn="sqlite+aiosqlite:///",
        db_dsn_migrator="sqlite+aiosqlite:///",
        bff_url="http://test-bff:8005",
        service_token="test_token",
    )
    assert s.http_port == 8013
    assert s.api_prefix == "/api/cron/v1"
    assert s.db_schema == "cron_runner"
    # service_user_id must match the platform usr_[0-9A-Z]{26} shape so that
    # downstream RBAC guards accept scheduler-fired runs.
    assert s.service_user_id == "usr_01SVCCRON00000000000000000"
