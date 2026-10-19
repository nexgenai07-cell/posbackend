"""
Pure-Python XAdES signing of a UBL invoice, as ZATCA's SDK does it.

  1. invoice hash = hashing.invoice_hash(xml)
  2. SignatureValue = ECDSA-SHA256 over the 32 hash bytes (not over
     SignedInfo: a ZATCA particularity the SDK and the API both expect)
  3. CertDigest = base64(hex(SHA-256(certificate base64 text)))
  4. SignedProperties digest = base64(hex(SHA-256(the SignedProperties
     element as written, with xmlns:xades on it and xmlns:ds on every ds:
     element))) — reproduced from the SDK, which ZATCA validates against
  5. the QR, read back from the signed document

The UBLExtensions block keeps the SDK's exact layout. Everything else in
the document is left byte for byte as given, so the hash computed in step 1
is still the hash of the signed result (checked before returning).

Reference: mabaega/ZatcaPython utilities/einvoice_signer.py; verified
against the SDK 3.0.8's INVOICE HASH on ZATCA's six samples.
"""

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import certs, qr
from .hashing import invoice_hash

XADES_NS = "http://uri.etsi.org/01903/v1.3.2#"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"

_SIGNED_PROPERTIES = (
    '<xades:SignedProperties Id="xadesSignedProperties">\n'
    "                                    <xades:SignedSignatureProperties>\n"
    "                                        <xades:SigningTime>{signing_time}</xades:SigningTime>\n"
    "                                        <xades:SigningCertificate>\n"
    "                                            <xades:Cert>\n"
    "                                                <xades:CertDigest>\n"
    '                                                    <ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>\n'
    "                                                    <ds:DigestValue>{cert_digest}</ds:DigestValue>\n"
    "                                                </xades:CertDigest>\n"
    "                                                <xades:IssuerSerial>\n"
    "                                                    <ds:X509IssuerName>{issuer}</ds:X509IssuerName>\n"
    "                                                    <ds:X509SerialNumber>{serial}</ds:X509SerialNumber>\n"
    "                                                </xades:IssuerSerial>\n"
    "                                            </xades:Cert>\n"
    "                                        </xades:SigningCertificate>\n"
    "                                    </xades:SignedSignatureProperties>\n"
    "                                </xades:SignedProperties>"
)

_UBL_EXTENSIONS = (
    "<ext:UBLExtensions>\n"
    "    <ext:UBLExtension>\n"
    "        <ext:ExtensionURI>urn:oasis:names:specification:ubl:dsig:enveloped:xades</ext:ExtensionURI>\n"
    "        <ext:ExtensionContent>\n"
    '            <sig:UBLDocumentSignatures xmlns:sig="urn:oasis:names:specification:ubl:schema:xsd:CommonSignatureComponents-2"'
    ' xmlns:sac="urn:oasis:names:specification:ubl:schema:xsd:SignatureAggregateComponents-2"'
    ' xmlns:sbc="urn:oasis:names:specification:ubl:schema:xsd:SignatureBasicComponents-2">\n'
    "                <sac:SignatureInformation>\n"
    "                    <cbc:ID>urn:oasis:names:specification:ubl:signature:1</cbc:ID>\n"
    "                    <sbc:ReferencedSignatureID>urn:oasis:names:specification:ubl:signature:Invoice</sbc:ReferencedSignatureID>\n"
    '                    <ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#" Id="signature">\n'
    "                        <ds:SignedInfo>\n"
    '                            <ds:CanonicalizationMethod Algorithm="http://www.w3.org/2006/12/xml-c14n11"/>\n'
    '                            <ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256"/>\n'
    '                            <ds:Reference Id="invoiceSignedData" URI="">\n'
    "                                <ds:Transforms>\n"
    '                                    <ds:Transform Algorithm="http://www.w3.org/TR/1999/REC-xpath-19991116">\n'
    "                                        <ds:XPath>not(//ancestor-or-self::ext:UBLExtensions)</ds:XPath>\n"
    "                                    </ds:Transform>\n"
    '                                    <ds:Transform Algorithm="http://www.w3.org/TR/1999/REC-xpath-19991116">\n'
    "                                        <ds:XPath>not(//ancestor-or-self::cac:Signature)</ds:XPath>\n"
    "                                    </ds:Transform>\n"
    '                                    <ds:Transform Algorithm="http://www.w3.org/TR/1999/REC-xpath-19991116">\n'
    "                                        <ds:XPath>not(//ancestor-or-self::cac:AdditionalDocumentReference[cbc:ID='QR'])</ds:XPath>\n"
    "                                    </ds:Transform>\n"
    '                                    <ds:Transform Algorithm="http://www.w3.org/2006/12/xml-c14n11"/>\n'
    "                                </ds:Transforms>\n"
    '                                <ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>\n'
    "                                <ds:DigestValue>{invoice_hash}</ds:DigestValue>\n"
    "                            </ds:Reference>\n"
    '                            <ds:Reference Type="http://www.w3.org/2000/09/xmldsig#SignatureProperties" URI="#xadesSignedProperties">\n'
    '                                <ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>\n'
    "                                <ds:DigestValue>{properties_digest}</ds:DigestValue>\n"
    "                            </ds:Reference>\n"
    "                        </ds:SignedInfo>\n"
    "                        <ds:SignatureValue>{signature}</ds:SignatureValue>\n"
    "                        <ds:KeyInfo>\n"
    "                            <ds:X509Data>\n"
    "                                <ds:X509Certificate>{certificate}</ds:X509Certificate>\n"
    "                            </ds:X509Data>\n"
    "                        </ds:KeyInfo>\n"
    "                        <ds:Object>\n"
    '                            <xades:QualifyingProperties xmlns:xades="http://uri.etsi.org/01903/v1.3.2#" Target="signature">\n'
    "                                {signed_properties}\n"
    "                            </xades:QualifyingProperties>\n"
    "                        </ds:Object>\n"
    "                    </ds:Signature>\n"
    "                </sac:SignatureInformation>\n"
    "            </sig:UBLDocumentSignatures>\n"
    "        </ext:ExtensionContent>\n"
    "    </ext:UBLExtension>\n"
    "</ext:UBLExtensions>"
)

