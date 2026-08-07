"""Task 2 contracts for URL validation and application-owned HTTP transport."""

from __future__ import annotations

import io
import socket
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


def addrinfo(*addresses: str) -> list[tuple]:
    results = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
        results.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
    return results


def public_resolver(hostname, port, **_kwargs):
    address = PUBLIC_V6 if ":" in hostname else PUBLIC_V4
    return addrinfo(address)


class FakeWireResponse:
    def __init__(self, status=200, headers=None, body=b"value\n1\n"):
        self.status = status
        self.reason = "test response"
        self.headers = headers or {}
        self._body = io.BytesIO(body)
        self.closed = False

    def read(self, amount=-1):
        return self._body.read(amount)

    def close(self):
        self.closed = True


class InterruptingWireResponse(FakeWireResponse):
    def read(self, amount=-1):
        raise KeyboardInterrupt()


class ScriptedConnector:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, target, method, headers, timeout):
        self.calls.append(
            {
                "target": target,
                "method": method,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected network connection")
        return self.responses.pop(0)


class FakeMonotonicClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class AdvancingWireResponse(FakeWireResponse):
    def __init__(self, clock, read_seconds, **kwargs):
        super().__init__(**kwargs)
        self._clock = clock
        self._read_seconds = read_seconds

    def read(self, amount=-1):
        self._clock.advance(self._read_seconds)
        return super().read(amount)


class AdvancingConnector(ScriptedConnector):
    def __init__(self, clock, connect_seconds, responses):
        super().__init__(responses)
        self._clock = clock
        self._connect_seconds = connect_seconds

    def __call__(self, target, method, headers, deadline):
        self._clock.advance(self._connect_seconds)
        return super().__call__(target, method, headers, deadline)


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_public_http_and_https_targets_are_admitted(scheme):
    from dataset_prober.tools.guards import validate_url

    target = validate_url(f"{scheme}://public.example/data.csv", resolver=public_resolver)

    assert target.scheme == scheme
    assert target.hostname == "public.example"
    assert [address.ip for address in target.addresses] == [PUBLIC_V4]


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example:80/data.csv",
        "https://public.example:443/data.csv",
    ],
)
def test_explicit_default_ports_are_admitted(url):
    from dataset_prober.tools.guards import validate_url

    target = validate_url(url, resolver=public_resolver)

    assert target.port in {80, 443}


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example:443/data.csv",
        "https://public.example:80/data.csv",
        "https://public.example:8443/data.csv",
        "http://public.example:8080/data.csv",
    ],
)
def test_non_policy_ports_are_rejected_before_dns(url):
    from dataset_prober.tools.guards import UnsafeURLError, validate_url

    resolver = Mock(side_effect=AssertionError("disallowed port reached DNS"))

    with pytest.raises(UnsafeURLError, match="port"):
        validate_url(url, resolver=resolver)

    resolver.assert_not_called()


@pytest.mark.parametrize(
    ("url", "resolved"),
    [
        (f"http://{PUBLIC_V4}/data.csv", PUBLIC_V4),
        (f"https://[{PUBLIC_V6}]/data.csv", PUBLIC_V6),
    ],
)
def test_literal_public_ipv4_and_ipv6_are_admitted(url, resolved):
    from dataset_prober.tools.guards import validate_url

    target = validate_url(url, resolver=lambda *_args, **_kwargs: addrinfo(resolved))

    assert target.addresses[0].ip == resolved


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "fc00::1",
        "169.254.169.254",
        "fe80::1",
        "224.0.0.1",
        "ff02::1",
        "0.0.0.0",
        "::",
        "240.0.0.1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
    ],
)
def test_every_non_public_address_class_is_rejected(address):
    from dataset_prober.tools.guards import UnsafeURLError, validate_url

    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_url(
            "https://public.example/data.csv",
            resolver=lambda *_args, **_kwargs: addrinfo(address),
        )


