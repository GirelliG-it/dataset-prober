"""Application-owned URL validation, redirect handling, and pinned HTTP transport."""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import queue
import re
import socket
import ssl
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit

from dataset_prober.loading_policy import safe_url_identity

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = {"http": frozenset({80}), "https": frozenset({443})}
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DEFAULT_MAX_REDIRECTS = 5
# The live CBS catalogue is currently about 18 MiB. A 32 MiB ceiling retains
# required headroom while keeping every in-memory catalogue/body allocation bounded.
MAX_HTTP_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_DATASET_DOWNLOAD_BYTES = 512 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SENSITIVE_REDIRECT_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization", "x-api-key"}
)
_DNS_WORKER_SLOTS = threading.BoundedSemaphore(8)


class UnsafeURLError(ValueError):
    """A URL cannot safely be used as an application-owned network destination."""


class RedirectSafetyError(UnsafeURLError):
    """A redirect chain is missing, malformed, cyclic, or too long."""


class UnsafeResourceError(ValueError):
    """A safely retrieved resource is not suitable for the intended data path."""


class SafeTransportError(RuntimeError):
    """A validated destination could not be reached or returned an HTTP error."""


class _MonotonicDeadline:
    """One non-resetting deadline shared by every phase of one HTTP request."""

    def __init__(self, timeout: float) -> None:
        self.__expires_at = time.monotonic() + timeout

    def remaining(self, url: str) -> float:
        remaining = self.__expires_at - time.monotonic()
        if remaining <= 0:
            raise SafeTransportError(
                f"end-to-end HTTP deadline exceeded for {safe_url_identity(url)}"
            )
        return remaining

    def check(self, url: str) -> None:
        self.remaining(url)


@dataclass(frozen=True)
class ResolvedAddress:
    """One exact socket address returned by the validation-time DNS lookup."""

    family: int
    socket_type: int
    protocol: int
    sockaddr: tuple
    ip: str


@dataclass(frozen=True)
class ResolvedURL:
    """Normalized request components plus the only socket addresses that may be used."""

    url: str
    scheme: str
    hostname: str
    port: int
    host_header: str
    request_target: str
    addresses: tuple[ResolvedAddress, ...]


@dataclass(frozen=True)
class SafeHttpResult:
    """Completed non-streaming response from the guarded transport."""

    url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)


@dataclass(frozen=True)
class FetchedResource:
    """Temporary local copy of one exact safely retrieved source URL."""

    source_url: str
    final_url: str
    path: str
    headers: Mapping[str, str]


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _canonical_hostname(hostname: str) -> str:
    if "%" in hostname:
        raise UnsafeURLError("URL hostname contains encoded or scoped data")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            canonical = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UnsafeURLError("URL hostname is malformed") from exc
        if not canonical:
            raise UnsafeURLError("URL has no hostname")
        return canonical
    return hostname.lower()


