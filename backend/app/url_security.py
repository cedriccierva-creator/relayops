"""Resolve, validate every address, then pin the request to one validated IP.

No second hostname resolution occurs in HTTPX. The original Host header and TLS
server name are retained for virtual hosting and certificate verification.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from app.config import Settings


class UnsafeDestination(ValueError):
    pass


class DestinationResolutionError(UnsafeDestination):
    """DNS unavailable: reject configuration, but allow bounded delivery retries."""


def parse_destination(value: str, settings: Settings) -> tuple[httpx.URL, bool]:
    try:
        parsed = urlsplit(value)
        # URLs are visible in the dashboard. Credentials and query tokens are forbidden.
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "?" in value or "#" in value
                or "\\" in value or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        url = httpx.URL(value)
        if "%" in url.host:
            raise ValueError
        origin = f"{parsed.scheme}://{parsed.netloc}"
        development = settings.ENVIRONMENT == "development" and origin in settings.DEV_ALLOWED_ORIGINS
        if parsed.scheme != "https" and not development:
            raise ValueError
        return url, development
    except (ValueError, httpx.InvalidURL):
        raise UnsafeDestination("Use a public HTTPS URL without credentials, query, or fragment") from None


def permitted_address(address: str, development: bool = False) -> bool:
    ip = ipaddress.ip_address(address)
    if str(ip) == "168.63.129.16":  # Azure platform virtual IP
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip in ipaddress.ip_network("64:ff9b::/96"):
        return False  # NAT64 may translate a seemingly global IPv6 address to private IPv4.
    # Reject mapped and transitional IPv6 addresses rather than relying on platform routing.
    if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo):
        return False
    if development and ip.is_loopback:
        return True
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


async def resolve_destination(value: str, settings: Settings) -> tuple[httpx.URL, str, str]:
    url, development = parse_destination(value, settings)
    port = url.port or (443 if url.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(url.host)
    except ValueError:
        try:
            records = await asyncio.get_running_loop().getaddrinfo(
                url.host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
            )
            addresses = list(dict.fromkeys(record[4][0] for record in records))
        except (OSError, UnicodeError):
            raise DestinationResolutionError("Destination DNS resolution failed") from None
    else:
        addresses = [str(literal)]
    if not addresses or any(not permitted_address(address, development) for address in addresses):
        raise UnsafeDestination("Destination resolves to a blocked address")
    # URL.copy_with brackets IPv6 correctly. HTTPX connects to this literal only.
    pinned = url.copy_with(host=addresses[0])
    return pinned, url.netloc.decode("ascii"), url.host


async def validate_destination(value: str, settings: Settings) -> None:
    try:
        async with asyncio.timeout(settings.DELIVERY_TIMEOUT_SECONDS):
            await resolve_destination(value, settings)
    except TimeoutError:
        raise UnsafeDestination("Destination DNS resolution timed out") from None