def test_localhost_alias_and_mixed_dns_answers_fail_closed():
    from dataset_prober.tools.guards import UnsafeURLError, validate_url

    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_url(
            "https://localhost.localdomain/data.csv",
            resolver=lambda *_args, **_kwargs: addrinfo("127.0.0.1"),
        )

    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_url(
            "https://mixed.example/data.csv",
            resolver=lambda *_args, **_kwargs: addrinfo(PUBLIC_V4, "10.0.0.1"),
        )


@pytest.mark.parametrize("answer", [[], None])
def test_empty_dns_answers_fail_closed(answer):
    from dataset_prober.tools.guards import UnsafeURLError, validate_url

    with pytest.raises(UnsafeURLError, match="no addresses"):
        validate_url(
            "https://empty.example/data.csv",
            resolver=lambda *_args, **_kwargs: answer,
        )


def test_dns_failure_fails_closed():
    from dataset_prober.tools.guards import UnsafeURLError, validate_url

    def fail(*_args, **_kwargs):
        raise socket.gaierror("not found")

    with pytest.raises(UnsafeURLError, match="DNS lookup failed"):
        validate_url("https://missing.example/data.csv", resolver=fail)


def test_malformed_dns_answer_fails_closed():
    from dataset_prober.tools.guards import UnsafeURLError, validate_url

    with pytest.raises(UnsafeURLError, match="malformed"):
        validate_url(
            "https://public.example/data.csv",
            resolver=lambda *_args, **_kwargs: [(socket.AF_INET,)],
        )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://public.example/data.csv",
        "gopher://public.example/",
        "data:text/plain,hello",
        "https:///missing-host.csv",
        "https://user:password@public.example/data.csv",
        " https://public.example/data.csv",
        "https://public.example/data.csv\nHost: internal",
        "https://public.example\\@127.0.0.1/data.csv",
        "https://public.example:invalid/data.csv",
        "https://public.example:0/data.csv",
        "https://public.example:/data.csv",
        "https://%31%32%37.0.0.1/data.csv",
        "https://public.example/%0d%0aHost:internal/data.csv",
        "https://public.example/%ZZ/data.csv",
    ],
)
def test_malformed_ambiguous_and_credentialed_urls_fail_before_dns(url):
    from dataset_prober.tools.guards import UnsafeURLError, validate_url

    resolver = Mock(side_effect=AssertionError("invalid URL reached DNS"))
    with pytest.raises(UnsafeURLError):
        validate_url(url, resolver=resolver)
    resolver.assert_not_called()


@pytest.mark.parametrize("hostname", ["2130706433", "0x7f000001", "0177.0.0.1"])
def test_address_forms_interpreted_by_the_connector_are_validated(hostname):
    from dataset_prober.tools.guards import UnsafeURLError, validate_url

    resolver = Mock(return_value=addrinfo("127.0.0.1"))
    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_url(f"http://{hostname}/data.csv", resolver=resolver)
    resolver.assert_called_once()


