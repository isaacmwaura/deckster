"""Self-signed TLS: cert generation, fingerprint, and SSL context."""
import ssl

from agent import tls


def test_ensure_cert_creates_and_is_idempotent(tmp_path):
    c1, k1 = tls.ensure_cert(tmp_path)
    assert c1.exists() and k1.exists()
    body = c1.read_bytes()
    c2, _ = tls.ensure_cert(tmp_path)      # second call reuses the existing pair
    assert c2.read_bytes() == body


def test_fingerprint_is_sha256_colon_hex(tmp_path):
    c, _ = tls.ensure_cert(tmp_path)
    parts = tls.fingerprint(c).split(":")
    assert len(parts) == 32                # SHA-256 = 32 bytes
    assert all(len(p) == 2 for p in parts)


def test_ssl_context_loads(tmp_path):
    c, k = tls.ensure_cert(tmp_path)
    ctx = tls.ssl_context(c, k)
    assert isinstance(ctx, ssl.SSLContext)
