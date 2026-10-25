"""Outbound requests to organisation-configured (BYOK) provider URLs.

Org admins choose the ``api_base`` of their own providers, and the gateway relays
whatever comes back, so an unchecked URL would let them read internal services
(cloud metadata at 169.254.169.254, databases, other sandboxes) through the
gateway. Each request therefore resolves the host once, refuses non-public
addresses, and connects to that exact IP while keeping the original ``Host``
header and TLS server name - so a DNS answer that changes between the check and
the connection (DNS rebinding) cannot redirect it.

Platform providers are configured by the operator and are trusted as is.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from .auth import GatewayError

# Headers an organisation may not set on its provider requests.
RESERVED_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}


def is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_provider_url(url: str, *, allow_private: bool) -> str:
    """Syntax/scheme check for a BYOK ``api_base`` (addresses are checked per request)."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("api_base must be an http(s) URL")
    if parts.scheme == "http" and not allow_private:
        raise ValueError("api_base must use https")
    if parts.username or parts.password:
        raise ValueError("api_base must not contain credentials; use the API key field")
    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not allow_private and not is_public_ip(host):
        raise ValueError("api_base must not point to a private or local address")
    if host in ("localhost",) and not allow_private:
        raise ValueError("api_base must not point to a private or local address")
    return url.strip().rstrip("/")


@dataclass
class PinnedTarget:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    extensions: dict[str, object] = field(default_factory=dict)


async def resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def pin(url: str, *, allow_private: bool) -> PinnedTarget:
    """Resolve ``url``'s host and return a request target bound to a vetted address."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        raise GatewayError(400, "invalid_request_error", "Provider URL is invalid")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = await resolve(host, port)
    except OSError:
        raise GatewayError(502, "upstream_error", f"Cannot resolve provider host {host}") from None
    if not addresses:
        raise GatewayError(502, "upstream_error", f"Cannot resolve provider host {host}")
    if not allow_private and not all(is_public_ip(a) for a in addresses):
        raise GatewayError(403, "permission_error", "Provider URL resolves to a private or local address")
    ip = addresses[0]
    netloc_ip = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        netloc_ip += f":{parts.port}"
    host_header = host if not parts.port else f"{host}:{parts.port}"
    target = PinnedTarget(
        url=urlunsplit((parts.scheme, netloc_ip, parts.path, parts.query, parts.fragment)),
        headers={"Host": host_header},
    )
    if parts.scheme == "https":
        target.extensions["sni_hostname"] = host
    return target


def clean_extra_headers(headers: dict | None) -> dict[str, str]:
    return {str(k): str(v) for k, v in (headers or {}).items() if str(k).lower() not in RESERVED_HEADERS}
