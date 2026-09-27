"""Reviewed address snapshots for fixed administrator-managed peer URLs."""

import ipaddress
import socket
from urllib.parse import urlsplit

from aiohttp.abc import AbstractResolver

from .contracts import Fault, require


class PinnedResolver(AbstractResolver):
    def __init__(self, pins):
        self.host = pins["host"]
        self.addresses = tuple(pins["addresses"])

    async def resolve(self, host, port=0, family=socket.AF_INET):
        require(host.lower() == self.host.lower(), "external_target_changed", 503)
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
            for address in self.addresses
        ]

    async def close(self):
        return None


async def reviewed_pins(url, networks):
    """Resolve once at save and refuse any answer outside the deployment's private ranges."""
    host = urlsplit(url).hostname
    require(host is not None, "invalid_input", 400)
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            infos = await __import__("asyncio").get_running_loop().getaddrinfo(
                host, None, type=socket.SOCK_STREAM
            )
        except OSError:
            raise Fault("external_address_unavailable", 503) from None
        addresses = sorted({ipaddress.ip_address(item[4][0]) for item in infos}, key=str)
    require(0 < len(addresses) <= 8, "external_address_unavailable", 503)
    allowed = [ipaddress.ip_network(item, strict=True) for item in networks]
    require(
        all(
            address.is_private
            and not address.is_link_local
            and not address.is_multicast
            and not address.is_unspecified
            and any(address in network for network in allowed)
            for address in addresses
        ),
        "external_target_forbidden",
        403,
    )
    return {"host": host, "addresses": [str(address) for address in addresses]}


def assert_pins(url, pins, networks):
    """Recheck persisted snapshots against current deployment policy without DNS or sockets."""
    require(
        isinstance(pins, dict)
        and set(pins) == {"host", "addresses"}
        and pins["host"] == urlsplit(url).hostname
        and isinstance(pins["addresses"], list)
        and 1 <= len(pins["addresses"]) <= 8,
        "external_target_forbidden", 403,
    )
    try:
        addresses = [ipaddress.ip_address(item) for item in pins["addresses"]]
        allowed = [ipaddress.ip_network(item, strict=True) for item in networks]
    except (ValueError, TypeError):
        raise Fault("external_target_forbidden", 403) from None
    require(
        all(
            address.is_private
            and not address.is_link_local
            and not address.is_multicast
            and not address.is_unspecified
            and any(address in network for network in allowed)
            for address in addresses
        ),
        "external_target_forbidden", 403,
    )
