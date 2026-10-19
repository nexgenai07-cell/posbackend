"""
Reading ZATCA certificates: what the status page shows and what onboarding checks.

A CSID response's `binarySecurityToken` is base64 of the certificate's own
base64 DER text. Devices store that inner base64 DER (one line, no PEM
headers), which is also exactly what goes into an invoice's
ds:X509Certificate.
"""

import base64
import re
from dataclasses import dataclass
from datetime import datetime

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID, ObjectIdentifier

_UID = ObjectIdentifier("0.9.2342.19200300.100.1.1")
_SERIAL_NUMBER = NameOID.SURNAME  # the EGS serial rides in SN (2.5.4.4)
_TITLE = NameOID.TITLE  # invoice types, e.g. 1100
_REGISTERED_ADDRESS = ObjectIdentifier("2.5.4.26")
_VAT_IN_TEXT = re.compile(r"3\d{13}3")


@dataclass(frozen=True)
class CertificateInfo:
    subject: str
    common_name: str
    issuer: str
    serial_number: str
    not_before: datetime
    not_after: datetime
    vat_number: str
    egs_serial: str
    invoice_types: str
    location: str


def cert_b64_from_token(binary_security_token):
    """CSID token -> the certificate's base64 DER text."""
    return base64.b64decode(binary_security_token).decode("ascii").strip()


def load(cert_b64):
    return x509.load_der_x509_certificate(base64.b64decode(cert_b64))


def info(cert_b64):
    cert = load(cert_b64)
    san = {}
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        for directory in names.get_values_for_type(x509.DirectoryName):
            for attribute in directory:
                san[attribute.oid] = attribute.value
    except x509.ExtensionNotFound:
        pass
    common_name = _first(cert.subject, NameOID.COMMON_NAME)
    vat = san.get(_UID) or _first_match(_VAT_IN_TEXT, common_name)
    return CertificateInfo(
        subject=cert.subject.rfc4514_string(),
        common_name=common_name,
        issuer=_first(cert.issuer, NameOID.COMMON_NAME) or cert.issuer.rfc4514_string(),
        serial_number=format(cert.serial_number, "x"),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        vat_number=vat or "",
        egs_serial=san.get(_SERIAL_NUMBER, ""),
        invoice_types=san.get(_TITLE, ""),
        location=san.get(_REGISTERED_ADDRESS, ""),
    )


def normalize_private_key(text):
    """
    Any private key the ZATCA SDK writes -> a standard PKCS#8 PEM.

    The SDK 3.0.8 labels a PKCS#8 body "BEGIN EC PRIVATE KEY" (the SEC1
    label), which strict PEM loaders refuse, and its Data/Certificates copy
    is one bare base64 line with no headers at all. Both are read by their
    DER body, whatever the label says. Raises ValueError if it's no key.
    """
    body = "".join(line.strip() for line in text.strip().splitlines() if not line.startswith("-----"))
    key = serialization.load_der_private_key(base64.b64decode(body), password=None)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode("ascii")


def key_matches_cert(private_key_pem, cert_b64):
    """True when the certificate was issued for this private key."""
    key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    public = serialization.PublicFormat.SubjectPublicKeyInfo
    return key.public_key().public_bytes(serialization.Encoding.DER, public) == load(cert_b64).public_key().public_bytes(
        serialization.Encoding.DER, public
    )


def _first(name, oid):
    values = name.get_attributes_for_oid(oid)
    return values[0].value if values else ""


def _first_match(pattern, text):
    match = pattern.search(text or "")
    return match.group(0) if match else ""
