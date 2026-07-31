"""TLS for the web interface.

The point of this feature is that the passphrase and the statement data stop
crossing the network in the clear. So the tests that matter are: a certificate
is produced that browsers will actually accept (which means Subject Alternative
Names, not Common Name), a client trusting the CA completes a handshake, a
client that does not trust it refuses, and the private keys stay private.

Certificate generation needs either `cryptography` or `openssl`, so tests that
need one skip cleanly where neither exists — but `test_serving` covers the
standard-library serving path, which is the part with no dependency.
"""

from __future__ import annotations

import http.client
import http.server
import shutil
import socket
import ssl
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spendtracker import tls as tls_mod


def _can_generate() -> bool:
    try:
        import cryptography  # noqa: F401

        return True
    except ImportError:
        return shutil.which("openssl") is not None


needs_generation = pytest.mark.skipif(
    not _can_generate(), reason="needs cryptography or openssl to make certificates"
)


@pytest.fixture
def certs(tmp_path: Path) -> tls_mod.TlsPaths:
    return tls_mod.generate(tmp_path)


# ---------------------------------------------------------------------------
# What the certificate has to cover
# ---------------------------------------------------------------------------


def test_local_addresses_always_include_loopback() -> None:
    addresses = tls_mod.local_addresses()
    assert "127.0.0.1" in addresses


def test_local_addresses_are_all_parseable() -> None:
    """A malformed entry would make certificate generation fail obscurely."""
    import ipaddress

    for address in tls_mod.local_addresses():
        ipaddress.ip_address(address)  # must not raise


def test_extra_names_are_sorted_into_hostnames_and_addresses() -> None:
    request = tls_mod.CertRequest(hostnames=[], addresses=[]).merged(
        ["192.168.1.42", "my-laptop.local", "10.0.0.7", ""]
    )

    assert request.addresses == ["192.168.1.42", "10.0.0.7"]
    assert request.hostnames == ["my-laptop.local"]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


@needs_generation
def test_setup_produces_a_ca_and_a_server_certificate(certs) -> None:
    assert certs.has_ca
    assert certs.has_server
    assert certs.ready


@needs_generation
def test_the_certificate_carries_subject_alternative_names(certs) -> None:
    """Browsers have ignored Common Name for years. No SANs, no connection."""
    info = tls_mod.describe(certs.server_cert)

    assert "localhost" in info.hostnames
    assert any(a.startswith("127.") for a in info.addresses)


@needs_generation
def test_every_local_address_is_covered(certs) -> None:
    """The address the phone uses is the LAN one, not loopback."""
    info = tls_mod.describe(certs.server_cert)

    for address in tls_mod.local_addresses():
        if ":" in address:
            continue  # IPv6 formatting differs between backends
        assert address in info.addresses, f"{address} not covered"


@needs_generation
def test_extra_names_reach_the_certificate(tmp_path: Path) -> None:
    certs = tls_mod.generate(tmp_path, extra_names=["192.168.99.99", "phone.local"])
    info = tls_mod.describe(certs.server_cert)

    assert "192.168.99.99" in info.addresses
    assert "phone.local" in info.hostnames


@needs_generation
def test_the_server_certificate_is_short_lived_enough_for_apple(certs) -> None:
    """iOS rejects manually trusted leaf certificates with long lifetimes."""
    info = tls_mod.describe(certs.server_cert)
    assert 0 < info.days_remaining <= 398


@needs_generation
def test_the_ca_is_long_lived(certs) -> None:
    """Reinstalling a root CA on every device is a chore; do it rarely."""
    info = tls_mod.describe(certs.ca_cert)
    assert info.days_remaining > 3000


@needs_generation
def test_the_certificate_is_valid_now(certs) -> None:
    """A minute of backdating absorbs clock skew between laptop and phone."""
    info = tls_mod.describe(certs.server_cert)
    now = datetime.now(timezone.utc)

    assert info.not_before <= now
    assert info.not_after > now
    assert info.expired is False


