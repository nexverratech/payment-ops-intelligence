"""Local-only network policy.

This project must never talk to a server outside this computer during normal
operation. This module turns that rule into code instead of a promise:

* `is_loopback_host` / `require_local_url` reject configuration that points
  at a non-local address (used by config.py at startup).
* `install_network_guard` blocks any Python socket connection to a non-local
  address (used by the tests, so a forgotten outbound call fails loudly).

It uses only the Python standard library on purpose, so it works even before
any packages are installed.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

_LOCAL_NAMES = {"localhost"}

# Docker (Phase 8): containers reach each other by service name ("db", "api") and the
# Windows host as "host.docker.internal", all on Docker's private network on this computer.
# These names are only accepted when explicitly listed (LOCAL_SERVICE_HOSTS), and only as
# single-word names, so a public hostname such as "ollama.com" can never be allowed this way.
DOCKER_HOST_ALIAS = "host.docker.internal"
_SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def valid_service_host(name: str) -> bool:
    return name == DOCKER_HOST_ALIAS or bool(_SERVICE_NAME.match(name))


class NetworkPolicyError(RuntimeError):
    """Raised when code tries to reach a host outside this computer."""


def is_loopback_host(host: str | None, service_hosts: tuple[str, ...] = ()) -> bool:
    """Return True only for addresses that stay on this computer.

    Accepts "localhost", 127.0.0.0/8 and ::1, plus the Docker service names explicitly
    listed in `service_hosts`. Everything else is rejected, including 0.0.0.0 (which means
    "every network card", not "local only").
    """
    if not host:
        return False
    host = host.strip().strip("[]").lower()
    if host in _LOCAL_NAMES or (host in service_hosts and valid_service_host(host)):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname such as "ollama.com" is not loopback


def require_local_url(url: str, service_hosts: tuple[str, ...] = ()) -> str:
    """Return `url` unchanged if it is an http(s) URL on this computer; else raise ValueError."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"URL must start with http:// or https:// (got scheme '{parts.scheme}')")
    if not is_loopback_host(parts.hostname, service_hosts):
        raise ValueError(
            f"URL host '{parts.hostname}' is not local. This application only allows "
            "localhost / 127.0.0.1 so that no data leaves your computer."
        )
    return url


def install_network_guard() -> Callable[[], None]:
    """Block socket connections and DNS lookups for non-local hosts.

    Returns a function that removes the guard again.
    """
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def _check(address: object) -> None:
        # AF_UNIX sockets use a file path (str/bytes): always local.
        if isinstance(address, (str, bytes)):
            return
        host = address[0] if isinstance(address, tuple) and address else None
        if not is_loopback_host(str(host) if host is not None else None):
            raise NetworkPolicyError(f"Blocked outbound connection to {address!r}: only localhost is allowed")

    def guarded_connect(self: socket.socket, address: object) -> None:
        _check(address)
        return original_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: object) -> int:
        _check(address)
        return original_connect_ex(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host is not None and not is_loopback_host(host if isinstance(host, str) else host.decode()):
            raise NetworkPolicyError(f"Blocked DNS lookup for '{host}': only localhost is allowed")
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = guarded_getaddrinfo

    def uninstall() -> None:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = original_getaddrinfo

    return uninstall
