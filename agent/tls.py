"""Self-signed TLS for the agent.

There is no public certificate authority for a private LAN IP, so the agent mints
its own long-lived self-signed certificate (localhost + LAN IP + hostname as Subject
Alternative Names) and serves HTTPS with it. Browsers show a one-time warning; the
native Android app instead *pins* this certificate's SHA-256 fingerprint, giving a
clean, warning-free encrypted connection with no certificate install on the phone.

The cert is generated once and persisted under the data dir. Because the app pins
the fingerprint (not the hostname), a changing DHCP LAN IP does not break it.
"""
from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
from pathlib import Path

from .net import lan_ip


def _san_entries():
    """Subject Alternative Names: localhost, hostname, 127.0.0.1, and the LAN IP."""
    from cryptography import x509

    entries = [x509.DNSName("localhost")]
    host = socket.gethostname().split(".")[0]
    if host and host.lower() != "localhost":
        entries.append(x509.DNSName(host))
        entries.append(x509.DNSName(f"{host}.local"))
    ips = {"127.0.0.1"}
    lan = lan_ip()
    if lan:
        ips.add(lan)
    for ip in ips:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    return entries


def ensure_cert(data_dir: Path) -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating a self-signed pair on first use."""
    cert_path = data_dir / "cert.pem"
    key_path = data_dir / "key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Deckster")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(_san_entries()), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def fingerprint(cert_path: Path) -> str:
    """SHA-256 fingerprint (uppercase colon-hex) the Android app pins."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx
