"""SSRF protection utilities.

These checks run synchronously before any network I/O and form the core
defence-in-depth against Server-Side Request Forgery:

1. check_url_scheme  — only http/https are allowed; file://, ftp://, etc. are
   rejected immediately.
2. check_domain_allowed — validates the hostname against the operator-supplied
   allowlist before a DNS query is performed.
3. resolve_and_check_ip — resolves the hostname to IP addresses and rejects any
   address that falls inside a private, loopback, link-local, or reserved range.

Known residual risk — DNS rebinding
------------------------------------
There is a window between resolve_and_check_ip (pre-connect) and the actual TCP
connection in which a malicious DNS server could serve a public IP during our
check and then rebind to an internal IP when the real connection is established.
The domain allowlist substantially shrinks the attack surface because only
operator-approved hostnames ever reach the DNS resolver.  Operators should also
set allowed_domains to the minimal set of real external domains their agents
need and avoid using the wildcard "*" value outside of local development.

A future mitigation (not implemented here) would be to use a custom
``httpx.AsyncTransport`` that validates the connected socket's remote address
after the TCP handshake completes.
"""

from __future__ import annotations

import ipaddress
import socket

from web_fetch_gateway.errors import (
    DomainNotAllowedError,
    FetchGatewayError,
    PrivateAddressError,
)

# ---------------------------------------------------------------------------
# Private / reserved IP ranges that must never be reachable via this gateway.
# This list is hard-coded and not user-configurable by design.
# ---------------------------------------------------------------------------
_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    # IPv4
    ipaddress.IPv4Network("127.0.0.0/8"),       # Loopback
    ipaddress.IPv4Network("10.0.0.0/8"),        # RFC-1918 class A
    ipaddress.IPv4Network("172.16.0.0/12"),     # RFC-1918 class B
    ipaddress.IPv4Network("192.168.0.0/16"),    # RFC-1918 class C
    ipaddress.IPv4Network("169.254.0.0/16"),    # Link-local / AWS metadata (169.254.169.254)
    ipaddress.IPv4Network("100.64.0.0/10"),     # Shared address space (RFC-6598 / Tailscale)
    ipaddress.IPv4Network("0.0.0.0/8"),         # "This" network
    ipaddress.IPv4Network("255.255.255.255/32"),# Broadcast
    # IPv6
    ipaddress.IPv6Network("::1/128"),           # Loopback
    ipaddress.IPv6Network("fc00::/7"),          # Unique local
    ipaddress.IPv6Network("fe80::/10"),         # Link-local
]


def check_url_scheme(url: str) -> None:
    """Raise FetchGatewayError for any scheme other than http or https."""
    from urllib.parse import urlparse

    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise FetchGatewayError(
            f"URL scheme {scheme!r} is not allowed; only http and https are permitted",
            code="FETCH.INVALID_SCHEME",
            status=400,
        )


def check_domain_allowed(hostname: str, allowed_domains: str) -> None:
    """Raise DomainNotAllowedError if hostname is not covered by allowed_domains.

    Rules
    -----
    - ``allowed_domains="*"`` bypasses all domain checks (dev only).
    - Otherwise the value is a comma-separated list of entries:
      - Exact match: ``"example.com"`` allows only ``example.com``.
      - Suffix match: ``".example.com"`` allows ``example.com`` and any
        subdomain such as ``api.example.com``.
    """
    stripped = allowed_domains.strip()
    if stripped == "*":
        return  # wildcard — no domain restriction

    entries = [e.strip() for e in stripped.split(",") if e.strip()]
    hostname_lower = hostname.lower()

    for entry in entries:
        entry_lower = entry.lower()
        if entry_lower.startswith("."):
            # Suffix match: ".example.com" matches "example.com" and sub.example.com
            domain_root = entry_lower[1:]  # e.g. "example.com"
            if hostname_lower == domain_root or hostname_lower.endswith(entry_lower):
                return
        else:
            # Exact match
            if hostname_lower == entry_lower:
                return

    raise DomainNotAllowedError(hostname)


def resolve_and_check_ip(hostname: str) -> str:
    """Resolve *hostname* and raise PrivateAddressError if any resolved IP is
    private, loopback, link-local, multicast, or otherwise reserved.

    Returns the first public IP address string (useful for audit logging).

    Note: this function performs a **synchronous** DNS lookup.  It must be
    called before any async I/O (i.e. before the ``await`` in the fetcher)
    so that it runs in the calling thread without blocking the event loop for
    a significant duration.  For high-throughput deployments the caller may
    want to push this into a thread-pool executor; at current agent-platform
    load this is acceptable.
    """
    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise FetchGatewayError(
            f"DNS resolution failed for {hostname!r}: {exc}",
            code="FETCH.DNS_RESOLUTION_FAILED",
            status=502,
        ) from exc

    first_public: str | None = None

    for _family, _type, _proto, _canonname, sockaddr in results:
        raw_ip = sockaddr[0]
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue

        # Check hard-coded blocked networks first.
        blocked = any(addr in net for net in _BLOCKED_NETWORKS)

        # Belt-and-suspenders: also use stdlib helpers to catch edge cases not
        # explicitly enumerated above (e.g. future reserved ranges).
        if not blocked:
            blocked = (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_reserved
                or addr.is_multicast
            )

        if blocked:
            raise PrivateAddressError(hostname)

        if first_public is None:
            first_public = raw_ip

    if first_public is None:
        raise FetchGatewayError(
            f"No resolvable public address for {hostname!r}",
            code="FETCH.NO_PUBLIC_ADDRESS",
            status=502,
        )

    return first_public
