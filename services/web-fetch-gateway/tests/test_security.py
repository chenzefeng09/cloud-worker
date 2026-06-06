"""Unit tests for web_fetch_gateway.security.

All tests are synchronous (security.py has no async code).
socket.getaddrinfo is monkeypatched to avoid real DNS lookups.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from web_fetch_gateway.errors import (
    DomainNotAllowedError,
    FetchGatewayError,
    PrivateAddressError,
)
from web_fetch_gateway.security import (
    check_domain_allowed,
    check_url_scheme,
    resolve_and_check_ip,
)


# ---------------------------------------------------------------------------
# check_url_scheme
# ---------------------------------------------------------------------------

class TestCheckUrlScheme:
    def test_http_allowed(self) -> None:
        check_url_scheme("http://example.com/path")  # must not raise

    def test_https_allowed(self) -> None:
        check_url_scheme("https://example.com/path")  # must not raise

    def test_file_rejected(self) -> None:
        with pytest.raises(FetchGatewayError) as exc_info:
            check_url_scheme("file:///etc/passwd")
        assert exc_info.value.code == "FETCH.INVALID_SCHEME"
        assert exc_info.value.status == 400

    def test_ftp_rejected(self) -> None:
        with pytest.raises(FetchGatewayError):
            check_url_scheme("ftp://files.example.com/pub/data")

    def test_javascript_rejected(self) -> None:
        with pytest.raises(FetchGatewayError):
            check_url_scheme("javascript:alert(1)")

    def test_gopher_rejected(self) -> None:
        with pytest.raises(FetchGatewayError):
            check_url_scheme("gopher://evil.example.com/")


# ---------------------------------------------------------------------------
# check_domain_allowed
# ---------------------------------------------------------------------------

class TestCheckDomainAllowed:
    def test_wildcard_allows_everything(self) -> None:
        check_domain_allowed("totally-internal.corp", "*")  # must not raise

    def test_exact_match_passes(self) -> None:
        check_domain_allowed("example.com", "example.com")

    def test_exact_match_different_domain_rejected(self) -> None:
        with pytest.raises(DomainNotAllowedError):
            check_domain_allowed("evil.com", "example.com")

    def test_suffix_match_subdomain_passes(self) -> None:
        # ".wikipedia.org" should match "en.wikipedia.org"
        check_domain_allowed("en.wikipedia.org", ".wikipedia.org")

    def test_suffix_match_root_domain_passes(self) -> None:
        # ".wikipedia.org" should also match "wikipedia.org" itself
        check_domain_allowed("wikipedia.org", ".wikipedia.org")

    def test_suffix_match_different_domain_rejected(self) -> None:
        with pytest.raises(DomainNotAllowedError):
            check_domain_allowed("evil.org", ".wikipedia.org")

    def test_suffix_partial_match_rejected(self) -> None:
        # "notwikipedia.org" must NOT match ".wikipedia.org"
        with pytest.raises(DomainNotAllowedError):
            check_domain_allowed("notwikipedia.org", ".wikipedia.org")

    def test_multiple_entries_first_matches(self) -> None:
        check_domain_allowed("example.com", "example.com,.wikipedia.org")

    def test_multiple_entries_second_matches(self) -> None:
        check_domain_allowed("en.wikipedia.org", "example.com,.wikipedia.org")

    def test_multiple_entries_none_match(self) -> None:
        with pytest.raises(DomainNotAllowedError):
            check_domain_allowed("attacker.com", "example.com,.wikipedia.org")

    def test_empty_allowed_domains_rejects_all(self) -> None:
        with pytest.raises(DomainNotAllowedError):
            check_domain_allowed("example.com", "")

    def test_case_insensitive(self) -> None:
        # Hostnames and entries should be compared case-insensitively.
        check_domain_allowed("EXAMPLE.COM", "example.com")


# ---------------------------------------------------------------------------
# resolve_and_check_ip
# ---------------------------------------------------------------------------

def _mock_getaddrinfo(ip: str):
    """Return a lambda that fakes socket.getaddrinfo returning a single IP."""
    return lambda host, port, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))
    ]


class TestResolveAndCheckIp:
    def test_public_ip_passes(self) -> None:
        with patch("socket.getaddrinfo", _mock_getaddrinfo("93.184.216.34")):
            # 93.184.216.34 is example.com — public address should pass
            result = resolve_and_check_ip("example.com")
        assert result == "93.184.216.34"

    def test_loopback_rejected(self) -> None:
        with patch("socket.getaddrinfo", _mock_getaddrinfo("127.0.0.1")):
            with pytest.raises(PrivateAddressError) as exc_info:
                resolve_and_check_ip("localhost")
        assert exc_info.value.code == "FETCH.PRIVATE_ADDRESS"
        assert exc_info.value.status == 403

    def test_rfc1918_class_a_rejected(self) -> None:
        with patch("socket.getaddrinfo", _mock_getaddrinfo("10.0.0.1")):
            with pytest.raises(PrivateAddressError):
                resolve_and_check_ip("internal.corp")

    def test_rfc1918_class_b_rejected(self) -> None:
        with patch("socket.getaddrinfo", _mock_getaddrinfo("172.16.0.1")):
            with pytest.raises(PrivateAddressError):
                resolve_and_check_ip("internal.corp")

    def test_rfc1918_class_c_rejected(self) -> None:
        with patch("socket.getaddrinfo", _mock_getaddrinfo("192.168.1.1")):
            with pytest.raises(PrivateAddressError):
                resolve_and_check_ip("router.local")

    def test_aws_metadata_rejected(self) -> None:
        # 169.254.169.254 is the AWS/GCP/Azure instance metadata IP
        with patch("socket.getaddrinfo", _mock_getaddrinfo("169.254.169.254")):
            with pytest.raises(PrivateAddressError):
                resolve_and_check_ip("metadata.amazonaws.com")

    def test_link_local_rejected(self) -> None:
        with patch("socket.getaddrinfo", _mock_getaddrinfo("169.254.0.1")):
            with pytest.raises(PrivateAddressError):
                resolve_and_check_ip("link-local.example")

    def test_shared_address_space_rejected(self) -> None:
        # 100.64.0.0/10 — shared address space (RFC-6598 / Tailscale)
        with patch("socket.getaddrinfo", _mock_getaddrinfo("100.64.0.1")):
            with pytest.raises(PrivateAddressError):
                resolve_and_check_ip("tailscale.example")

    def test_ipv6_loopback_rejected(self) -> None:
        with patch(
            "socket.getaddrinfo",
            lambda host, port, *a, **kw: [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 80, 0, 0))
            ],
        ):
            with pytest.raises(PrivateAddressError):
                resolve_and_check_ip("ip6-localhost")

    def test_dns_failure_raises_fetch_error(self) -> None:
        with patch(
            "socket.getaddrinfo",
            side_effect=socket.gaierror("Name or service not known"),
        ):
            with pytest.raises(FetchGatewayError) as exc_info:
                resolve_and_check_ip("nonexistent.invalid")
        assert exc_info.value.code == "FETCH.DNS_RESOLUTION_FAILED"
