"""Tests for the local-only network policy (standard library only)."""

import socket

import pytest

from payops.netpolicy import NetworkPolicyError, is_loopback_host, require_local_url


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "127.5.5.5", "::1", "[::1]"])
def test_loopback_hosts_are_allowed(host):
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "ollama.com", "8.8.8.8", "", None])
def test_non_local_hosts_are_rejected(host):
    assert not is_loopback_host(host)


def test_require_local_url_accepts_localhost():
    assert require_local_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434"


@pytest.mark.parametrize(
    "url",
    ["https://ollama.com/api", "http://192.168.1.20:11434", "ftp://127.0.0.1", "127.0.0.1:11434"],
)
def test_require_local_url_rejects_remote_or_malformed(url):
    with pytest.raises(ValueError):
        require_local_url(url)


def test_guard_blocks_outbound_connection():
    # conftest.py installed the guard for the whole session.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(NetworkPolicyError):
            s.connect(("93.184.216.34", 80))  # a public IP; must never be reached


def test_guard_blocks_dns_lookup_of_public_name():
    with pytest.raises(NetworkPolicyError):
        socket.getaddrinfo("example.com", 443)


def test_guard_allows_localhost():
    # Nothing listens on this port, so we expect "connection refused"
    # (proof the guard let the attempt through), not NetworkPolicyError.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        result = s.connect_ex(("127.0.0.1", 9))
        assert result != 0


def test_docker_names_need_an_explicit_listing():
    from payops.netpolicy import is_loopback_host, require_local_url

    assert not is_loopback_host("db")
    assert is_loopback_host("db", ("db",))
    assert require_local_url("http://api:8000", ("api",)) == "http://api:8000"
    assert not is_loopback_host("evil.example.org", ("evil.example.org",))   # dotted names are never accepted
