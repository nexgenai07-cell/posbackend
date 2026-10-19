"""
The invoice hash: what ds:DigestValue (invoiceSignedData), the PIH chain and
QR tag 6 all carry.

ZATCA's signature reference applies three XPath transforms and C14N 1.1:
drop ext:UBLExtensions, cac:Signature and the QR AdditionalDocumentReference,
canonicalise what's left without comments, SHA-256 it, base64 the digest.

Two details make or break matching the SDK byte for byte:
  - removing an element keeps the whitespace that followed it (lxml would
    drop it with the element's `tail`), exactly as an XPath node-set does;
  - comments are excluded: ZATCA's own samples carry several.
C14N 1.0 (all lxml offers) equals 1.1 here: they differ only for xml:id and
xml:base attributes, which UBL invoices never use.
"""

import base64
import hashlib

from lxml import etree

NS = {
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}

_EXCLUDED = (
    "ext:UBLExtensions",
    "cac:Signature",
    "cac:AdditionalDocumentReference[cbc:ID='QR']",
)


def parse(xml):
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    return etree.fromstring(xml, etree.XMLParser(remove_blank_text=False, resolve_entities=False, no_network=True))


def canonical(xml):
    """The exact bytes ZATCA hashes."""
    root = parse(xml)
    for path in _EXCLUDED:
        for element in root.xpath(path, namespaces=NS):
            _remove_keeping_tail(element)
    return etree.tostring(root, method="c14n", with_comments=False)


def invoice_hash(xml):
    """base64(SHA-256(canonical invoice)), e.g. for ds:DigestValue and the next PIH."""
    return base64.b64encode(hashlib.sha256(canonical(xml)).digest()).decode("ascii")


def _remove_keeping_tail(element):
    parent = element.getparent()
    if element.tail:
        previous = element.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + element.tail
        else:
            parent.text = (parent.text or "") + element.tail
    parent.remove(element)
