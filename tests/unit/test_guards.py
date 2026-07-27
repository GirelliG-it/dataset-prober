"""
tests/unit/test_guards.py

Tests for fetch-time URL hardening (`tools.guards.is_safe_url`).

DNS is mocked throughout — no test here touches the network, and no test
depends on how any real hostname happens to resolve today.

Key behaviours under test:
1. Disallowed schemes are rejected without a DNS lookup
2. Non-public addresses are rejected (loopback, private, link-local,
   multicast, reserved, unspecified)
3. IPv6 zone indices are handled rather than crashing
4. A host resolving to several addresses is rejected if ANY is non-public
5. Resolution is cached per hostname
"""

import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


def make_addrinfo(*ips: str) -> list:
    """
    Build a getaddrinfo-shaped return value for the given IP strings.

    Real getaddrinfo returns 5-tuples whose last element is the sockaddr;
    is_safe_url reads sockaddr[0]. Only that position needs to be faithful.
    """
    return [(socket.AF_UNSPEC, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in ips]


@pytest.fixture(autouse=True)
def clear_resolution_cache():
    """
    Clear the lru_cache on _resolve between tests.

    Without this, a hostname mocked as public in one test stays cached and the
    next test's mock is never consulted — tests would pass or fail depending on
    execution order. Autouse so it can never be forgotten.
    """
    from tools.guards import _resolve

    _resolve.cache_clear()
    yield
    _resolve.cache_clear()


class TestSchemeRejection:
    """Schemes outside http/https are rejected before any network call."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/data.csv",
            "data:text/csv;base64,YQpi",
            "javascript:alert(1)",
            "gopher://example.com/",
        ],
    )
    def test_disallowed_scheme_rejected(self, url):
        from tools.guards import is_safe_url

        safe, reason = is_safe_url(url)
        assert safe is False
        assert "scheme" in reason

    def test_scheme_checked_before_dns(self):
        """A bad scheme must not cost a DNS lookup."""
        from tools.guards import is_safe_url

        with patch("socket.getaddrinfo") as mock_resolve:
            safe, _ = is_safe_url("file:///etc/passwd")
        assert safe is False
        mock_resolve.assert_not_called()

    def test_missing_hostname_rejected(self):
        from tools.guards import is_safe_url

        safe, reason = is_safe_url("http:///just/a/path")
        assert safe is False
        assert "hostname" in reason


class TestNonPublicAddresses:
    """Hosts resolving to non-routable address space are rejected."""

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # loopback
            "::1",  # loopback, v6
            "10.0.0.5",  # private
            "192.168.1.1",  # private
            "172.16.4.2",  # private
            "169.254.169.254",  # link-local — cloud metadata endpoint
            "224.0.0.1",  # multicast
            "240.0.0.1",  # reserved
            "0.0.0.0",  # unspecified
            "fd00::1",  # unique local, v6
        ],
    )
    def test_non_public_address_rejected(self, ip):
        from tools.guards import is_safe_url

        with patch("socket.getaddrinfo", return_value=make_addrinfo(ip)):
            safe, reason = is_safe_url("https://data.example.com/x.csv")
        assert safe is False
        assert "non-public" in reason

    def test_public_address_accepted(self):
        from tools.guards import is_safe_url

        with patch("socket.getaddrinfo", return_value=make_addrinfo("93.184.216.34")):
            safe, reason = is_safe_url("https://data.example.com/x.csv")
        assert safe is True
        assert reason == ""

    def test_public_ipv6_accepted(self):
        from tools.guards import is_safe_url

        with patch(
            "socket.getaddrinfo", return_value=make_addrinfo("2606:2800:220:1:248:1893:25c8:1946")
        ):
            safe, _ = is_safe_url("https://data.example.com/x.csv")
        assert safe is True

    def test_any_non_public_address_rejects_whole_host(self):
        """
        A host answering with one public and one private address is rejected.
        Accepting it would mean the connection's choice of address decides
        whether the guard held.
        """
        from tools.guards import is_safe_url

        with patch("socket.getaddrinfo", return_value=make_addrinfo("93.184.216.34", "10.0.0.5")):
            safe, reason = is_safe_url("https://data.example.com/x.csv")
        assert safe is False
        assert "10.0.0.5" in reason


class TestAddressParsing:
    """Address strings that trip ipaddress.ip_address() must not crash."""

    def test_ipv6_zone_index_stripped(self):
        """
        fe80::1%eth0 is what getaddrinfo can hand back for a v6 link-local
        address. ip_address() raises on the zone suffix, so it is stripped
        before parsing — this is the exact case the guard exists to block.
        """
        from tools.guards import is_safe_url

        with patch("socket.getaddrinfo", return_value=make_addrinfo("fe80::1%eth0")):
            safe, reason = is_safe_url("https://data.example.com/x.csv")
        assert safe is False
        assert "non-public" in reason

    def test_unparseable_address_fails_closed(self):
        from tools.guards import is_safe_url

        with patch("socket.getaddrinfo", return_value=make_addrinfo("not-an-ip")):
            safe, reason = is_safe_url("https://data.example.com/x.csv")
        assert safe is False
        assert "unparseable" in reason

    def test_empty_resolution_rejected(self):
        from tools.guards import is_safe_url

        with patch("socket.getaddrinfo", return_value=[]):
            safe, reason = is_safe_url("https://data.example.com/x.csv")
        assert safe is False
        assert "no addresses" in reason


class TestDnsFailure:
    """A hostname that will not resolve is rejected, not raised on."""

    def test_gaierror_rejected(self):
        from tools.guards import is_safe_url

        with patch("socket.getaddrinfo", side_effect=socket.gaierror("Name or service not known")):
            safe, reason = is_safe_url("https://no-such-host.example/x.csv")
        assert safe is False
        assert "DNS lookup failed" in reason

    def test_failed_lookup_is_not_cached(self):
        """
        lru_cache stores returns, not exceptions — a transient DNS failure
        must be retried on the next call rather than remembered.
        """
        from tools.guards import is_safe_url

        with patch("socket.getaddrinfo", side_effect=socket.gaierror("transient")):
            assert is_safe_url("https://flaky.example.com/x.csv")[0] is False

        with patch("socket.getaddrinfo", return_value=make_addrinfo("93.184.216.34")):
            assert is_safe_url("https://flaky.example.com/x.csv")[0] is True


class TestUrlParsing:
    """The hostname the guard checks is the host that will be connected to."""

    def test_userinfo_prefix_does_not_disguise_host(self):
        """
        http://trusted.example.com@127.0.0.1/ connects to 127.0.0.1. The text
        before the @ is credentials, not the host — urlparse().hostname gets
        this right, and the guard must inherit that.
        """
        from tools.guards import is_safe_url

        with patch("socket.getaddrinfo", return_value=make_addrinfo("127.0.0.1")) as mock_resolve:
            safe, _ = is_safe_url("http://trusted.example.com@127.0.0.1/data.csv")
        assert safe is False
        assert mock_resolve.call_args[0][0] == "127.0.0.1"

    def test_ip_literal_host_is_checked(self):
        from tools.guards import is_safe_url

        with patch("socket.getaddrinfo", return_value=make_addrinfo("192.168.0.10")):
            safe, _ = is_safe_url("http://192.168.0.10:8080/data.csv")
        assert safe is False

    def test_port_is_not_part_of_hostname(self):
        from tools.guards import is_safe_url

        with patch(
            "socket.getaddrinfo", return_value=make_addrinfo("93.184.216.34")
        ) as mock_resolve:
            is_safe_url("https://data.example.com:8443/x.csv")
        assert mock_resolve.call_args[0][0] == "data.example.com"


class TestResolutionCaching:
    """One lookup per hostname per run, not one per link."""

    def test_same_host_resolved_once(self):
        from tools.guards import is_safe_url

        with patch(
            "socket.getaddrinfo", return_value=make_addrinfo("93.184.216.34")
        ) as mock_resolve:
            is_safe_url("https://data.example.com/a.csv")
            is_safe_url("https://data.example.com/b.csv")
            is_safe_url("https://data.example.com/c.csv")
        assert mock_resolve.call_count == 1

    def test_different_hosts_resolved_separately(self):
        from tools.guards import is_safe_url

        with patch(
            "socket.getaddrinfo", return_value=make_addrinfo("93.184.216.34")
        ) as mock_resolve:
            is_safe_url("https://a.example.com/x.csv")
            is_safe_url("https://b.example.com/x.csv")
        assert mock_resolve.call_count == 2
