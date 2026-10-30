"""Process-wide JSON logging, set up once at startup by ``create_app``.

Every service package under ``services/*/src/`` carries an identical copy of
this module: each image ships only its own ``src/``, so there is no shared
package to put it in.  Each service's ``test_logging_setup.py`` fails when its
copy differs from the others.

``configure_logging`` does not depend on OpenTelemetry being on.  It:

- writes every record to stderr as one JSON object per line, built with
  ``json.dumps``, so quotes, backslashes, newlines and tracebacks cannot break
  the line.  Fields: ``timestamp``, ``level``, ``service``, ``logger``,
  ``message``, ``exception`` / ``stack`` when present, then the record's
  ``extra=`` values;
- applies the configured level (``<PREFIX>LOG_LEVEL``) to the service's own
  package.  Every other logger (uvicorn, httpx, botocore, SQLAlchemy, ...)
  stays at INFO or above, because their DEBUG output carries request and
  response headers, request bodies and bound SQL parameters;
- sends uvicorn's startup and access logs, and SQL echo when a service turns
  it on, through the same handler;
- cuts the query string out of the request URLs that uvicorn and httpx log,
  since it can carry a secret (an OAuth ``code``, a presigned S3 signature);
- routes Python warnings into logging, so they are JSON lines too.

Calling it again replaces the handler it installed before and leaves every
other handler alone (pytest's log capture, for one).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

# The service's top-level package (``agent_bff``, ``skill_store``, ...), taken
# from this module's own name so that every copy stays identical.
_PACKAGE = __name__.partition(".")[0]

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# Loggers outside the service's package never go below this level.
_LIBRARY_FLOOR = logging.INFO

_HANDLER_NAME = "service-json"

# uvicorn's default ``LOGGING_CONFIG`` gives these their own plain-text handlers.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Loggers that pass a request URL as a message argument.
_URL_LOGGERS = frozenset({"uvicorn.access", "uvicorn.error", "httpx"})

# Attributes every LogRecord has: anything else on a record came from
# ``extra=``.  ``color_message`` is uvicorn's ANSI-coloured copy of the message.
_RECORD_ATTRIBUTES = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "color_message",
}


def configure_logging(*, service: str, level: str) -> None:
    """Log every record to stderr as JSON, at ``level`` for the service's code.

    Raises ``ValueError`` for an unknown level name, so a typo in
    ``<PREFIX>LOG_LEVEL`` stops the service at startup.
    """
    service_level = _parse_level(level)

    handler = _StderrHandler()
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter(service))

    root = logging.getLogger()
    for old in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(max(service_level, _LIBRARY_FLOOR))
    logging.getLogger(_PACKAGE).setLevel(service_level)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        # A uvicorn logger without handlers was switched off on purpose
        # (``--no-access-log``), so it stays off.
        if uvicorn_logger.handlers:
            uvicorn_logger.handlers.clear()
            uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(logging.NOTSET)

    # ``create_engine(echo=True)`` gives this logger a plain-text stdout handler
    # of its own unless it already has one: with this one there, SQL echo
    # (opt-in, parameters hidden) comes out through the JSON handler instead.
    sql_logger = logging.getLogger("sqlalchemy.engine.Engine")
    if not sql_logger.handlers:
        sql_logger.addHandler(logging.NullHandler())

    logging.captureWarnings(True)


class JsonFormatter(logging.Formatter):
    """Formats a record as one line of valid JSON, whatever it contains."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "message": _message(record),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key not in _RECORD_ATTRIBUTES and key not in entry:
                entry[key] = value
        try:
            return json.dumps(entry, default=_text, allow_nan=False)
        except Exception:
            # An ``extra=`` value json cannot take even through ``default``
            # (a circular structure, a dict with tuple keys, a NaN): that
            # value goes in as text, the others as they are.
            entry = {key: _jsonable(value) for key, value in entry.items()}
            return json.dumps(entry, default=_text, allow_nan=False)


class _StderrHandler(logging.Handler):
    """Writes to ``sys.stderr`` as it is at the time of each write.

    Keeping the stream from setup time would break once something swaps
    ``sys.stderr`` (pytest's capture does, per test).
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


def _parse_level(value: str) -> int:
    try:
        return _LEVELS[value.strip().upper()]
    except KeyError:
        raise ValueError(
            f"unknown log level {value!r}; use one of {', '.join(_LEVELS)}"
        ) from None


def _message(record: logging.LogRecord) -> str:
    """``record.getMessage()``, minus URL query strings, and it never raises."""
    msg = _text(record.msg)
    args = record.args
    if not args:
        return msg
    if record.name in _URL_LOGGERS and isinstance(args, tuple):
        args = tuple(_without_query(arg) for arg in args)
    try:
        return msg % args
    except Exception as exc:
        return f"{msg} [arguments do not fit the message: {type(exc).__name__}]"


def _without_query(value: object) -> object:
    if isinstance(value, int | float):
        return value  # status codes and ports, formatted with %d
    path, separator, _ = _text(value).partition("?")
    return f"{path}?[redacted]" if separator else value


def _jsonable(value: object) -> object:
    try:
        json.dumps(value, default=_text, allow_nan=False)
    except Exception:
        return _text(value)
    return value


def _text(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return f"<unprintable {type(value).__name__}>"
