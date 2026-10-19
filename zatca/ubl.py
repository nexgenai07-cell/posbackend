"""
Build the unsigned UBL 2.1 XML of a ZATCA invoice, credit note or debit note.

Amounts come from vat.calculate() and are written as they are: no rounding
happens here, so every check ZATCA runs on the arithmetic (price x quantity
= line amount, per-category VAT, totals, PayableRoundingAmount) holds
because it held in the VAT engine. Currency is always SAR.

The result has an empty ext:UBLExtensions and an empty QR, which
signing.sign() fills in.
"""

import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from lxml import etree

from .vat import VatCategory

NSMAP = {
    None: "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
}
CURRENCY = "SAR"
QR_PLACEHOLDER = "QR"


class DocumentKind:
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    DEBIT_NOTE = "debit_note"

    TYPE_CODES = {INVOICE: "388", CREDIT_NOTE: "381", DEBIT_NOTE: "383"}
    CHOICES = [(INVOICE, "Tax invoice"), (CREDIT_NOTE, "Credit note"), (DEBIT_NOTE, "Debit note")]


class PaymentMeans:
    """UNTDID 4461 codes ZATCA accepts (BR-KSA-16)."""

    CASH = "10"
    CREDIT = "30"
    BANK_ACCOUNT = "42"
    BANK_CARD = "48"
    NOT_DEFINED = "1"


@dataclass(frozen=True)
class Address:
    street: str
    building_no: str
    district: str
    city: str
    postal_code: str
    additional_no: str = ""
    country: str = "SA"


@dataclass(frozen=True)
class Party:
    name: str
    address: Address | None = None
    vat_number: str = ""
    other_id: str = ""
    other_id_scheme: str = "CRN"


@dataclass(frozen=True)
class Document:
    kind: str
    simplified: bool
    number: str
    issued_at: datetime
    icv: int
    pih: str
    seller: Party
    vat: object  # vat.VatResult
    names: dict  # SaleLine key -> item name
    payment_means: tuple = (PaymentMeans.CASH,)
    buyer: Party | None = None
    # Credit/debit notes: the original invoice number and the reason (KSA-10).
    billing_reference: str = ""
    reason: str = ""
    delivery_date: date | None = None
    uuid: str = field(default_factory=lambda: str(uuid_lib.uuid4()))
    # Category -> (VATEX code, reason) for zero-rated / exempt / out-of-scope lines.
    exemptions: dict = field(default_factory=dict)
    timezone: str = "Asia/Riyadh"


