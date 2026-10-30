"""Foundation-DLP client for the session-orchestrator's data-flow controls.

Three entry points, one per kind of call site (bare ``httpx.AsyncClient``,
tenant forwarded as ``X-Tenant-Id``, 2-second timeout, like ``authz_guard``):

``check_download(...)`` (attachment and workspace-file downloads)
    ``POST {dlp_url}/core/check:download`` with ``X-Tenant-Id`` +
    ``X-User-Id`` and ``{resource_type, resource_id, destination}``.  DLP
    resolves the user's clearance itself (it refuses a caller-supplied one)
    and compares it with the resource's classification label.

``scan_upload(...)`` (attachment and workspace-file uploads)
    ``POST {dlp_url}/core/scan`` as a trusted ``X-Actor: SERVICE`` caller, so
    DLP records the hit level as the resource's classification label, which
    ``check:download`` then enforces.  A hit never rejects the upload.  Call it
    after the row exists (the label is keyed by its id) and before the blob is
    written.

``scan_and_tag(...)`` (assistant transcript messages at run finalization)
    The same scan, but it never raises (see "Persisted output" below).

Failure policy
--------------
The worker's PreToolUse hook (``worker-claude-sdk/src/dlp.ts``) applies the
same decision table to tool calls; ``contracts/worker-wire-contract.md``
documents both.

* ``dlp_url`` empty: DLP is disabled for the deployment.  Everything is
  allowed and the policy is logged once.
* DLP answers: its verdict applies.  A denied download raises
  ``DlpDownloadDeniedError`` (HTTP 403 at the call site); a scan hit labels the
  resource and the upload is kept.
* The call cannot complete (network error, timeout, non-2xx, a body that is
  not a JSON object, or one without the expected field):
  ``SESSION_ORCHESTRATOR_DLP_FAILURE_MODE`` decides.

  - ``closed`` (the default, also used for an unrecognised value): raise
    ``DlpUnavailableError``, rendered as HTTP 503 ``AGENT.DLP.UNAVAILABLE``.
    A refused upload stores nothing: the request transaction rolls the row
    back, and the blob has not been written yet.
  - ``open``: the upload or download proceeds unchecked, and every such
    operation is logged at ERROR with its resource id.

Persisted output (``scan_and_tag``) is the deliberate exception: on a failed
scan it is neither blocked, held back nor redacted, in either mode.  The reply
was already streamed to the user token by token before run finalization
persists and scans it, so refusing or hiding the stored copy would prevent no
disclosure and would only destroy the user's record of what they saw.
Redaction needs the very scan that failed.  Nothing in agent-platform reads a
transcript label to make an access decision, and DLP treats an unlabelled
resource as its configured default level (L3).  So the message is kept,
unlabelled, and an ERROR log names it so it can be rescanned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_session_orchestrator.errors import OrchestratorError

logger = logging.getLogger(__name__)

# Tight timeout, the same as the authz guard and the worker's DLP hook. DLP sits
# on the upload/download path and must not add meaningful latency.
_DLP_TIMEOUT = 2.0  # seconds

# At most this many distinct rule names are quoted in a scan-hit log line.
_MAX_RULE_NAMES = 3

FailureMode = Literal["open", "closed"]
Operation = Literal["upload", "download"]

_FAILURE_MODE_ENV = "SESSION_ORCHESTRATOR_DLP_FAILURE_MODE"


class _DlpPolicySettings(BaseSettings):
    """Reads ``SESSION_ORCHESTRATOR_DLP_FAILURE_MODE`` from the env or ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="SESSION_ORCHESTRATOR_",
        env_file=".env",
        extra="ignore",
    )

    dlp_failure_mode: str = "closed"


def parse_failure_mode(raw: str | None) -> FailureMode | None:
    """Parse a failure-mode setting.

    ``"open"`` and ``"closed"`` (any case, surrounding blanks ignored) map to
    themselves and an empty value means ``"closed"``.  Anything else returns
    ``None``, which callers treat as ``"closed"``.
    """
    value = (raw or "").strip().lower()
    if value == "open":
        return "open"
    if value in ("", "closed"):
        return "closed"
    return None


@lru_cache(maxsize=1)
def configured_failure_mode() -> FailureMode:
    """The deployment's DLP failure mode, read once per process."""
    raw = _DlpPolicySettings().dlp_failure_mode
    mode = parse_failure_mode(raw)
    if mode is None:
        logger.error(
            "%s=%r is neither 'open' nor 'closed'; using 'closed'", _FAILURE_MODE_ENV, raw,
        )
        return "closed"
    return mode


# Policies already logged by ``log_dlp_policy`` (one per process in practice).
_logged_policies: set[tuple[str, FailureMode]] = set()


