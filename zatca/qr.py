"""
The Phase 2 QR code: base64 of TLV fields read back from the signed invoice.

  1 seller name          5 VAT total
  2 seller VAT number    6 invoice hash
  3 issue timestamp      7 ECDSA signature
  4 amount due           8 public key (DER SubjectPublicKeyInfo)
                         9 the certificate's own signature (simplified only)

Read from the XML rather than passed in, so the QR can only ever say what
the signed document says. Standard invoices print the QR from ZATCA's
cleared XML instead; theirs is kept for completeness.
"""

import base64

from cryptography.hazmat.primitives import serialization

from . import certs
from .hashing import NS, parse


class QrError(ValueError):
    pass


def tlv(tag, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    if len(value) > 255:
        raise QrError(f"QR field {tag} is {len(value)} bytes; a QR field holds at most 255")
    return bytes([tag, len(value)]) + value


def from_signed_xml(xml):
    root = parse(xml)

    def text(path):
        element = root.find(path, NS)
        if element is None or element.text is None:
            raise QrError(f"The invoice has no {path}")
        return element.text.strip()

    currency = text("cbc:DocumentCurrencyCode")
    vat_total = next(
        (e.text.strip() for e in root.findall("cac:TaxTotal/cbc:TaxAmount", NS) if e.get("currencyID") == currency),
        None,
    )
    if vat_total is None:
        raise QrError("The invoice has no VAT total in its own currency")
    signed = root.find(".//ds:Reference[@Id='invoiceSignedData']/ds:DigestValue", NS)
    signature = root.find(".//ds:SignatureValue", NS)
    certificate = root.find(".//ds:X509Certificate", NS)
    if signed is None or signature is None or certificate is None:
        raise QrError("The invoice isn't signed")

    cert = certs.load("".join(certificate.text.split()))
    public_key = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    data = (
        tlv(1, text("cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName"))
        + tlv(2, text("cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID"))
        + tlv(3, text("cbc:IssueDate") + "T" + text("cbc:IssueTime"))
        # Amount due (BT-115): differs from TaxInclusiveAmount by any
        # PayableRoundingAmount, and ZATCA warns if tag 4 isn't it.
        + tlv(4, text("cac:LegalMonetaryTotal/cbc:PayableAmount"))
        + tlv(5, vat_total)
        + tlv(6, signed.text.strip())
        + tlv(7, "".join(signature.text.split()))
        + tlv(8, public_key)
    )
    if root.find("cbc:InvoiceTypeCode", NS).get("name", "").startswith("02"):
        data += tlv(9, cert.signature)
    return base64.b64encode(data).decode("ascii")


def embedded(xml):
    """The QR value already inside an invoice, or ''."""
    element = parse(xml).find(
        "cac:AdditionalDocumentReference[cbc:ID='QR']/cac:Attachment/cbc:EmbeddedDocumentBinaryObject", NS,
    )
    return "".join((element.text or "").split()) if element is not None else ""


def decode(value):
    """{tag: bytes} — for tests and the receipt preview."""
    data = base64.b64decode(value)
    fields, i = {}, 0
    while i < len(data):
        tag, length = data[i], data[i + 1]
        fields[tag] = data[i + 2 : i + 2 + length]
        i += 2 + length
    return fields