_EXTENSIONS_RE = re.compile(r"<ext:UBLExtensions\s*/>|<ext:UBLExtensions>.*?</ext:UBLExtensions>", re.S)
_QR_RE = re.compile(
    r"(<cbc:ID>QR</cbc:ID>\s*<cac:Attachment>\s*<cbc:EmbeddedDocumentBinaryObject[^>]*>)[^<]*(</cbc:EmbeddedDocumentBinaryObject>)"
)


class SigningError(ValueError):
    pass


@dataclass(frozen=True)
class SignedInvoice:
    xml: str
    invoice_hash: str
    signature: str
    qr: str


def sign(xml, *, certificate, private_key_pem, signing_time=None):
    """
    Sign an unsigned (or previously signed) UBL invoice.

    `xml` must already contain an ext:UBLExtensions element (empty is fine)
    and the QR AdditionalDocumentReference; both are filled in here.
    `certificate` is the base64 DER text of the signing certificate.
    """
    if isinstance(xml, bytes):
        xml = xml.decode("utf-8")
    if not _EXTENSIONS_RE.search(xml):
        raise SigningError("The invoice has no ext:UBLExtensions element to put the signature in")
    if not _QR_RE.search(xml):
        raise SigningError("The invoice has no QR AdditionalDocumentReference")

    digest = invoice_hash(xml)
    key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise SigningError("The device key is not an EC key")
    signature = base64.b64encode(key.sign(base64.b64decode(digest), ec.ECDSA(hashes.SHA256()))).decode("ascii")

    cert = certs.load(certificate)
    signed_properties = _SIGNED_PROPERTIES.format(
        signing_time=(signing_time or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        cert_digest=hex_digest_b64(certificate.encode("ascii")),
        issuer=issuer_name(cert),
        serial=cert.serial_number,
    )
    block = _UBL_EXTENSIONS.format(
        invoice_hash=digest,
        properties_digest=signed_properties_digest(signed_properties),
        signature=signature,
        certificate=certificate,
        signed_properties=signed_properties,
    )
    signed = _EXTENSIONS_RE.sub(lambda _m: block, xml, count=1)
    code = qr.from_signed_xml(signed)
    signed = _QR_RE.sub(lambda m: m.group(1) + code + m.group(2), signed, count=1)

    if invoice_hash(signed) != digest:
        raise SigningError("Signing changed the invoice content")  # a bug here, never bad input
    return SignedInvoice(xml=signed, invoice_hash=digest, signature=signature, qr=code)


def hex_digest_b64(data):
    """base64 of the hex SHA-256 — ZATCA's form for CertDigest and SignedProperties."""
    return base64.b64encode(hashlib.sha256(data).hexdigest().encode("ascii")).decode("ascii")


def signed_properties_digest(signed_properties):
    with_namespaces = signed_properties.replace(
        "<xades:SignedProperties ", f'<xades:SignedProperties xmlns:xades="{XADES_NS}" ', 1,
    )
    with_namespaces = re.sub(r"<ds:(\w+)", lambda m: f'<ds:{m.group(1)} xmlns:ds="{DS_NS}"', with_namespaces)
    return hex_digest_b64(with_namespaces.encode("utf-8"))


def issuer_name(cert):
    """Issuer DN most-specific first, as the SDK writes it, e.g.
    'CN=PRZEINVOICESCA4-CA, DC=extgazt, DC=gov, DC=local'."""
    return ", ".join(rdn.rfc4514_string() for rdn in reversed(cert.issuer.rdns))


def verify(xml):
    """True when the document's SignatureValue verifies against its own
    certificate over its own invoice hash. For tests and the SDK check."""
    from .hashing import NS, parse

    root = parse(xml)
    digest = root.find(".//ds:Reference[@Id='invoiceSignedData']/ds:DigestValue", NS).text.strip()
    if digest != invoice_hash(xml):
        return False
    signature = base64.b64decode("".join(root.find(".//ds:SignatureValue", NS).text.split()))
    cert = certs.load("".join(root.find(".//ds:X509Certificate", NS).text.split()))
    try:
        cert.public_key().verify(signature, base64.b64decode(digest), ec.ECDSA(hashes.SHA256()))
    except Exception:  # InvalidSignature, or a malformed DER signature
        return False
    return True
