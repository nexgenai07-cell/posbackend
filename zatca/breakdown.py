"""
The stored VAT split of a ZATCA document: its net (TaxExclusiveAmount) and,
per VAT category, the taxable amount and VAT — the same numbers the signed
XML states in cac:TaxTotal/cac:TaxSubtotal. The VAT return adds these up;
it never recalculates VAT from prices.

Shape of `ZatcaDocument.vat_breakdown` (amounts as 2-dp strings, positive
for every kind; a credit note is subtracted by whoever adds them up):

    [{"category": "S", "rate": "15.00", "taxable": "26.09", "vat": "3.91"}, ...]

`from_xml` reads an already signed document. The 0006 backfill migration
imports it, so keep it working for every XML this app has ever written.
"""

from decimal import Decimal, InvalidOperation

from lxml import etree

_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
_NS = {"cac": _CAC, "cbc": _CBC}
_CENT = Decimal("0.01")


class BreakdownError(ValueError):
    """The XML doesn't carry a readable VAT split."""


def _decimal(value):
    return Decimal(value).quantize(_CENT)


def _money(value):
    return str(_decimal(value))


def from_vat_result(vat):
    """(net_total as Decimal, breakdown) from a vat.VatResult, as documents.issue() writes it."""
    return _decimal(vat.net_total), [
        {"category": c.category, "rate": _money(c.rate), "taxable": _money(c.taxable), "vat": _money(c.vat)}
        for c in vat.categories
    ]


def from_xml(xml):
    """(net_total as Decimal, breakdown) as stated in a document's XML.

    Raises BreakdownError if the XML can't be parsed or is missing
    TaxExclusiveAmount or any TaxSubtotal figure."""
    try:
        root = etree.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise BreakdownError(f"XML can't be parsed: {exc}") from exc

    def amount(element, path):
        found = element.find(path, _NS)
        if found is None or not (found.text or "").strip():
            raise BreakdownError(f"{path} is missing")
        try:
            return _money(found.text.strip())
        except InvalidOperation as exc:
            raise BreakdownError(f"{path} is not an amount: {found.text!r}") from exc

    net_total = Decimal(amount(root, "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount"))
    # The document has two cac:TaxTotal elements; only one carries subtotals.
    subtotals = root.findall("cac:TaxTotal/cac:TaxSubtotal", _NS)
    if not subtotals:
        raise BreakdownError("cac:TaxSubtotal is missing")
    breakdown = []
    for subtotal in subtotals:
        category = subtotal.find("cac:TaxCategory/cbc:ID", _NS)
        if category is None or not (category.text or "").strip():
            raise BreakdownError("cac:TaxCategory/cbc:ID is missing")
        breakdown.append({
            "category": category.text.strip(),
            "rate": amount(subtotal, "cac:TaxCategory/cbc:Percent"),
            "taxable": amount(subtotal, "cbc:TaxableAmount"),
            "vat": amount(subtotal, "cbc:TaxAmount"),
        })
    return net_total, breakdown


def vat_of(breakdown):
    """Sum of the breakdown's VAT, for checking it against vat_total."""
    return sum((Decimal(row["vat"]) for row in breakdown), Decimal("0.00"))