def build(doc):
    """The unsigned invoice as UTF-8 XML text."""
    if doc.kind not in DocumentKind.TYPE_CODES:
        raise ValueError(f"Unknown document kind {doc.kind!r}")
    if doc.kind != DocumentKind.INVOICE and not (doc.billing_reference and doc.reason):
        raise ValueError("Credit and debit notes need the original invoice number and a reason")
    if not doc.simplified and doc.buyer is None:
        raise ValueError("A standard (B2B) invoice needs the buyer")
    if not doc.vat.lines:
        raise ValueError("A document needs at least one line")

    local = doc.issued_at.astimezone(ZoneInfo(doc.timezone))
    root = etree.Element("Invoice", nsmap=NSMAP)
    _el(root, "ext:UBLExtensions")
    _el(root, "cbc:ProfileID", "reporting:1.0")
    _el(root, "cbc:ID", doc.number)
    _el(root, "cbc:UUID", doc.uuid)
    _el(root, "cbc:IssueDate", local.strftime("%Y-%m-%d"))
    _el(root, "cbc:IssueTime", local.strftime("%H:%M:%S"))
    _el(root, "cbc:InvoiceTypeCode", DocumentKind.TYPE_CODES[doc.kind], name="0200000" if doc.simplified else "0100000")
    _el(root, "cbc:DocumentCurrencyCode", CURRENCY)
    _el(root, "cbc:TaxCurrencyCode", CURRENCY)
    if doc.billing_reference:
        reference = _el(_el(root, "cac:BillingReference"), "cac:InvoiceDocumentReference")
        _el(reference, "cbc:ID", doc.billing_reference)

    icv = _el(root, "cac:AdditionalDocumentReference")
    _el(icv, "cbc:ID", "ICV")
    _el(icv, "cbc:UUID", str(doc.icv))
    pih = _el(root, "cac:AdditionalDocumentReference")
    _el(pih, "cbc:ID", "PIH")
    _el(_el(pih, "cac:Attachment"), "cbc:EmbeddedDocumentBinaryObject", doc.pih, mimeCode="text/plain")
    qr = _el(root, "cac:AdditionalDocumentReference")
    _el(qr, "cbc:ID", "QR")
    _el(_el(qr, "cac:Attachment"), "cbc:EmbeddedDocumentBinaryObject", QR_PLACEHOLDER, mimeCode="text/plain")
    signature = _el(root, "cac:Signature")
    _el(signature, "cbc:ID", "urn:oasis:names:specification:ubl:signature:Invoice")
    _el(signature, "cbc:SignatureMethod", "urn:oasis:names:specification:ubl:dsig:enveloped:xades")

    _party(_el(_el(root, "cac:AccountingSupplierParty"), "cac:Party"), doc.seller)
    customer = _el(root, "cac:AccountingCustomerParty")
    if doc.buyer is not None:
        _party(_el(customer, "cac:Party"), doc.buyer)

    _el(_el(root, "cac:Delivery"), "cbc:ActualDeliveryDate", (doc.delivery_date or local.date()).isoformat())
    for code in doc.payment_means:
        means = _el(root, "cac:PaymentMeans")
        _el(means, "cbc:PaymentMeansCode", code)
        if doc.reason:
            _el(means, "cbc:InstructionNote", doc.reason)

    vat = doc.vat
    _el(_el(root, "cac:TaxTotal"), "cbc:TaxAmount", _amount(vat.vat_total), currencyID=CURRENCY)
    tax_total = _el(root, "cac:TaxTotal")
    _el(tax_total, "cbc:TaxAmount", _amount(vat.vat_total), currencyID=CURRENCY)
    for category in vat.categories:
        subtotal = _el(tax_total, "cac:TaxSubtotal")
        _el(subtotal, "cbc:TaxableAmount", _amount(category.taxable), currencyID=CURRENCY)
        _el(subtotal, "cbc:TaxAmount", _amount(category.vat), currencyID=CURRENCY)
        tax_category = _el(subtotal, "cac:TaxCategory")
        _el(tax_category, "cbc:ID", category.category, schemeID="UN/ECE 5305", schemeAgencyID="6")
        _el(tax_category, "cbc:Percent", _amount(category.rate))
        if category.category != VatCategory.STANDARD:
            code, reason = doc.exemptions.get(category.category, ("", ""))
            if not (code and reason):
                raise ValueError(f"VAT category {category.category} needs an exemption reason code and text")
            _el(tax_category, "cbc:TaxExemptionReasonCode", code)
            _el(tax_category, "cbc:TaxExemptionReason", reason)
        _el(_el(tax_category, "cac:TaxScheme"), "cbc:ID", "VAT", schemeID="UN/ECE 5153", schemeAgencyID="6")

    totals = _el(root, "cac:LegalMonetaryTotal")
    _el(totals, "cbc:LineExtensionAmount", _amount(vat.net_total), currencyID=CURRENCY)
    _el(totals, "cbc:TaxExclusiveAmount", _amount(vat.net_total), currencyID=CURRENCY)
    _el(totals, "cbc:TaxInclusiveAmount", _amount(vat.tax_inclusive), currencyID=CURRENCY)
    _el(totals, "cbc:AllowanceTotalAmount", "0.00", currencyID=CURRENCY)
    _el(totals, "cbc:PrepaidAmount", "0.00", currencyID=CURRENCY)
    if vat.payable_rounding:
        _el(totals, "cbc:PayableRoundingAmount", _amount(vat.payable_rounding), currencyID=CURRENCY)
    _el(totals, "cbc:PayableAmount", _amount(vat.payable), currencyID=CURRENCY)

    for number, line in enumerate(vat.lines, start=1):
        if line.unit_net * line.quantity != line.net:
            raise ValueError(f"Line {number}: PriceAmount x InvoicedQuantity != LineExtensionAmount")
        invoice_line = _el(root, "cac:InvoiceLine")
        _el(invoice_line, "cbc:ID", str(number))
        _el(invoice_line, "cbc:InvoicedQuantity", str(line.quantity), unitCode="PCE")
        _el(invoice_line, "cbc:LineExtensionAmount", _amount(line.net), currencyID=CURRENCY)
        line_tax = _el(invoice_line, "cac:TaxTotal")
        _el(line_tax, "cbc:TaxAmount", _amount(line.vat), currencyID=CURRENCY)
        _el(line_tax, "cbc:RoundingAmount", _amount(line.total), currencyID=CURRENCY)
        item = _el(invoice_line, "cac:Item")
        _el(item, "cbc:Name", str(doc.names[line.key]))
        classified = _el(item, "cac:ClassifiedTaxCategory")
        _el(classified, "cbc:ID", line.category)
        _el(classified, "cbc:Percent", _amount(line.rate))
        _el(_el(classified, "cac:TaxScheme"), "cbc:ID", "VAT")
        _el(_el(invoice_line, "cac:Price"), "cbc:PriceAmount", _amount(line.unit_net), currencyID=CURRENCY)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True).decode("utf-8")


def _party(party_el, party):
    if party.other_id:
        _el(_el(party_el, "cac:PartyIdentification"), "cbc:ID", party.other_id, schemeID=party.other_id_scheme)
    if party.address is not None:
        address = party.address
        postal = _el(party_el, "cac:PostalAddress")
        _el(postal, "cbc:StreetName", address.street)
        _el(postal, "cbc:BuildingNumber", address.building_no)
        if address.additional_no:
            _el(postal, "cbc:PlotIdentification", address.additional_no)
        _el(postal, "cbc:CitySubdivisionName", address.district)
        _el(postal, "cbc:CityName", address.city)
        _el(postal, "cbc:PostalZone", address.postal_code)
        _el(_el(postal, "cac:Country"), "cbc:IdentificationCode", address.country)
    if party.vat_number:
        scheme = _el(party_el, "cac:PartyTaxScheme")
        _el(scheme, "cbc:CompanyID", party.vat_number)
        _el(_el(scheme, "cac:TaxScheme"), "cbc:ID", "VAT")
    _el(_el(party_el, "cac:PartyLegalEntity"), "cbc:RegistrationName", party.name)


def _el(parent, tag, text=None, **attributes):
    prefix, _, local = tag.rpartition(":")
    element = etree.SubElement(parent, f"{{{NSMAP[prefix or None]}}}{local}", **attributes)
    if text is not None:
        element.text = text
    return element


def _amount(value):
    return f"{Decimal(value):.2f}"