def test_client_connects_only_to_the_single_validated_dns_result():
    from dataset_prober.tools.guards import SafeHttpClient

    resolver = Mock(return_value=addrinfo(PUBLIC_V4))
    response = FakeWireResponse()
    connector = ScriptedConnector([response])
    client = SafeHttpClient(resolver=resolver, connector=connector)

    result = client.get("https://public.example/data.csv")

    assert result.content == b"value\n1\n"
    assert resolver.call_count == 1
    assert connector.calls[0]["target"].addresses[0].ip == PUBLIC_V4
    assert response.closed is True


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_default_connector_uses_exact_sockaddr_and_https_hostname(monkeypatch, scheme):
    from dataset_prober.tools.guards import _MonotonicDeadline, _PinnedConnector, validate_url

    port = 443 if scheme == "https" else 80
    sockaddr = (PUBLIC_V4, port)
    target = validate_url(
        f"{scheme}://public.example/data.csv",
        resolver=lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)
        ],
    )
    network_socket = Mock()
    socket_factory = Mock(return_value=network_socket)
    ssl_context = Mock()
    ssl_context.wrap_socket.return_value = network_socket
    connection = Mock()
    response = Mock(status=200, reason="OK")
    response.getheaders.return_value = [("Content-Type", "text/csv")]
    connection.getresponse.return_value = response
    http_connection = Mock(return_value=connection)
    monkeypatch.setattr("http.client.HTTPConnection", http_connection)
    connector = _PinnedConnector(
        socket_factory=socket_factory,
        ssl_context_factory=Mock(return_value=ssl_context),
    )

    wire_response = connector(target, "GET", {}, _MonotonicDeadline(3))
    wire_response.close()

    network_socket.connect.assert_called_once_with(sockaddr)
    assert http_connection.call_args.args == ("public.example", port)
    assert 0 < http_connection.call_args.kwargs["timeout"] <= 3
    if scheme == "https":
        ssl_context.wrap_socket.assert_called_once_with(
            network_socket,
            server_hostname="public.example",
        )
    else:
        ssl_context.wrap_socket.assert_not_called()
    response.close.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_unsafe_initial_url_is_rejected_before_transport_access():
    from dataset_prober.tools.guards import SafeHttpClient, UnsafeURLError

    connector = ScriptedConnector([])
    client = SafeHttpClient(
        resolver=lambda *_args, **_kwargs: addrinfo("127.0.0.1"), connector=connector
    )

    with pytest.raises(UnsafeURLError):
        client.get("http://localhost/data.csv")

    assert connector.calls == []


def test_forbidden_redirect_is_rejected_before_redirected_request():
    from dataset_prober.tools.guards import SafeHttpClient, UnsafeURLError

    def resolver(hostname, _port, **_kwargs):
        return addrinfo("127.0.0.1" if hostname == "internal.example" else PUBLIC_V4)

    connector = ScriptedConnector(
        [FakeWireResponse(status=302, headers={"Location": "https://internal.example/private"})]
    )
    client = SafeHttpClient(resolver=resolver, connector=connector)

    with pytest.raises(UnsafeURLError, match="non-public"):
        client.get("https://public.example/start")

    assert [call["target"].hostname for call in connector.calls] == ["public.example"]


def test_https_to_http_redirect_is_rejected_before_redirected_request():
    from dataset_prober.tools.guards import RedirectSafetyError, SafeHttpClient

    response = FakeWireResponse(
        status=302,
        headers={"Location": "http://other-public.example/data.csv"},
    )
    connector = ScriptedConnector([response])
    client = SafeHttpClient(resolver=public_resolver, connector=connector)

    with pytest.raises(RedirectSafetyError, match="HTTPS-to-HTTP"):
        client.get("https://public.example/start")

    assert response.closed is True
    assert len(connector.calls) == 1


def test_http_to_https_redirect_remains_allowed():
    from dataset_prober.tools.guards import SafeHttpClient

    connector = ScriptedConnector(
        [
            FakeWireResponse(
                status=302,
                headers={"Location": "https://public.example/data.csv"},
            ),
            FakeWireResponse(status=200, body=b"value\n1\n"),
        ]
    )
    client = SafeHttpClient(resolver=public_resolver, connector=connector)

    result = client.get("http://public.example/start")

    assert result.url == "https://public.example/data.csv"
    assert len(connector.calls) == 2


def test_relative_redirect_is_resolved_validated_and_followed():
    from dataset_prober.tools.guards import SafeHttpClient

    connector = ScriptedConnector(
        [
            FakeWireResponse(status=302, headers={"Location": "../files/data.csv"}),
            FakeWireResponse(status=200, body=b"a,b\n1,2\n"),
        ]
    )
    client = SafeHttpClient(resolver=public_resolver, connector=connector)

    result = client.get("https://public.example/catalog/start")

    assert result.url == "https://public.example/files/data.csv"
    assert result.content == b"a,b\n1,2\n"
    assert [call["target"].request_target for call in connector.calls] == [
        "/catalog/start",
        "/files/data.csv",
    ]