def log_dlp_policy(dlp_url: str, failure_mode: FailureMode | None = None) -> None:
    """Log the effective DLP policy, once per process.

    Every entry point calls this before doing anything else, so the policy is
    in the logs from the first DLP decision on.  Safe to call at startup too.
    """
    mode = failure_mode or configured_failure_mode()
    key = (dlp_url, mode)
    if key in _logged_policies:
        return
    _logged_policies.add(key)
    if not dlp_url:
        logger.warning(
            "DLP disabled: SESSION_ORCHESTRATOR_FOUNDATION_DLP_URL is empty, so "
            "uploads, downloads and assistant messages are not checked",
        )
    elif mode == "open":
        logger.warning(
            "DLP enabled (DLP_FAILURE_MODE=open): an upload or download whose DLP "
            "check cannot complete proceeds UNCHECKED, and each one is logged as an error",
        )
    else:
        logger.info(
            "DLP enabled (DLP_FAILURE_MODE=closed): an upload or download whose DLP "
            "check cannot complete is refused with HTTP 503",
        )


class DlpDownloadDeniedError(Exception):
    """Raised when DLP explicitly denies a download (``allowed=false``).

    Carries the DLP-provided ``reason`` so the API layer can surface it in the
    403 detail.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class DlpUnavailableError(OrchestratorError):
    """DLP could not be consulted and the failure mode is closed (HTTP 503).

    ``detail`` and ``user_message`` are shown to the user, so they never carry
    the technical cause; that goes to the ERROR log instead.
    """

    def __init__(self, operation: Operation) -> None:
        if operation == "upload":
            detail = (
                "The data-loss-prevention (DLP) scan of this upload could not be "
                "completed, so the file was not stored."
            )
            user_message = (
                "This file can't be uploaded right now because the data-protection "
                "scan is unavailable. Please try again in a few minutes, and contact "
                "your administrator if it keeps failing."
            )
        else:
            detail = (
                "The data-loss-prevention (DLP) check for this download could not be "
                "completed, so the file was not served."
            )
            user_message = (
                "This file can't be downloaded right now because the data-protection "
                "check is unavailable. Please try again in a few minutes, and contact "
                "your administrator if it keeps failing."
            )
        super().__init__(
            detail, code="AGENT.DLP.UNAVAILABLE", status=503, user_message=user_message,
        )
        self.operation = operation


@dataclass(frozen=True)
class _Incomplete:
    """A DLP call that produced no usable answer. ``detail`` is for logs only."""

    detail: str


@dataclass(frozen=True)
class _Verdict:
    action: str
    max_sensitivity: str | None


def _describe_http_failure(resp: httpx.Response) -> str:
    """``HTTP <status>`` plus the problem ``code`` (or ``detail``), for logs."""
    why = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = body.get("code")
        text = code if isinstance(code, str) else body.get("detail")
        if isinstance(text, str) and text:
            why = f" {text[:120]}"
    return f"HTTP {resp.status_code}{why}"


async def _post_json(
    url: str, *, payload: dict[str, Any], headers: dict[str, str],
) -> dict[str, Any] | _Incomplete:
    """POST to DLP and return its JSON object, or why there is none."""
    try:
        async with httpx.AsyncClient(timeout=_DLP_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        return _Incomplete(f"no answer within {_DLP_TIMEOUT:g}s")
    except Exception as exc:  # noqa: BLE001 - any client error means the call did not complete
        return _Incomplete(f"{type(exc).__name__}: {exc}"[:200])
    if not 200 <= resp.status_code < 300:
        return _Incomplete(_describe_http_failure(resp))
    try:
        body = resp.json()
    except ValueError:
        return _Incomplete(f"HTTP {resp.status_code} with a body that is not JSON")
    if not isinstance(body, dict):
        return _Incomplete(f"HTTP {resp.status_code} with a JSON body that is not an object")
    return body


def _rule_names(hits: object) -> list[str]:
    """Distinct rule names from DLP hits, for the logs."""
    names: list[str] = []
    if isinstance(hits, list):
        for hit in hits:
            name = hit.get("rule_name") if isinstance(hit, dict) else None
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    return names[:_MAX_RULE_NAMES]


async def _scan(
    dlp_url: str,
    *,
    tenant_id: str,
    user_id: str | None,
    content: str,
    resource_type: str,
    resource_id: str,
) -> _Verdict | _Incomplete:
    """Scan ``content`` as a SERVICE caller, so DLP also labels the resource."""
    headers = {"X-Tenant-Id": tenant_id, "X-Actor": "SERVICE"}
    if user_id:
        headers["X-User-Id"] = user_id
    result = await _post_json(
        f"{dlp_url.rstrip('/')}/core/scan",
        payload={
            "content": content,
            "resource_type": resource_type,
            "resource_id": resource_id,
        },
        headers=headers,
    )
    if isinstance(result, _Incomplete):
        return result
    action = result.get("action")
    if not isinstance(action, str) or not action:
        return _Incomplete("HTTP 2xx without a string 'action'")
    max_sensitivity = result.get("max_sensitivity")
    verdict = _Verdict(
        action=action,
        max_sensitivity=max_sensitivity if isinstance(max_sensitivity, str) else None,
    )
    if action != "ALLOW":
        logger.warning(
            "DLP flagged %s %s: action=%s max_sensitivity=%s rules=%s; DLP has labelled it",
            resource_type,
            resource_id,
            action,
            verdict.max_sensitivity,
            ", ".join(_rule_names(result.get("hits"))) or "-",
        )
    return verdict


def _cannot_complete(
    *,
    operation: Operation,
    mode: FailureMode,
    resource_type: str,
    resource_id: str,
    detail: str,
) -> None:
    """Apply the failure mode: log loudly, then raise (closed) or return (open)."""
    call = "scan" if operation == "upload" else "check:download"
    if mode == "open":
        logger.error(
            "DLP %s for %s %s could not complete (%s); the %s proceeds UNCHECKED "
            "because DLP_FAILURE_MODE=open",
            call, resource_type, resource_id, detail, operation,
        )
        return
    logger.error(
        "DLP %s for %s %s could not complete (%s); refusing the %s with HTTP 503 "
        "(DLP_FAILURE_MODE=closed)",
        call, resource_type, resource_id, detail, operation,
    )
    raise DlpUnavailableError(operation)


async def check_download(
    *,
    dlp_url: str,
    tenant_id: str,
    user_id: str,
    resource_id: str,
    resource_type: str = "attachment",
    destination: str = "laptop",
    failure_mode: FailureMode | None = None,
) -> None:
    """Ask DLP whether ``user_id`` may download a resource.

    Returns when the download may proceed.  Raises ``DlpDownloadDeniedError``
    when DLP says no, and ``DlpUnavailableError`` when the check cannot
    complete and the failure mode is closed.  ``failure_mode`` defaults to
    ``SESSION_ORCHESTRATOR_DLP_FAILURE_MODE``.
    """
    mode = failure_mode or configured_failure_mode()
    log_dlp_policy(dlp_url, mode)
    if not dlp_url:
        return
    result = await _post_json(
        f"{dlp_url.rstrip('/')}/core/check:download",
        payload={
            "resource_type": resource_type,
            "resource_id": resource_id,
            "destination": destination,
        },
        headers={"X-Tenant-Id": tenant_id, "X-User-Id": user_id},
    )
    if isinstance(result, _Incomplete):
        detail = result.detail
    else:
        allowed = result.get("allowed")
        if allowed is True:
            return
        if allowed is False:
            reason = str(result.get("reason") or "blocked by data-loss-prevention policy")
            logger.info(
                "DLP check:download denied %s %s to user %s: %s",
                resource_type, resource_id, user_id, reason,
            )
            raise DlpDownloadDeniedError(reason)
        detail = "HTTP 2xx without a boolean 'allowed'"
    _cannot_complete(
        operation="download",
        mode=mode,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
    )


async def scan_upload(
    *,
    dlp_url: str,
    tenant_id: str,
    user_id: str,
    content: str,
    resource_id: str,
    resource_type: str,
    failure_mode: FailureMode | None = None,
) -> str | None:
    """Scan an upload's text before its bytes are stored.

    DLP labels the resource with the hit level; a hit never rejects the
    upload.  Returns DLP's ``action``, or ``None`` when nothing was scanned
    (DLP disabled, or the scan failed in open mode).  Raises
    ``DlpUnavailableError`` when the scan cannot complete and the failure mode
    is closed.  ``failure_mode`` defaults to
    ``SESSION_ORCHESTRATOR_DLP_FAILURE_MODE``.
    """
    mode = failure_mode or configured_failure_mode()
    log_dlp_policy(dlp_url, mode)
    if not dlp_url:
        return None
    verdict = await _scan(
        dlp_url,
        tenant_id=tenant_id,
        user_id=user_id,
        content=content,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    if isinstance(verdict, _Incomplete):
        _cannot_complete(
            operation="upload",
            mode=mode,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=verdict.detail,
        )
        return None
    return verdict.action


async def scan_and_tag(
    *,
    dlp_url: str,
    tenant_id: str,
    content: str,
    resource_id: str,
    resource_type: str,
) -> None:
    """Scan persisted output (an assistant message) so DLP labels it.

    Never raises, whatever the failure mode: a scan that cannot complete leaves
    the message unlabelled and is logged at ERROR (see "Persisted output" in
    the module docstring for why it is not blocked, held back or redacted).
    """
    log_dlp_policy(dlp_url)
    if not dlp_url or not content:
        return
    verdict = await _scan(
        dlp_url,
        tenant_id=tenant_id,
        user_id=None,
        content=content,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    if isinstance(verdict, _Incomplete):
        logger.error(
            "DLP scan for %s %s could not complete (%s); it stays UNLABELLED. "
            "Persisted output is kept on a DLP failure (it was already streamed "
            "to the user); rescan it once DLP is back",
            resource_type, resource_id, verdict.detail,
        )
