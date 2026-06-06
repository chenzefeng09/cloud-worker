"""Async HTTP fetcher with SSRF protection and conservative redirect handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from web_fetch_gateway.config import Settings
from web_fetch_gateway.errors import (
    ContentTypeRejectedError,
    FetchTimeoutError,
    FetchUpstreamError,
)
from web_fetch_gateway.security import (
    check_domain_allowed,
    check_url_scheme,
    resolve_and_check_ip,
)

# Headers that must be stripped from caller-supplied request headers to prevent
# information leakage and header-injection attacks.
_STRIP_REQUEST_HEADERS = frozenset(
    {"host", "x-forwarded-for", "x-real-ip", "authorization"}
)

_MAX_REDIRECTS = 5

_ALLOWED_METHODS = frozenset({"GET", "HEAD"})

# Content-type prefixes accepted when restrict_content_types is True.
_ALLOWED_CT_PREFIXES = ("text/", "application/json")


@dataclass
class FetchResult:
    status_code: int
    content_type: str
    body: str
    truncated: bool = field(default=False)


def _run_security_checks(url: str, settings: Settings) -> None:
    """Run the full SSRF security chain synchronously (before any await)."""
    check_url_scheme(url)
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    check_domain_allowed(hostname, settings.allowed_domains)
    resolve_and_check_ip(hostname)


async def fetch(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    settings: Settings,
    max_bytes: int,
) -> FetchResult:
    """Fetch *url* with SSRF protection and conservative redirect following.

    Security invariants
    -------------------
    - All security checks (scheme, domain allowlist, IP blocklist) run
      **synchronously before any await** so they complete before any I/O.
    - Redirects are followed manually (``follow_redirects=False``) so that
      every redirect target is re-validated through the full security chain.
    - TLS is always verified (``verify=True``).
    - Only GET and HEAD are accepted.
    - Sensitive request headers are stripped before forwarding.
    """
    method = method.upper()
    if method not in _ALLOWED_METHODS:
        raise FetchUpstreamError(f"Method {method!r} is not allowed; use GET or HEAD")

    # Sanitise caller headers — strip headers that must not be forwarded.
    safe_headers = {
        k: v for k, v in headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS
    }

    # --- All security checks run synchronously, BEFORE the first await ---
    _run_security_checks(url, settings)

    current_url = url
    redirect_count = 0

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            verify=True,
            timeout=settings.fetch_timeout_seconds,
        ) as client:
            while True:
                response = await client.request(
                    method=method,
                    url=current_url,
                    headers=safe_headers,
                )

                if response.is_redirect:
                    if redirect_count >= _MAX_REDIRECTS:
                        raise FetchUpstreamError(
                            f"Too many redirects (>{_MAX_REDIRECTS}) following {url!r}"
                        )
                    redirect_count += 1
                    location = response.headers.get("location", "")
                    if not location:
                        raise FetchUpstreamError(
                            "Redirect response missing Location header"
                        )
                    # Re-run full security checks on every redirect target.
                    _run_security_checks(location, settings)
                    current_url = location
                    continue

                # Non-redirect response — validate content-type if required.
                raw_ct = response.headers.get("content-type", "")
                # Extract just the media-type part (strip ; charset=…)
                content_type = raw_ct.split(";")[0].strip().lower()

                if settings.restrict_content_types:
                    if not any(content_type.startswith(p) for p in _ALLOWED_CT_PREFIXES):
                        raise ContentTypeRejectedError(content_type or "(none)")

                # Read body up to max_bytes+1 to detect truncation.
                raw = await response.aread()
                truncated = len(raw) > max_bytes
                if truncated:
                    raw = raw[:max_bytes]

                # Decode: prefer UTF-8, fall back to Latin-1.
                try:
                    body = raw.decode("utf-8")
                except UnicodeDecodeError:
                    body = raw.decode("latin-1")

                return FetchResult(
                    status_code=response.status_code,
                    content_type=content_type,
                    body=body,
                    truncated=truncated,
                )

    except (ContentTypeRejectedError, FetchUpstreamError, FetchTimeoutError):
        # Re-raise our own errors unchanged.
        raise
    except httpx.TimeoutException as exc:
        raise FetchTimeoutError(url) from exc
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise FetchUpstreamError(str(exc)) from exc
