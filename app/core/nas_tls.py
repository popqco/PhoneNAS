"""An explicitly paired PhoneNAS leaf, scoped to one WebDAV authority."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class NasTrust:
    authority: str
    pem: str
    fingerprint: str

    def https_handler(self) -> urllib.request.HTTPSHandler:
        return _PinnedHTTPSHandler(self)


def read_nas_trust(url: str, pem: str, fingerprint: str, *, check_time: bool = True) -> NasTrust | None:
    if not pem and not fingerprint:
        return None
    endpoint = re.fullmatch(r"https://([0-9.]+):([1-9][0-9]{0,4})/?", url)
    if not endpoint:
        raise ValueError("PhoneNAS 证书须用于根地址 https://IPv4:端口（不含共享路径）")
    try:
        ip = ipaddress.IPv4Address(endpoint[1])
    except ValueError:
        raise ValueError("PhoneNAS 证书地址不是规范的 IPv4") from None
    if int(endpoint[2]) > 65535 or ip.is_unspecified or ip.is_multicast:
        raise ValueError("PhoneNAS 证书地址或端口无效")
    expected = fingerprint.replace(":", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("请核对并输入服务器显示的完整 SHA-256 指纹")
    text = pem.strip()
    if len(pem) > 16 * 1024 or not re.fullmatch(
        r"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----", text
    ):
        raise ValueError("请导入一张公开 PEM 证书，不能导入私钥或证书链")

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID
    from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm

    try:
        cert = x509.load_pem_x509_certificate(text.encode("ascii"))
        cert.verify_directly_issued_by(cert)
        extensions = cert.extensions
        basic = extensions.get_extension_for_class(x509.BasicConstraints).value
        usage = extensions.get_extension_for_class(x509.KeyUsage).value
        eku = extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        names = list(extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
        key = cert.public_key()
    except (ValueError, x509.ExtensionNotFound, x509.DuplicateExtension, InvalidSignature, UnsupportedAlgorithm):
        raise ValueError("无效的 PhoneNAS 公开证书") from None
    if (basic.ca or not usage.digital_signature or usage.key_cert_sign
            or list(eku) != [ExtendedKeyUsageOID.SERVER_AUTH]
            or not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1)):
        raise ValueError("需要 PhoneNAS 的自签名服务器叶证书，而不是 CA 或客户端证书")
    if names != [x509.IPAddress(ip)]:
        raise ValueError("证书中的 IPv4 与连接地址不一致")
    now = datetime.now(timezone.utc)
    if check_time and not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise ValueError("PhoneNAS 证书尚未生效或已过期；请在手机上核对证书和系统时间")
    actual = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ValueError("证书与输入的 SHA-256 指纹不一致")
    return NasTrust(f"{ip}:{endpoint[2]}", cert.public_bytes(serialization.Encoding.PEM).decode("ascii"), actual)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, trust: NasTrust):
        super().__init__()
        self._trust = trust
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        self._context.load_verify_locations(cadata=trust.pem)

    def https_open(self, req):
        if req.host != self._trust.authority:
            raise urllib.error.URLError("PhoneNAS 证书只用于配对地址")
        return self.do_open(self._connection, req)

    def _connection(self, host, **kwargs):
        return _PinnedConnection(host, context=self._context, fingerprint=self._trust.fingerprint, **kwargs)


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, fingerprint, **kwargs):
        super().__init__(host, **kwargs)
        self._fingerprint = fingerprint

    def connect(self):
        super().connect()
        peer = self.sock.getpeercert(binary_form=True)
        if not peer or not hmac.compare_digest(hashlib.sha256(peer).hexdigest(), self._fingerprint):
            self.close()
            raise ssl.SSLCertVerificationError("PhoneNAS 服务器证书已变化")