def test_cross_origin_redirect_does_not_forward_sensitive_headers():
    from dataset_prober.tools.guards import SafeHttpClient

    connector = ScriptedConnector(
        [
            FakeWireResponse(
                status=302,
                headers={"Location": "https://other-public.example/data.csv"},
            ),
            FakeWireResponse(status=200),
        ]
    )
    client = SafeHttpClient(resolver=public_resolver, connector=connector)

    client.get(
        "https://catalog.public.example/start",
        headers={
            "x-api-key": "catalog-secret",
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "X-Public-Header": "retained",
        },
    )

    assert connector.calls[0]["headers"]["x-api-key"] == "catalog-secret"
    redirected_headers = {
        key.lower(): value for key, value in connector.calls[1]["headers"].items()
    }
    assert "x-api-key" not in redirected_headers
    assert "authorization" not in redirected_headers
    assert "cookie" not in redirected_headers
    assert redirected_headers["x-public-header"] == "retained"


def test_same_origin_redirect_retains_caller_headers():
    from dataset_prober.tools.guards import SafeHttpClient

    connector = ScriptedConnector(
        [
            FakeWireResponse(status=302, headers={"Location": "/data.csv"}),
            FakeWireResponse(status=200),
        ]
    )
    client = SafeHttpClient(resolver=public_resolver, connector=connector)

    client.get(
        "https://catalog.public.example/start",
        headers={"x-api-key": "catalog-secret"},
    )

    assert connector.calls[1]["headers"]["x-api-key"] == "catalog-secret"


def test_redirect_loop_and_excessive_redirects_fail_closed():
    from dataset_prober.tools.guards import RedirectSafetyError, SafeHttpClient

    loop_connector = ScriptedConnector(
        [FakeWireResponse(status=302, headers={"Location": "/loop"})]
    )
    loop_client = SafeHttpClient(resolver=public_resolver, connector=loop_connector)
    with pytest.raises(RedirectSafetyError, match="loop"):
        loop_client.get("https://public.example/loop")

    limit_connector = ScriptedConnector(
        [
            FakeWireResponse(status=302, headers={"Location": "/one"}),
            FakeWireResponse(status=302, headers={"Location": "/two"}),
        ]
    )
    limit_client = SafeHttpClient(
        resolver=public_resolver, connector=limit_connector, max_redirects=1
    )
    with pytest.raises(RedirectSafetyError, match="limit"):
        limit_client.get("https://public.example/start")
    assert len(limit_connector.calls) == 2


@pytest.mark.parametrize(
    "location",
    [
        "",
        " https://public.example/data.csv",
        "https://public.example/%0d%0aHost:internal",
    ],
)
def test_missing_or_malformed_redirect_location_fails_closed(location):
    from dataset_prober.tools.guards import RedirectSafetyError, SafeHttpClient, UnsafeURLError

    connector = ScriptedConnector(
        [FakeWireResponse(status=302, headers={"Location": location} if location else {})]
    )
    client = SafeHttpClient(resolver=public_resolver, connector=connector)

    with pytest.raises((RedirectSafetyError, UnsafeURLError)):
        client.get("https://public.example/start")
    assert len(connector.calls) == 1


def test_unhandled_three_hundred_status_fails_closed():
    from dataset_prober.tools.guards import RedirectSafetyError, SafeHttpClient

    connector = ScriptedConnector([FakeWireResponse(status=300)])
    client = SafeHttpClient(resolver=public_resolver, connector=connector)

    with pytest.raises(RedirectSafetyError, match="unsupported redirect"):
        client.get("https://public.example/start")