def _public_ip(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    without_zone = address.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(without_zone)
    except ValueError as exc:
        raise UnsafeURLError("DNS returned an unparseable address") from exc

    if (
        not parsed.is_global
        or parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    ):
        raise UnsafeURLError(f"hostname resolves to non-public address ({parsed})")
    return parsed


def _resolve_with_deadline(
    resolver: Callable[..., list[tuple]],
    hostname: str,
    port: int,
    deadline: _MonotonicDeadline,
    url: str,
) -> list[tuple]:
    """Run otherwise-unbounded resolver code behind the request deadline."""
    if not _DNS_WORKER_SLOTS.acquire(timeout=deadline.remaining(url)):
        raise SafeTransportError(f"end-to-end HTTP deadline exceeded for {safe_url_identity(url)}")

    outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            result = resolver(
                hostname,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except BaseException as exc:
            outcome.put((False, exc))
        else:
            outcome.put((True, result))
        finally:
            _DNS_WORKER_SLOTS.release()

    threading.Thread(
        target=resolve,
        name="dataset-prober-dns",
        daemon=True,
    ).start()
    try:
        succeeded, result = outcome.get(timeout=deadline.remaining(url))
    except queue.Empty as exc:
        raise SafeTransportError(
            f"end-to-end HTTP deadline exceeded for {safe_url_identity(url)}"
        ) from exc
    deadline.check(url)
    if not succeeded:
        raise result
    return result


def validate_url(
    url: str,
    *,
    resolver: Callable[..., list[tuple]] | None = None,
    deadline: _MonotonicDeadline | None = None,
) -> ResolvedURL:
    """Normalize, resolve, and validate one URL without issuing a request."""
    if not isinstance(url, str) or not url or url != url.strip():
        raise UnsafeURLError("URL is empty or has surrounding whitespace")
    if _contains_control(url) or _contains_control(unquote(url)):
        raise UnsafeURLError("URL contains control characters")
    if "\\" in url or "\\" in unquote(url):
        raise UnsafeURLError("URL contains an ambiguous backslash")
    if _BAD_PERCENT.search(url):
        raise UnsafeURLError("URL contains malformed percent encoding")

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURLError("URL cannot be parsed unambiguously") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme '{scheme}' not allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("embedded URL credentials are not allowed")
    if not parsed.hostname:
        raise UnsafeURLError("URL has no hostname")
    if parsed.netloc.endswith(":"):
        raise UnsafeURLError("URL port is malformed")
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("URL port is malformed") from exc

    hostname = _canonical_hostname(parsed.hostname)
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise UnsafeURLError("URL port is outside the supported range")
    if port not in ALLOWED_PORTS[scheme]:
        raise UnsafeURLError(f"port {port} is not allowed for scheme '{scheme}'")

    path = parsed.path or "/"
    request_target = path + (f"?{parsed.query}" if parsed.query else "")
    bracketed_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    host_header = bracketed_host if port == default_port else f"{bracketed_host}:{port}"
    normalized_url = urlunsplit((scheme, host_header, path, parsed.query, ""))

    active_resolver = resolver or socket.getaddrinfo
    try:
        if deadline is None:
            answers = active_resolver(
                hostname,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        else:
            answers = _resolve_with_deadline(
                active_resolver,
                hostname,
                port,
                deadline,
                normalized_url,
            )
    except socket.gaierror as exc:
        raise UnsafeURLError("DNS lookup failed") from exc
    except OSError as exc:
        raise UnsafeURLError("DNS lookup failed") from exc
    if not answers:
        raise UnsafeURLError("hostname resolved to no addresses")

    addresses = []
    seen = set()
    for answer in answers:
        try:
            family, socket_type, protocol, _canonical_name, sockaddr = answer
            socket_address = tuple(sockaddr)
        except (TypeError, ValueError) as exc:
            raise UnsafeURLError("DNS returned a malformed socket address") from exc
        if not socket_address:
            raise UnsafeURLError("DNS returned a malformed socket address")
        parsed_address = _public_ip(str(socket_address[0]))
        key = (family, socket_type, protocol, socket_address)
        if key in seen:
            continue
        seen.add(key)
        addresses.append(
            ResolvedAddress(
                family=family,
                socket_type=socket_type or socket.SOCK_STREAM,
                protocol=protocol,
                sockaddr=socket_address,
                ip=str(parsed_address),
            )
        )
    if not addresses:
        raise UnsafeURLError("hostname resolved to no addresses")

    return ResolvedURL(
        url=normalized_url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        host_header=host_header,
        request_target=request_target,
        addresses=tuple(addresses),
    )


def is_safe_url(url: str) -> tuple[bool, str]:
    """Diagnostic compatibility wrapper; enabled fetches use :class:`SafeHttpClient`."""
    try:
        validate_url(url)
    except UnsafeURLError as exc:
        return False, str(exc)
    return True, ""


class _PinnedWireResponse:
    """Own an http.client response and its exact pinned connection."""

    def __init__(
        self,
        response,
        connection,
        deadline: _MonotonicDeadline,
        url: str,
    ) -> None:
        self.status = response.status
        self.reason = response.reason
        self.headers = {key: value for key, value in response.getheaders()}
        self.__response = response
        self.__connection = connection
        self.__deadline = deadline
        self.__url = url

    def read(self, amount: int = -1) -> bytes:
        remaining = self.__deadline.remaining(self.__url)
        network_socket = getattr(self.__connection, "sock", None)
        if network_socket is not None:
            network_socket.settimeout(remaining)
        content = self.__response.read(amount)
        self.__deadline.check(self.__url)
        return content

    def close(self) -> None:
        try:
            self.__response.close()
        finally:
            self.__connection.close()


class _PinnedConnector:
    """Connect directly to a validated sockaddr while retaining HTTP host and TLS SNI."""

    def __init__(
        self,
        *,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        ssl_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
    ) -> None:
        self.__socket_factory = socket_factory
        self.__ssl_context_factory = ssl_context_factory

    def __call__(
        self,
        target: ResolvedURL,
        method: str,
        headers: Mapping[str, str],
        deadline: _MonotonicDeadline,
    ):
        last_error = None
        for address in target.addresses:
            network_socket = None
            try:
                remaining = deadline.remaining(target.url)
                network_socket = self.__socket_factory(
                    address.family,
                    address.socket_type,
                    address.protocol,
                )
                network_socket.settimeout(remaining)
                network_socket.connect(address.sockaddr)
                if target.scheme == "https":
                    context = self.__ssl_context_factory()
                    network_socket.settimeout(deadline.remaining(target.url))
                    network_socket = context.wrap_socket(
                        network_socket,
                        server_hostname=target.hostname,
                    )

                remaining = deadline.remaining(target.url)
                network_socket.settimeout(remaining)
                connection = http.client.HTTPConnection(
                    target.hostname,
                    target.port,
                    timeout=remaining,
                )
                connection.sock = network_socket
                connection.putrequest(
                    method,
                    target.request_target,
                    skip_host=True,
                    skip_accept_encoding=True,
                )
                connection.putheader("Host", target.host_header)
                connection.putheader("Accept-Encoding", "identity")
                connection.putheader("Connection", "close")
                for name, value in headers.items():
                    if _contains_control(str(name)) or _contains_control(str(value)):
                        raise SafeTransportError("HTTP header contains control characters")
                    connection.putheader(str(name), str(value))
                network_socket.settimeout(deadline.remaining(target.url))
                connection.endheaders()
                network_socket.settimeout(deadline.remaining(target.url))
                response = connection.getresponse()
                deadline.check(target.url)
                return _PinnedWireResponse(response, connection, deadline, target.url)
            except BaseException as exc:
                if network_socket is not None:
                    network_socket.close()
                if not isinstance(exc, Exception):
                    raise
                if isinstance(exc, SafeTransportError):
                    raise
                last_error = exc

        identity = safe_url_identity(target.url)
        error_type = type(last_error).__name__ if last_error is not None else "unknown error"
        raise SafeTransportError(f"connection failed for {identity} ({error_type})") from last_error


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _is_html(headers: Mapping[str, str]) -> bool:
    content_type = (_header(headers, "Content-Type") or "").split(";", 1)[0].strip().lower()
    return content_type in {"text/html", "application/xhtml+xml"}


def _declared_content_length(headers: Mapping[str, str]) -> int | None:
    raw_length = _header(headers, "Content-Length")
    if raw_length is None:
        return None
    if not raw_length.isascii() or not raw_length.isdigit():
        raise UnsafeResourceError("HTTP Content-Length is malformed")
    return int(raw_length)


def _validate_timeout(timeout: float) -> float:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("HTTP timeout must be a positive finite number")
    return float(timeout)


def _validate_size_limit(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _reject_declared_oversize(
    target: ResolvedURL,
    headers: Mapping[str, str],
    maximum_bytes: int,
    resource_kind: str,
) -> None:
    declared = _declared_content_length(headers)
    if declared is not None and declared > maximum_bytes:
        raise UnsafeResourceError(
            f"{resource_kind} exceeds the configured size limit for {safe_url_identity(target.url)}"
        )


def _redirect_origin(url: str) -> tuple[str, str, int] | None:
    """Parse an origin for header-forwarding decisions without issuing DNS."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in ALLOWED_SCHEMES or not parsed.hostname:
            return None
        hostname = _canonical_hostname(parsed.hostname)
        port = parsed.port
    except (UnsafeURLError, ValueError):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port


class SafeHttpClient:
    """Resolve once, validate all answers, connect to one validated address, and own redirects."""

    def __init__(
        self,
        *,
        resolver: Callable[..., list[tuple]] | None = None,
        connector: Callable[..., Any] | None = None,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        temporary_directory: str | Path | None = None,
        max_response_bytes: int = MAX_HTTP_RESPONSE_BYTES,
        max_download_bytes: int = MAX_DATASET_DOWNLOAD_BYTES,
    ) -> None:
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        self.__resolver = resolver
        self.__connector = connector or _PinnedConnector()
        self.__max_redirects = max_redirects
        self.__temporary_directory = temporary_directory
        self.__max_response_bytes = _validate_size_limit(max_response_bytes, "max_response_bytes")
        self.__max_download_bytes = _validate_size_limit(max_download_bytes, "max_download_bytes")

    @staticmethod
    def _with_params(url: str, params: Mapping[str, Any] | None) -> str:
        if not params:
            return url
        parsed = urlsplit(url)
        encoded = urlencode(params, doseq=True)
        query = "&".join(part for part in (parsed.query, encoded) if part)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))

    @contextmanager
    def _open(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        timeout: float,
    ) -> Iterator[tuple[ResolvedURL, Any, _MonotonicDeadline]]:
        timeout = _validate_timeout(timeout)
        deadline = _MonotonicDeadline(timeout)
        current = url
        request_headers = dict(headers or {})
        seen = set()
        redirect_count = 0

        while True:
            target = validate_url(
                current,
                resolver=self.__resolver,
                deadline=deadline,
            )
            if target.url in seen:
                raise RedirectSafetyError("redirect loop detected")
            seen.add(target.url)

            response = self.__connector(target, method, request_headers, deadline)
            deadline.check(target.url)
            if response.status in REDIRECT_STATUSES:
                location = _header(response.headers, "Location")
                response.close()
                if not location:
                    raise RedirectSafetyError("redirect response has no Location header")
                if (
                    location != location.strip()
                    or _contains_control(location)
                    or _contains_control(unquote(location))
                    or "\\" in location
                    or _BAD_PERCENT.search(location)
                ):
                    raise RedirectSafetyError("redirect Location is malformed or ambiguous")
                if redirect_count >= self.__max_redirects:
                    raise RedirectSafetyError("redirect limit exceeded")
                next_url = urljoin(target.url, location)
                next_origin = _redirect_origin(next_url)
                if target.scheme == "https" and next_origin and next_origin[0] == "http":
                    raise RedirectSafetyError("HTTPS-to-HTTP redirect is not allowed")
                if next_origin != (
                    target.scheme,
                    target.hostname,
                    target.port,
                ):
                    request_headers = {
                        name: value
                        for name, value in request_headers.items()
                        if name.lower() not in _SENSITIVE_REDIRECT_HEADERS
                    }
                current = next_url
                redirect_count += 1
                continue

            if response.status >= 400:
                response.close()
                raise SafeTransportError(
                    f"HTTP {response.status} from {safe_url_identity(target.url)}"
                )
            if 300 <= response.status < 400:
                response.close()
                raise RedirectSafetyError("unsupported redirect status")
            try:
                yield target, response, deadline
            finally:
                response.close()
            return

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30,
    ) -> SafeHttpResult:
        requested_url = self._with_params(url, params)
        with self._open("GET", requested_url, headers=headers, timeout=timeout) as (
            target,
            response,
            deadline,
        ):
            _reject_declared_oversize(
                target,
                response.headers,
                self.__max_response_bytes,
                "HTTP response",
            )
            content = response.read(self.__max_response_bytes + 1)
            deadline.check(target.url)
            if len(content) > self.__max_response_bytes:
                raise UnsafeResourceError(
                    "HTTP response exceeds the configured size limit "
                    f"for {safe_url_identity(target.url)}"
                )
            return SafeHttpResult(
                url=target.url,
                status_code=response.status,
                headers=dict(response.headers),
                content=content,
            )

    def head(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30,
    ) -> SafeHttpResult:
        with self._open("HEAD", url, headers=headers, timeout=timeout) as (
            target,
            response,
            deadline,
        ):
            deadline.check(target.url)
            return SafeHttpResult(
                url=target.url,
                status_code=response.status,
                headers=dict(response.headers),
                content=b"",
            )

    @contextmanager
    def download(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30,
        reject_html: bool = True,
    ) -> Iterator[FetchedResource]:
        path = None
        with self._open("GET", url, headers=headers, timeout=timeout) as (
            target,
            response,
            deadline,
        ):
            _reject_declared_oversize(
                target,
                response.headers,
                self.__max_download_bytes,
                "Dataset download",
            )
            if reject_html and _is_html(response.headers):
                raise UnsafeResourceError("safely retrieved resource declares HTML content")
            suffix = Path(urlsplit(target.url).path).suffix[:16] or ".download"
            temporary = tempfile.NamedTemporaryFile(
                mode="wb",
                prefix="dataset-prober-",
                suffix=suffix,
                dir=self.__temporary_directory,
                delete=False,
            )
            path = Path(temporary.name)
            try:
                downloaded_bytes = 0
                with temporary:
                    while True:
                        deadline.check(target.url)
                        chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                        deadline.check(target.url)
                        if not chunk:
                            break
                        downloaded_bytes += len(chunk)
                        if downloaded_bytes > self.__max_download_bytes:
                            raise UnsafeResourceError(
                                "Dataset download exceeds the configured size limit "
                                f"for {safe_url_identity(target.url)}"
                            )
                        temporary.write(chunk)
                yield FetchedResource(
                    source_url=url,
                    final_url=target.url,
                    path=str(path),
                    headers=dict(response.headers),
                )
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)


def safe_http_get(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30,
) -> SafeHttpResult:
    """Fetch one non-streaming response through the authoritative transport."""
    return SafeHttpClient().get(url, params=params, headers=headers, timeout=timeout)


def safe_http_head(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30,
) -> SafeHttpResult:
    """Fetch response headers through the authoritative transport."""
    return SafeHttpClient().head(url, headers=headers, timeout=timeout)


@contextmanager
def safe_download(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30,
    reject_html: bool = True,
) -> Iterator[FetchedResource]:
    """Create and clean up a guarded temporary copy of one source resource."""
    with SafeHttpClient().download(
        url,
        headers=headers,
        timeout=timeout,
        reject_html=reject_html,
    ) as fetched:
        yield fetched
