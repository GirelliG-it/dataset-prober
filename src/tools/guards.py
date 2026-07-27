"""
src/tools/guards.py

Fetch-time input hardening.

Scope: deciding whether a URL the tool *discovered* — a crawler link, a Tavily
search result, a CKAN resource URL — is safe to fetch. URLs the user types
directly are trusted input and are not checked here.

Explicitly NOT in scope: whether the data that comes back is safe to publish,
redistribute, or share. "Safe to fetch" and "safe to publish" are different
concerns that happen to share a word; keep the second one in its own module.

The HTML-landing-page guards (`response_is_html`, `_reject_if_html`) are
siblings of this code and currently still live in `tools/base.py`. Moving them
here would consolidate the guard cluster — deferred deliberately so this file
lands as a pure addition.
"""

import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlparse

# Only these schemes are ever fetched. Blocks file://, ftp://, data:, gopher://
# and friends — none of which a legitimate open-data link needs.
ALLOWED_SCHEMES = frozenset({"http", "https"})


@lru_cache(maxsize=512)
def _resolve(hostname: str) -> tuple[str, ...]:
    """
    Resolve `hostname` to its unique IP addresses, as strings.

    Cached because a single crawled page can carry hundreds of links across a
    handful of hosts, and `socket.getaddrinfo` is a blocking call with no
    timeout parameter — a slow or hostile resolver would otherwise stall the
    crawl once per link instead of once per host. The cache lives for the
    process lifetime, which is the right scale for a CLI run.

    Failed lookups raise `socket.gaierror` and are NOT cached (lru_cache only
    stores successful returns), so a transient DNS failure is retried rather
    than remembered.
    """
    infos = socket.getaddrinfo(hostname, None)
    return tuple({info[4][0] for info in infos})


def is_safe_url(url: str) -> tuple[bool, str]:
    """
    Check whether a discovered URL is safe to fetch.

    Returns `(True, "")` if the URL passes, otherwise `(False, reason)` where
    `reason` is a short human-readable explanation suitable for printing.

    Rejects, in order of cost:
      1. schemes outside ALLOWED_SCHEMES  — no network call needed
      2. URLs with no hostname
      3. hostnames that fail to resolve
      4. hostnames resolving to a private, loopback, link-local, multicast,
         reserved, or unspecified address

    Fails closed: anything unparseable or unresolvable is rejected rather than
    waved through.

    Obfuscated hosts need no special handling. `http://0x7f000001/` and
    `http://2130706433/` are resolved by getaddrinfo to 127.0.0.1 and caught by
    the loopback check; `http://example.com@127.0.0.1/` is parsed by urlparse
    into hostname `127.0.0.1`, and the userinfo part is discarded.

    Known limitation — DNS rebinding. The hostname is resolved here, at check
    time, and resolved again independently by the HTTP client at connect time.
    A host that answers with a public address for the first lookup and a
    private one for the second would slip through. Closing that gap means
    resolving once and pinning the resulting IP for the actual connection,
    which is more plumbing than a local CLI warrants. Recorded so it is a
    documented decision rather than an oversight.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        return False, f"scheme '{parsed.scheme}' not allowed"

    # .hostname lowercases, strips any user:pass@ prefix, and strips the
    # brackets from an IPv6 literal — all of which we want.
    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname"

    try:
        addresses = _resolve(hostname)
    except socket.gaierror as e:
        return False, f"DNS lookup failed: {e}"

    if not addresses:
        return False, "hostname resolved to no addresses"

    for address in addresses:
        # An IPv6 link-local address can arrive carrying its zone index
        # (fe80::1%eth0). ip_address() rejects that string, so strip the zone
        # before parsing — otherwise the exact case this guard exists to block
        # would raise instead of returning False.
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            return False, f"unparseable address: {address}"

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, f"resolves to non-public address ({ip})"

    return True, ""
