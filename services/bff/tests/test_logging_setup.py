"""JSON logging from startup, at the configured level, with or without OTel.

Everything below the "same in every service" line is identical in each
service's copy of this file, like ``logging_setup.py`` itself.
"""

from __future__ import annotations

import io
import json
import logging
import math
import sys
import warnings
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from agent_bff import logging_setup
from agent_bff.config import Settings
from agent_bff.logging_setup import JsonFormatter, configure_logging
from agent_bff.main import create_app
from agent_bff.observability import init_observability

SERVICE = "agent-bff"
PACKAGE = "agent_bff"
OTEL_ENV = "BFF_OTEL_ENABLED"


@pytest.fixture
def make_app(settings: Settings) -> Callable[[str], object]:
    """``create_app`` with this suite's test settings at the given log level."""

    def make(log_level: str) -> object:
        settings.log_level = log_level
        return create_app(settings)

    return make


def test_logging_is_set_up_with_otel_off(
    make_app: Callable[[str], object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(OTEL_ENV, "false")
    make_app("INFO")
    init_observability(service_name=SERVICE)  # the lifespan's first step; off, it returns
    logging.getLogger(PACKAGE).info("ready")

    entries = _json_lines(capsys.readouterr().err)
    assert [e["message"] for e in entries if e["logger"] == PACKAGE] == ["ready"]


def test_turning_otel_on_leaves_logging_alone(
    make_app: Callable[[str], object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OTEL_ENV, "true")
    make_app("WARNING")
    root = logging.getLogger()
    before = (root.level, list(root.handlers), logging.getLogger(PACKAGE).level)

    init_observability(service_name=SERVICE)

    assert (root.level, list(root.handlers), logging.getLogger(PACKAGE).level) == before


# ---------------------------------------------------------------------------
# The rest of this file is the same in every service.
# ---------------------------------------------------------------------------

_JSON_HANDLER = "service-json"
_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")
_SQL_ECHO = "sqlalchemy.engine.Engine"


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Undo configure_logging, so the rest of the suite keeps the logging it had.

    The root logger's other handlers are pytest's log capture, which changes
    per test phase, so only the JSON handler is put back.
    """
    root = logging.getLogger()
    root_level = root.level
    ours = [h for h in root.handlers if h.get_name() == _JSON_HANDLER]
    loggers = [logging.getLogger(name) for name in (PACKAGE, *_UVICORN, _SQL_ECHO)]
    saved = [(lg, lg.level, list(lg.handlers), lg.propagate) for lg in loggers]
    yield
    root.setLevel(root_level)
    for handler in [h for h in root.handlers if h.get_name() == _JSON_HANDLER]:
        root.removeHandler(handler)
    for handler in ours:
        root.addHandler(handler)
    for lg, level, handlers, propagate in saved:
        lg.setLevel(level)
        lg.handlers[:] = handlers
        lg.propagate = propagate


def _json_lines(err: str) -> list[dict[str, Any]]:
    """Each line written to stderr, parsed: a line that is not JSON fails the test."""
    return [json.loads(line) for line in err.splitlines()]


def _record(msg: str, *args: object, name: str = PACKAGE, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, None)
    record.__dict__.update(extra)
    return record


def test_quotes_backslashes_and_newlines_stay_inside_one_json_line() -> None:
    text = 'say "hi" \\ C:\\tmp\nnext line\ttab \x00 \u2028 \u00e9\u4e2d'
    line = JsonFormatter(SERVICE).format(_record("got %s", text))

    assert len(line.splitlines()) == 1
    entry = json.loads(line)
    assert entry["message"] == f"got {text}"
    assert (entry["level"], entry["service"], entry["logger"]) == ("INFO", SERVICE, PACKAGE)
    assert datetime.fromisoformat(entry["timestamp"]).tzinfo is not None


def test_a_traceback_is_one_escaped_field() -> None:
    try:
        raise ValueError('bad "value"\non two lines')
    except ValueError:
        record = _record("upload failed")
        record.exc_info = sys.exc_info()
    line = JsonFormatter(SERVICE).format(record)

    assert len(line.splitlines()) == 1
    entry = json.loads(line)
    assert entry["message"] == "upload failed"
    assert entry["exception"].startswith("Traceback (most recent call last):\n")
    assert entry["exception"].endswith('ValueError: bad "value"\non two lines')


def test_extra_fields_are_kept_and_odd_values_cannot_break_the_line() -> None:
    loop: dict[str, object] = {}
    loop["self"] = loop
    record = _record(
        "pool tick", count=3, ids=["a", "b"], ratio=math.nan, loop=loop, service="spoofed"
    )
    entry = json.loads(JsonFormatter(SERVICE).format(record))

    assert entry["count"] == 3
    assert entry["ids"] == ["a", "b"]
    assert entry["ratio"] == "nan"
    assert entry["loop"] == "{'self': {...}}"
    assert entry["service"] == SERVICE


def test_arguments_that_do_not_fit_the_message_still_give_json() -> None:
    entry = json.loads(JsonFormatter(SERVICE).format(_record("%d items", "many")))

    assert entry["message"].startswith("%d items")


def test_query_strings_are_cut_from_logged_urls() -> None:
    formatter = JsonFormatter(SERVICE)
    callback = "/api/bff/v1/auth/sso/callback?code=SECRET&state=s"
    access = _record(
        '%s - "%s %s HTTP/%s" %d', "10.0.0.7:41000", "GET", callback, "1.1", 302,
        name="uvicorn.access",
    )
    presigned = "http://minio:9000/skills/a.zip?X-Amz-Signature=SECRET"
    outbound = _record(
        'HTTP Request: %s %s "%s %d %s"', "GET", presigned, "HTTP/1.1", 200, "OK",
        name="httpx",
    )

    assert json.loads(formatter.format(access))["message"] == (
        '10.0.0.7:41000 - "GET /api/bff/v1/auth/sso/callback?[redacted] HTTP/1.1" 302'
    )
    assert json.loads(formatter.format(outbound))["message"] == (
        'HTTP Request: GET http://minio:9000/skills/a.zip?[redacted] "HTTP/1.1 200 OK"'
    )


@pytest.mark.parametrize(
    ("configured", "shown"),
    [
        ("DEBUG", ["DEBUG", "INFO", "WARNING"]),
        ("info", ["INFO", "WARNING"]),
        ("WARNING", ["WARNING"]),
    ],
)
def test_the_configured_level_is_honoured(
    configured: str, shown: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(service=SERVICE, level=configured)
    log = logging.getLogger(f"{PACKAGE}.somewhere")
    log.debug("d")
    log.info("i")
    log.warning("w")

    assert [e["level"] for e in _json_lines(capsys.readouterr().err)] == shown


def test_other_libraries_never_log_below_info() -> None:
    configure_logging(service=SERVICE, level="DEBUG")

    assert logging.getLogger(f"{PACKAGE}.somewhere").isEnabledFor(logging.DEBUG)
    for name in ("httpx", "httpcore.http11", "botocore.endpoint", "aiosqlite", "hpack.hpack"):
        assert not logging.getLogger(name).isEnabledFor(logging.DEBUG), name
        assert logging.getLogger(name).isEnabledFor(logging.INFO), name

    configure_logging(service=SERVICE, level="WARNING")

    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


def test_an_unknown_level_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown log level 'verbose'"):
        configure_logging(service=SERVICE, level="verbose")


def test_setting_up_again_replaces_only_its_own_handler() -> None:
    root = logging.getLogger()
    other = logging.NullHandler()
    root.addHandler(other)
    try:
        configure_logging(service=SERVICE, level="INFO")
        configure_logging(service=SERVICE, level="INFO")

        assert [h.get_name() for h in root.handlers].count(_JSON_HANDLER) == 1
        assert other in root.handlers
    finally:
        root.removeHandler(other)


def test_uvicorn_logs_come_out_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    # What uvicorn's LOGGING_CONFIG has set up by the time it loads the app.
    plain = logging.StreamHandler(io.StringIO())
    for name in ("uvicorn", "uvicorn.access"):
        logging.getLogger(name).addHandler(plain)
        logging.getLogger(name).propagate = False

    configure_logging(service=SERVICE, level="INFO")
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 7)
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "10.0.0.7:41000", "GET", "/healthz", "1.1", 200
    )

    entries = _json_lines(capsys.readouterr().err)
    assert [(e["logger"], e["message"]) for e in entries] == [
        ("uvicorn.error", "Started server process [7]"),
        ("uvicorn.access", '10.0.0.7:41000 - "GET /healthz HTTP/1.1" 200'),
    ]
    assert plain.stream.getvalue() == ""


def test_a_switched_off_access_log_stays_off(capsys: pytest.CaptureFixture[str]) -> None:
    # ``uvicorn --no-access-log`` leaves the access logger without handlers.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False

    configure_logging(service=SERVICE, level="INFO")
    access.info("GET /healthz")

    assert capsys.readouterr().err == ""


def test_sql_echo_comes_out_as_json_without_parameters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    logging.getLogger(_SQL_ECHO).handlers.clear()  # as in a fresh process

    configure_logging(service=SERVICE, level="WARNING")
    engine = sqlalchemy.create_engine("sqlite://", echo=True, hide_parameters=True)
    with engine.connect() as connection:
        connection.execute(sqlalchemy.text("SELECT :token"), {"token": "hunter2"})
    engine.dispose()

    captured = capsys.readouterr()
    assert captured.out == ""  # no plain-text handler of SQLAlchemy's own
    echoed = [e["message"] for e in _json_lines(captured.err) if e["logger"] == _SQL_ECHO]
    assert "SELECT ?" in echoed
    assert "hunter2" not in captured.err


def test_warnings_become_json_lines(capsys: pytest.CaptureFixture[str]) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        logging.captureWarnings(False)  # start from the interpreter's own hook
        configure_logging(service=SERVICE, level="INFO")
        try:
            warnings.warn("old call", DeprecationWarning, stacklevel=1)
        finally:
            logging.captureWarnings(False)

    (entry,) = _json_lines(capsys.readouterr().err)
    assert entry["logger"] == "py.warnings"
    assert "DeprecationWarning: old call" in entry["message"]


def test_create_app_sets_up_json_logging_at_the_configured_level(
    make_app: Callable[[str], object], capsys: pytest.CaptureFixture[str]
) -> None:
    make_app("DEBUG")
    logging.getLogger(f"{PACKAGE}.startup").debug('ready "now"\nreally')

    entries = _json_lines(capsys.readouterr().err)
    assert [
        (e["level"], e["service"], e["message"])
        for e in entries
        if e["logger"] == f"{PACKAGE}.startup"
    ] == [("DEBUG", SERVICE, 'ready "now"\nreally')]


def test_create_app_refuses_an_unknown_level(make_app: Callable[[str], object]) -> None:
    with pytest.raises(ValueError, match="unknown log level"):
        make_app("verbose")


def test_every_service_has_the_same_logging_setup() -> None:
    mine = Path(logging_setup.__file__).resolve()
    services = mine.parents[3]  # services/<service>/src/<package>/logging_setup.py
    if services.name != "services":
        pytest.skip("not running from the repository checkout")
    copies = sorted(services.glob("*/src/*/logging_setup.py"))
    apps = sorted(services.glob("*/src/*/main.py"))

    assert [c.parent for c in copies] == [a.parent for a in apps], (
        "every service with a main.py needs logging_setup.py"
    )
    mine_bytes = mine.read_bytes()
    different = [str(c.relative_to(services)) for c in copies if c.read_bytes() != mine_bytes]
    assert different == [], "logging_setup.py must stay identical in every service"