def test_environment_proxy_variables_do_not_change_the_pinned_connector(monkeypatch):
    from dataset_prober.tools.guards import SafeHttpClient

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    connector = ScriptedConnector([FakeWireResponse()])
    client = SafeHttpClient(resolver=public_resolver, connector=connector)

    client.get("https://public.example/data.csv")

    assert len(connector.calls) == 1
    assert connector.calls[0]["target"].addresses[0].ip == PUBLIC_V4


def test_guarded_download_is_temporary_and_bound_to_source_url(tmp_path):
    from dataset_prober.tools.guards import SafeHttpClient

    connector = ScriptedConnector(
        [FakeWireResponse(headers={"Content-Type": "text/csv"}, body=b"a,b\n1,2\n")]
    )
    client = SafeHttpClient(
        resolver=public_resolver,
        connector=connector,
        temporary_directory=tmp_path,
    )

    with client.download("https://public.example/data.csv") as fetched:
        local_path = Path(fetched.path)
        assert fetched.source_url == "https://public.example/data.csv"
        assert fetched.final_url == "https://public.example/data.csv"
        assert local_path.read_bytes() == b"a,b\n1,2\n"

    assert not local_path.exists()


def test_html_resource_is_rejected_without_creating_a_temporary_file(tmp_path):
    from dataset_prober.tools.guards import SafeHttpClient, UnsafeResourceError

    connector = ScriptedConnector(
        [FakeWireResponse(headers={"Content-Type": "text/html"}, body=b"<html></html>")]
    )
    client = SafeHttpClient(
        resolver=public_resolver,
        connector=connector,
        temporary_directory=tmp_path,
    )

    with pytest.raises(UnsafeResourceError, match="HTML"):
        with client.download("https://public.example/data.csv"):
            pass

    assert list(tmp_path.iterdir()) == []


def test_temporary_download_and_response_close_on_keyboard_interrupt(tmp_path):
    from dataset_prober.tools.guards import SafeHttpClient

    response = InterruptingWireResponse(headers={"Content-Type": "text/csv"})
    client = SafeHttpClient(
        resolver=public_resolver,
        connector=ScriptedConnector([response]),
        temporary_directory=tmp_path,
    )

    with pytest.raises(KeyboardInterrupt):
        with client.download("https://public.example/data.csv"):
            pass

    assert response.closed is True
    assert list(tmp_path.iterdir()) == []


def test_get_rejects_declared_oversized_body_before_reading_it():
    from dataset_prober.tools.guards import SafeHttpClient, UnsafeResourceError

    response = FakeWireResponse(
        headers={"Content-Type": "application/json", "Content-Length": "9"},
        body=b"123456789",
    )
    response.read = Mock(side_effect=AssertionError("oversized body was read"))
    client = SafeHttpClient(
        resolver=public_resolver,
        connector=ScriptedConnector([response]),
        max_response_bytes=8,
    )

    with pytest.raises(UnsafeResourceError, match="size limit"):
        client.get("https://public.example/catalog?token=secret#private")

    response.read.assert_not_called()
    assert response.closed is True


def test_get_rejects_chunked_body_that_exceeds_limit_and_redacts_url():
    from dataset_prober.tools.guards import SafeHttpClient, UnsafeResourceError

    response = FakeWireResponse(body=b"123456789")
    client = SafeHttpClient(
        resolver=public_resolver,
        connector=ScriptedConnector([response]),
        max_response_bytes=8,
    )

    with pytest.raises(UnsafeResourceError) as caught:
        client.get("https://public.example/catalog?token=secret#private")

    message = str(caught.value)
    assert "secret" not in message
    assert "private" not in message
    assert "SHA-256:" in message
    assert response.closed is True


def test_download_rejects_stream_over_limit_and_removes_partial_file(tmp_path):
    from dataset_prober.tools.guards import SafeHttpClient, UnsafeResourceError

    response = FakeWireResponse(headers={"Content-Type": "text/csv"}, body=b"123456789")
    client = SafeHttpClient(
        resolver=public_resolver,
        connector=ScriptedConnector([response]),
        max_download_bytes=8,
        temporary_directory=tmp_path,
    )

    with pytest.raises(UnsafeResourceError, match="size limit"):
        with client.download("https://public.example/data.csv?signature=secret"):
            pass

    assert response.closed is True
    assert list(tmp_path.iterdir()) == []


