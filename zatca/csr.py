"""
A device's key pair and CSR, generated on the server: the private key never
exists as a file. Same structure as the SDK's -csr output (checked against
the CSR it made for the imported sandbox device):

  subject  C=SA, OU=<org unit>, O=<seller name>, CN=<EGS common name>
  1.3.6.1.4.1.311.20.2   certificate template name (UTF8String), per environment
  SAN dirName            SN=1-<solution>|2-<model>|3-<uuid>, UID=<VAT>,
                         title=<invoice types>, registeredAddress, businessCategory
"""

import uuid
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier

from .environments import profile

_TEMPLATE_OID = ObjectIdentifier("1.3.6.1.4.1.311.20.2")
_UID = ObjectIdentifier("0.9.2342.19200300.100.1.1")
_REGISTERED_ADDRESS = ObjectIdentifier("2.5.4.26")
_BUSINESS_CATEGORY = ObjectIdentifier("2.5.4.15")

SOLUTION_NAME = "NexgenPOS"
SOLUTION_VERSION = "1.0"


@dataclass(frozen=True)
class DeviceRequest:
    private_key_pem: str
    csr_pem: str
    common_name: str
    egs_serial: str
    location: str


def generate(settings, environment, device_label):
    """New secp256k1 key + CSR for `settings` (a ZatcaSettings) in `environment`."""
    key = ec.generate_private_key(ec.SECP256K1())
    egs_serial = f"1-{SOLUTION_NAME}|2-{SOLUTION_VERSION}|3-{uuid.uuid4()}"
    common_name = f"EGS-{device_label}-{settings.vat_number}"
    location = f"{settings.building_no} {settings.street}, {settings.city}"[:100]
    template = profile(environment).csr_template.encode("utf-8")

    builder = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, settings.country or "SA"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, settings.org_unit[:64]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, (settings.seller_name_en or settings.seller_name_ar)[:64]),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64]),
        ]))
        .add_extension(x509.UnrecognizedExtension(_TEMPLATE_OID, b"\x0c" + bytes([len(template)]) + template),
                       critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DirectoryName(x509.Name([
            x509.NameAttribute(NameOID.SURNAME, egs_serial),
            x509.NameAttribute(_UID, settings.vat_number),
            x509.NameAttribute(NameOID.TITLE, settings.invoice_types),
            x509.NameAttribute(_REGISTERED_ADDRESS, location),
            x509.NameAttribute(_BUSINESS_CATEGORY, settings.business_category[:64] or "Restaurant"),
        ]))]), critical=False)
    )
    csr = builder.sign(key, hashes.SHA256())
    return DeviceRequest(
        private_key_pem=key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ).decode("ascii"),
        csr_pem=csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        common_name=common_name[:64],
        egs_serial=egs_serial,
        location=location,
    )
