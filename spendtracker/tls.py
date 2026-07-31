"""TLS for the local web interface.

Serving bank statements to a phone over plain HTTP means the passphrase and the
statement data cross the network in the clear. On home Wi-Fi that is readable by
anything already on the network. This module closes that gap.

## Why a local CA rather than a bare self-signed certificate

The obvious approach is one self-signed certificate. It encrypts, and it is two
lines of code. It is also the wrong answer, for a reason worth stating plainly:
every device shows a full-page security warning, every time, and the only way
through is to tap "proceed anyway".

That does two bad things. It trains the person to dismiss certificate warnings,
which is precisely the reflex a real attack depends on. And dismissing the
warning means nothing was verified, so you get encryption without
authentication — a passive eavesdropper is defeated, but anyone able to
intercept and answer traffic can impersonate the server and collect the
passphrase.

So instead: generate a tiny certificate authority once, install its certificate
on the devices you use, and issue the server a certificate signed by it. No
warnings, and the connection is genuinely authenticated.

## The cost of that, stated honestly

Installing a root CA on your phone means that phone will trust *anything* signed
by that CA, for any website. If the CA private key is stolen, whoever has it can
impersonate any site to that device until you remove the CA.

The mitigations here: the key never leaves the machine that generated it, it is
written with owner-only permissions, and it is only ever used to sign
certificates for this app. `spendtracker tls trust-file` exports the public
certificate — the only part that should ever be copied to another device. If you
are uneasy about that tradeoff, it is a reasonable thing to be uneasy about; the
offline HTML report needs no certificates at all.

## Implementation notes

Serving TLS uses `ssl` from the standard library, so the zero-dependency promise
holds for everything except *creating* certificates, which cannot be done in
pure Python without writing an X.509 encoder by hand. That step prefers the
`cryptography` package and falls back to the `openssl` command line, which is
present on macOS and Linux and ships with Git for Windows. If neither exists the
error says so and points at the offline report.

Certificates carry Subject Alternative Names for every address the machine
answers on. Browsers have ignored Common Name for years — a certificate without
matching SANs fails no matter what its CN says.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

CA_KEY_NAME = "ca-key.pem"
CA_CERT_NAME = "ca-cert.pem"
SERVER_KEY_NAME = "server-key.pem"
SERVER_CERT_NAME = "server-cert.pem"
TLS_DIRNAME = "tls"

CA_VALID_DAYS = 3650  # ten years: reinstalling a root CA on a phone is a chore

# Apple platforms reject manually installed leaf certificates valid for much
# longer than this, so keep it under the limit and renew instead.
SERVER_VALID_DAYS = 397

RENEW_WARNING_DAYS = 30

CA_SUBJECT = "spendtracker local CA"
SERVER_SUBJECT = "spendtracker"


class TlsError(Exception):
    """Raised when certificates cannot be created, read, or used."""


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------


@dataclass
class TlsPaths:
    root: Path

    @property
    def ca_key(self) -> Path:
        return self.root / CA_KEY_NAME

    @property
    def ca_cert(self) -> Path:
        return self.root / CA_CERT_NAME

    @property
    def server_key(self) -> Path:
        return self.root / SERVER_KEY_NAME

    @property
    def server_cert(self) -> Path:
        return self.root / SERVER_CERT_NAME

    @property
    def has_ca(self) -> bool:
        return self.ca_key.exists() and self.ca_cert.exists()

    @property
    def has_server(self) -> bool:
        return self.server_key.exists() and self.server_cert.exists()

    @property
    def ready(self) -> bool:
        return self.has_ca and self.has_server


def tls_paths(data_dir: Path | str) -> TlsPaths:
    return TlsPaths(Path(data_dir) / TLS_DIRNAME)


def _restrict(path: Path) -> None:
    """Owner-only. Windows ignores this; documented rather than pretended away."""
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


# ---------------------------------------------------------------------------
# What addresses should the certificate cover
# ---------------------------------------------------------------------------


def local_addresses() -> list[str]:
    """Every address this machine plausibly answers on, best effort.

    A certificate is only valid for the names and addresses it lists, and the
    address a phone uses is the LAN one, not 127.0.0.1. Getting this wrong is
    the most likely reason a certificate appears broken, so cast wide.
    """
    found: list[str] = ["127.0.0.1", "::1"]

    # The standard trick: opening a UDP socket to a public address makes the
    # OS choose an outbound interface. No packets are sent.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            found.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            address = info[4][0]
            if "%" in address:  # strip IPv6 zone identifiers
                address = address.split("%")[0]
            found.append(address)
    except (OSError, socket.gaierror):
        pass

    out: list[str] = []
    for address in found:
        try:
            ipaddress.ip_address(address)
        except ValueError:
            continue
        if address not in out:
            out.append(address)
    return out


def local_hostnames() -> list[str]:
    names = ["localhost", "spendtracker.local"]
    try:
        hostname = socket.gethostname()
        for candidate in (hostname, f"{hostname}.local"):
            if candidate and candidate not in names:
                names.append(candidate)
    except OSError:
        pass
    return names


@dataclass
class CertRequest:
    hostnames: list[str] = field(default_factory=local_hostnames)
    addresses: list[str] = field(default_factory=local_addresses)

    def merged(self, extra: list[str] | None) -> CertRequest:
        """Fold in user-supplied names or addresses."""
        hostnames = list(self.hostnames)
        addresses = list(self.addresses)
        for item in extra or []:
            item = item.strip()
            if not item:
                continue
            try:
                ipaddress.ip_address(item)
            except ValueError:
                if item not in hostnames:
                    hostnames.append(item)
            else:
                if item not in addresses:
                    addresses.append(item)
        return CertRequest(hostnames=hostnames, addresses=addresses)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _generate_with_cryptography(paths: TlsPaths, request: CertRequest) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = datetime.now(timezone.utc)
    # A minute of backdating absorbs clock skew between the laptop and phone,
    # which otherwise shows up as "certificate not yet valid".
    start = now - timedelta(minutes=1)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, CA_SUBJECT)]
    )
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(now + timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans: list[x509.GeneralName] = [
        x509.DNSName(name) for name in request.hostnames
    ]
    for address in request.addresses:
        sans.append(x509.IPAddress(ipaddress.ip_address(address)))

    server_cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVER_SUBJECT)])
        )
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(now + timedelta(days=SERVER_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    paths.root.mkdir(parents=True, exist_ok=True)
    no_encryption = serialization.NoEncryption()
    paths.ca_key.write_bytes(
        ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            no_encryption,
        )
    )
    paths.ca_cert.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    paths.server_key.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            no_encryption,
        )
    )
    paths.server_cert.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))


def _openssl_binary() -> str | None:
    found = shutil.which("openssl")
    if found:
        return found
    # Git for Windows bundles one but does not put it on PATH.
    for candidate in (
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def _generate_with_openssl(paths: TlsPaths, request: CertRequest) -> None:
    binary = _openssl_binary()
    if binary is None:
        raise TlsError("openssl not found")

    paths.root.mkdir(parents=True, exist_ok=True)
    entries = [f"DNS:{name}" for name in request.hostnames]
    entries += [f"IP:{address}" for address in request.addresses]
    san = ",".join(entries)

    def run(args: list[str]) -> None:
        result = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise TlsError(f"openssl failed: {result.stderr.strip()[:300]}")

    run(
        [
            "req", "-x509", "-newkey", "rsa:3072", "-nodes",
            "-keyout", str(paths.ca_key), "-out", str(paths.ca_cert),
            "-days", str(CA_VALID_DAYS), "-subj", f"/CN={CA_SUBJECT}",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        csr = Path(tmp) / "server.csr"
        ext = Path(tmp) / "server.ext"
        ext.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "extendedKeyUsage=serverAuth\n"
            f"subjectAltName={san}\n",
            encoding="utf-8",
        )
        run(
            [
                "req", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(paths.server_key), "-out", str(csr),
                "-subj", f"/CN={SERVER_SUBJECT}",
            ]
        )
        run(
            [
                "x509", "-req", "-in", str(csr),
                "-CA", str(paths.ca_cert), "-CAkey", str(paths.ca_key),
                "-CAcreateserial", "-out", str(paths.server_cert),
                "-days", str(SERVER_VALID_DAYS), "-sha256",
                "-extfile", str(ext),
            ]
        )


def generate(
    data_dir: Path | str,
    *,
    extra_names: list[str] | None = None,
    reuse_ca: bool = True,
) -> TlsPaths:
    """Create a CA (if needed) and a server certificate covering this machine.

    ``reuse_ca`` keeps an existing CA so that renewing the server certificate
    does not force you to reinstall the CA on every phone — the whole reason the
    CA is separate from the server certificate.
    """
    paths = tls_paths(data_dir)
    request = CertRequest().merged(extra_names)

    existing_ca = paths.has_ca and reuse_ca
    if existing_ca:
        _reissue_server(paths, request)
    else:
        errors = []
        for backend in (_generate_with_cryptography, _generate_with_openssl):
            try:
                backend(paths, request)
                break
            except ImportError as exc:
                errors.append(f"cryptography: {exc}")
            except TlsError as exc:
                errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001 - report, do not mask
                errors.append(f"{backend.__name__}: {exc}")
        else:
            raise TlsError(
                "cannot create certificates: neither the `cryptography` package "
                "nor the `openssl` command is available.\n\n"
                "    pip install 'spendtracker[tls]'\n\n"
                "installs what is needed. Alternatively, skip TLS entirely and "
                "use the offline HTML report:\n\n"
                "    spendtracker report --html spending.html\n\n"
                f"(tried: {'; '.join(errors)})"
            )

    for path in (paths.ca_key, paths.server_key):
        _restrict(path)
    return paths


def _reissue_server(paths: TlsPaths, request: CertRequest) -> None:
    """Issue a fresh server certificate from the existing CA."""
    try:
        _reissue_with_cryptography(paths, request)
        return
    except ImportError:
        pass

    if _openssl_binary() is None:
        raise TlsError(
            "cannot reissue the server certificate: neither `cryptography` nor "
            "`openssl` is available"
        )

    binary = _openssl_binary()
    entries = [f"DNS:{n}" for n in request.hostnames]
    entries += [f"IP:{a}" for a in request.addresses]
    with tempfile.TemporaryDirectory() as tmp:
        csr = Path(tmp) / "server.csr"
        ext = Path(tmp) / "server.ext"
        ext.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "extendedKeyUsage=serverAuth\n"
            f"subjectAltName={','.join(entries)}\n",
            encoding="utf-8",
        )
        for args in (
            ["req", "-newkey", "rsa:2048", "-nodes", "-keyout",
             str(paths.server_key), "-out", str(csr), "-subj",
             f"/CN={SERVER_SUBJECT}"],
            ["x509", "-req", "-in", str(csr), "-CA", str(paths.ca_cert),
             "-CAkey", str(paths.ca_key), "-CAcreateserial", "-out",
             str(paths.server_cert), "-days", str(SERVER_VALID_DAYS),
             "-sha256", "-extfile", str(ext)],
        ):
            result = subprocess.run(
                [binary, *args], capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                raise TlsError(f"openssl failed: {result.stderr.strip()[:300]}")


def _reissue_with_cryptography(paths: TlsPaths, request: CertRequest) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca_key = serialization.load_pem_private_key(
        paths.ca_key.read_bytes(), password=None
    )
    ca_cert = x509.load_pem_x509_certificate(paths.ca_cert.read_bytes())

    now = datetime.now(timezone.utc)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans: list[x509.GeneralName] = [x509.DNSName(n) for n in request.hostnames]
    sans += [x509.IPAddress(ipaddress.ip_address(a)) for a in request.addresses]

    cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVER_SUBJECT)])
        )
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=SERVER_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    paths.server_key.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    paths.server_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


@dataclass
class CertInfo:
    subject: str
    not_before: datetime
    not_after: datetime
    hostnames: list[str]
    addresses: list[str]

    @property
    def days_remaining(self) -> int:
        return (self.not_after - datetime.now(timezone.utc)).days

    @property
    def expired(self) -> bool:
        return self.days_remaining < 0

    @property
    def expiring_soon(self) -> bool:
        return 0 <= self.days_remaining <= RENEW_WARNING_DAYS

    def covers(self, address: str) -> bool:
        return address in self.addresses or address in self.hostnames


def describe(cert_path: Path) -> CertInfo:
    """Read a certificate. Raises TlsError if it cannot be parsed."""
    if not cert_path.exists():
        raise TlsError(f"{cert_path} does not exist")

    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        try:
            san = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
            hostnames = list(san.get_values_for_type(x509.DNSName))
            addresses = [str(a) for a in san.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            hostnames, addresses = [], []

        return CertInfo(
            subject=cert.subject.rfc4514_string(),
            not_before=_aware(cert.not_valid_before_utc),
            not_after=_aware(cert.not_valid_after_utc),
            hostnames=hostnames,
            addresses=addresses,
        )
    except ImportError:
        pass

    # Fall back to ssl's own parser, which needs no third-party package.
    try:
        decoded = ssl._ssl._test_decode_cert(str(cert_path))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise TlsError(f"cannot read {cert_path}: {exc}") from exc

    hostnames, addresses = [], []
    for kind, value in decoded.get("subjectAltName", ()):
        if kind == "DNS":
            hostnames.append(value)
        elif kind == "IP Address":
            addresses.append(value)

    return CertInfo(
        subject=str(decoded.get("subject", "")),
        not_before=_parse_openssl_date(decoded["notBefore"]),
        not_after=_parse_openssl_date(decoded["notAfter"]),
        hostnames=hostnames,
        addresses=addresses,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_openssl_date(value: str) -> datetime:
    return datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=timezone.utc
    )


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


def ssl_context(paths: TlsPaths) -> ssl.SSLContext:
    """An SSL context for the server. Uses only the standard library.

    TLS 1.2 is the floor: everything that can reach this app supports it, and
    the older protocols have nothing to recommend them.
    """
    if not paths.ready:
        raise TlsError(
            "no certificates found — run `spendtracker tls setup` first"
        )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(str(paths.server_cert), str(paths.server_key))
    except (ssl.SSLError, OSError) as exc:
        raise TlsError(
            f"certificate and key do not load ({exc}). Re-run "
            "`spendtracker tls setup --renew` to issue a fresh pair."
        ) from exc
    return context