def test_response_exactly_at_configured_limit_is_allowed(tmp_path):
    from dataset_prober.tools.guards import SafeHttpClient

    body = b"12345678"
    get_client = SafeHttpClient(
        resolver=public_resolver,
        connector=ScriptedConnector([FakeWireResponse(body=body)]),
        max_response_bytes=len(body),
    )
    assert get_client.get("https://public.example/catalog").content == body

    download_client = SafeHttpClient(
        resolver=public_resolver,
        connector=ScriptedConnector(
            [FakeWireResponse(headers={"Content-Type": "text/csv"}, body=body)]
        ),
        max_download_bytes=len(body),
        temporary_directory=tmp_path,
    )
    with download_client.download("https://public.example/data.csv") as fetched:
        assert Path(fetched.path).read_bytes() == body


@pytest.mark.parametrize("content_length", ["not-a-number", "-1", "1, 2"])
def test_malformed_content_length_fails_closed(content_length):
    from dataset_prober.tools.guards import SafeHttpClient, UnsafeResourceError

    response = FakeWireResponse(headers={"Content-Length": content_length})
    client = SafeHttpClient(
        resolver=public_resolver,
        connector=ScriptedConnector([response]),
    )

    with pytest.raises(UnsafeResourceError, match="Content-Length"):
        client.get("https://public.example/catalog")

    assert response.closed is True


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_invalid_timeout_is_rejected_before_dns_or_transport(timeout):
    from dataset_prober.tools.guards import SafeHttpClient

    resolver = Mock(side_effect=AssertionError("invalid timeout reached DNS"))
    connector = ScriptedConnector([])
    client = SafeHttpClient(resolver=resolver, connector=connector)

    with pytest.raises(ValueError, match="timeout"):
        client.get("https://public.example/catalog", timeout=timeout)

    resolver.assert_not_called()
    assert connector.calls == []


def test_timeout_is_one_monotonic_deadline_across_dns_redirects_connect_and_body(monkeypatch):
    from dataset_prober.tools.guards import SafeHttpClient, SafeTransportError

    clock = FakeMonotonicClock()
    monkeypatch.setattr("dataset_prober.tools.guards.time.monotonic", clock)

    def resolver(*_args, **_kwargs):
        clock.advance(0.2)
        return addrinfo(PUBLIC_V4)

    first_response = FakeWireResponse(status=302, headers={"Location": "/data.csv"})
    final_response = AdvancingWireResponse(
        clock,
        0.3,
        status=200,
        body=b"value\n1\n",
    )
    connector = AdvancingConnector(clock, 0.2, [first_response, final_response])
    client = SafeHttpClient(resolver=resolver, connector=connector)

    with pytest.raises(SafeTransportError, match="deadline"):
        client.get("https://public.example/start", timeout=1.0)

    assert first_response.closed is True
    assert final_response.closed is True
    assert len(connector.calls) == 2
    assert connector.calls[0]["timeout"] is connector.calls[1]["timeout"]


def test_dns_cannot_block_past_the_end_to_end_deadline():
    from dataset_prober.tools.guards import SafeHttpClient, SafeTransportError

    release_resolver = threading.Event()

    def blocking_resolver(*_args, **_kwargs):
        release_resolver.wait(2)
        return addrinfo(PUBLIC_V4)

    connector = ScriptedConnector([])
    client = SafeHttpClient(resolver=blocking_resolver, connector=connector)
    started = time.monotonic()
    try:
        with pytest.raises(SafeTransportError, match="deadline"):
            client.get("https://public.example/data.csv", timeout=0.05)
    finally:
        release_resolver.set()

    assert time.monotonic() - started < 0.5
    assert connector.calls == []