@needs_generation
def test_private_keys_are_owner_only(certs) -> None:
    import os
    import sys

    if sys.platform == "win32":
        pytest.skip("POSIX permissions are not meaningful on Windows")

    for key in (certs.ca_key, certs.server_key):
        mode = os.stat(key).st_mode & 0o777
        assert mode == 0o600, f"{key} is {oct(mode)}"


@needs_generation
def test_renewing_keeps_the_same_ca(tmp_path: Path) -> None:
    """Otherwise every renewal means reinstalling the CA on every device."""
    first = tls_mod.generate(tmp_path)
    ca_before = first.ca_cert.read_bytes()
    server_before = first.server_cert.read_bytes()

    second = tls_mod.generate(tmp_path, reuse_ca=True)

    assert second.ca_cert.read_bytes() == ca_before, "CA must survive a renewal"
    assert second.server_cert.read_bytes() != server_before, "server cert must be fresh"


@needs_generation
def test_starting_over_replaces_the_ca(tmp_path: Path) -> None:
    first = tls_mod.generate(tmp_path)
    ca_before = first.ca_cert.read_bytes()

    second = tls_mod.generate(tmp_path, reuse_ca=False)

    assert second.ca_cert.read_bytes() != ca_before


# ---------------------------------------------------------------------------
# Serving — the standard-library half, with no dependency
# ---------------------------------------------------------------------------


@needs_generation
def test_ssl_context_refuses_old_protocols(certs) -> None:
    context = tls_mod.ssl_context(certs)
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_ssl_context_without_certificates_says_what_to_do(tmp_path: Path) -> None:
    paths = tls_mod.tls_paths(tmp_path)
    with pytest.raises(tls_mod.TlsError, match="tls setup"):
        tls_mod.ssl_context(paths)


@needs_generation
def test_a_client_trusting_the_ca_completes_a_handshake(certs) -> None:
    """The end-to-end property: real TLS, verified, no warnings."""
    with _serving(certs) as port:
        context = ssl.create_default_context(cafile=str(certs.ca_cert))
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as secure:
                assert secure.version() in ("TLSv1.2", "TLSv1.3")
                assert secure.cipher() is not None


@needs_generation
def test_the_ip_address_works_not_just_the_hostname(certs) -> None:
    """The phone connects to an IP, so the IP SAN is the one that matters."""
    with _serving(certs) as port:
        context = ssl.create_default_context(cafile=str(certs.ca_cert))
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname="127.0.0.1") as secure:
                assert secure.version() is not None


@needs_generation
def test_a_client_that_does_not_trust_the_ca_refuses(certs) -> None:
    """If this ever passed, the certificate would be trusted by everyone."""
    with _serving(certs) as port:
        context = ssl.create_default_context()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with pytest.raises(ssl.SSLCertVerificationError):
                context.wrap_socket(raw, server_hostname="localhost")


@needs_generation
def test_a_wrong_hostname_is_rejected(certs) -> None:
    """SAN checking must actually be enforced, not merely present."""
    with _serving(certs) as port:
        context = ssl.create_default_context(cafile=str(certs.ca_cert))
        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            with pytest.raises(ssl.SSLCertVerificationError):
                context.wrap_socket(raw, server_hostname="not-this-machine.example")


@needs_generation
def test_plain_http_to_a_tls_port_fails_rather_than_serving(certs) -> None:
    with _serving(certs) as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        with pytest.raises((ConnectionResetError, http.client.HTTPException, OSError)):
            connection.request("GET", "/")
            connection.getresponse()


# ---------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence the test output
        pass


class _serving:
    """Run a throwaway HTTPS server wrapped in our own SSL context."""

    def __init__(self, certs: tls_mod.TlsPaths) -> None:
        self.certs = certs

    def __enter__(self) -> int:
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.server.socket = tls_mod.ssl_context(self.certs).wrap_socket(
            self.server.socket, server_side=True
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.socket.getsockname()[1]

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
