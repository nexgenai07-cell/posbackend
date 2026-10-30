"""
Input VAT on purchases: checking a supplier's tax-invoice QR and adding up
what can be claimed. Nothing here talks to ZATCA — the supplier reports its
own invoices; we only record and check them.

QR check. A supplier's QR (pasted, or typed by a USB scanner) is base64 TLV:
1 seller name, 2 VAT number, 3 timestamp, 4 total incl. VAT, 5 VAT, and on
Phase 2 invoices 6-9 hash/signature/key. We decode the first five and
compare them with what was keyed in and with the supplier's saved VAT
number. A mismatch is a warning for a person, never a block: the paper
invoice is what counts. Verifying the signature needs the supplier's XML
and isn't done (yet).
"""

import base64
import binascii
from datetime import datetime
from decimal import Decimal, InvalidOperation

TOLERANCE = Decimal("0.01")

# Codes the admin translates (purchasing.vatCheck.*).
VAT_NUMBER = "vatNumber"
TOTAL = "total"
VAT = "vat"
DATE = "date"
SUPPLIER_VAT_MISSING = "supplierVatMissing"


def decode_qr(text):
    """{tag: str} from a supplier QR, or None if it isn't a ZATCA QR."""
    compact = "".join((text or "").split())
    if not compact:
        return None
    try:
        data = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None
    fields, i = {}, 0
    while i < len(data):
        if i + 2 > len(data):
            return None
        tag, length = data[i], data[i + 1]
        if i + 2 + length > len(data):
            return None
        value = data[i + 2:i + 2 + length]
        if tag <= 5:
            try:
                fields[tag] = value.decode("utf-8")
            except UnicodeDecodeError:
                return None
        else:
            fields[tag] = ""  # binary (hash, signature, key): present, not shown
        i += 2 + length
    return fields if all(tag in fields for tag in (1, 2, 3, 4, 5)) else None


def _amount(value):
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _date_of(timestamp):
    """The date part of tag 3 (ISO 8601, with or without a time/zone)."""
    text = (timestamp or "").strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            continue
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def check_qr(text, *, supplier_vat="", issue_date=None, total=None, vat=None):
    """What a supplier QR says and where it disagrees with the entered values.

    Returns {"present", "readable", "phase", "fields", "mismatches"}; an
    empty QR is simply not present (no warning by itself — the report flags
    a registered supplier's invoice without one).
    """
    if not (text or "").strip():
        return {"present": False, "readable": False, "phase": None, "fields": {}, "mismatches": []}
    fields = decode_qr(text)
    if fields is None:
        return {"present": True, "readable": False, "phase": None, "fields": {}, "mismatches": []}
    decoded = {
        "seller_name": fields[1], "vat_number": fields[2], "timestamp": fields[3],
        "total": fields[4], "vat": fields[5],
    }
    mismatches = []
    if not supplier_vat:
        mismatches.append(SUPPLIER_VAT_MISSING)
    elif decoded["vat_number"].strip() != supplier_vat.strip():
        mismatches.append(VAT_NUMBER)
    for code, entered, in_qr in ((TOTAL, total, decoded["total"]), (VAT, vat, decoded["vat"])):
        qr_amount = _amount(in_qr)
        if entered is not None and (qr_amount is None or abs(Decimal(entered) - qr_amount) > TOLERANCE):
            mismatches.append(code)
    qr_date = _date_of(decoded["timestamp"])
    if issue_date is not None and qr_date != issue_date:
        mismatches.append(DATE)
    return {"present": True, "readable": True, "phase": 2 if 6 in fields else 1, "fields": decoded,
            "mismatches": mismatches}


# -- input VAT report ------------------------------------------------------------

# Flags per purchase (purchasing.inputVat.flags.*).
NO_INVOICE = "noInvoice"
NO_QR = "noQr"
QR_UNREADABLE = "qrUnreadable"
QR_MISMATCH = "qrMismatch"
NOT_REGISTERED = "notRegistered"
SUPPLIER_VAT = "supplierVatMissing"
TOTALS = "totalsDontAdd"
VAT_NOT_STANDARD = "vatNot15"
NET_VS_PO = "netDiffersFromPurchase"
# Business expenses only (purchasing/expenses.py).
NOT_CLAIMABLE = "notClaimable"
NO_ATTACHMENT = "noAttachment"
POSSIBLE_DUPLICATE = "possibleDuplicate"

# The flags that hold a document's VAT back from the claim: it goes to the
# "VAT needing review" card instead, until the supplier's details are fixed.
REVIEW_FLAGS = (NOT_REGISTERED, SUPPLIER_VAT)

# For the CSV, which is read outside the app (by the accountant), in English.
FLAG_LABELS = {
    NO_INVOICE: "No supplier invoice",
    NO_QR: "No QR",
    QR_UNREADABLE: "QR unreadable",
    QR_MISMATCH: "QR doesn't match",
    NOT_REGISTERED: "Supplier not VAT-registered",
    SUPPLIER_VAT: "Supplier VAT number missing",
    TOTALS: "Net + VAT doesn't equal total",
    VAT_NOT_STANDARD: "VAT not 15%",
    NET_VS_PO: "Net differs from purchase order",
    NOT_CLAIMABLE: "Not claimable",
    NO_ATTACHMENT: "No attachment",
    POSSIBLE_DUPLICATE: "Possible duplicate",
}

STANDARD_RATE = Decimal("15")
# "VAT not 15%" warns when the VAT is further than this from 15% of the net.
RATE_TOLERANCE = Decimal("0.05")
_CENT = Decimal("0.01")


def is_claimable(document):
    """A document's VAT can be claimed only from a supplier that was
    VAT-registered with a VAT number when the document was saved (the
    snapshot on the document, not the supplier as it is now)."""
    return document.supplier_vat_registered and bool(document.supplier_vat_number)


def claimable_vat(document):
    """Input VAT this document lets the restaurant claim (negative for a
    credit note): none unless is_claimable(), and none for an expense
    marked "Not claimable" or voided (its VAT stays recorded)."""
    if not is_claimable(document) or getattr(document, "not_claimable", False) or getattr(document, "voided", False):
        return Decimal("0.00")
    return -document.vat if document.kind == "credit_note" else document.vat


def vat_rate(document):
    """VAT as a % of net, 2 decimals; None when the net is 0."""
    if not document.net:
        return None
    return (document.vat / document.net * 100).quantize(_CENT)


def document_flags(document, purchase_subtotal=None):
    """What a person should look at on a supplier document, from its stored
    figures and supplier snapshot. `purchase_subtotal` (quantity x unit cost
    of the purchase's lines) adds the net-vs-PO check on an invoice."""
    flags = []
    if abs(document.net + document.vat - document.total) > TOLERANCE:
        flags.append(TOTALS)
    check = document.qr_check or {}
    if document.supplier_vat_registered and document.kind == "invoice":
        if not check.get("present"):
            flags.append(NO_QR)
        elif not check.get("readable"):
            flags.append(QR_UNREADABLE)
        elif check.get("mismatches"):
            flags.append(QR_MISMATCH)
    if not document.supplier_vat_registered:
        flags.append(NOT_REGISTERED)
    elif not document.supplier_vat_number:
        flags.append(SUPPLIER_VAT)
    # Zero-rated and exempt documents carry no VAT by design.
    if document.vat_category == "standard":
        expected = (document.net * STANDARD_RATE / 100).quantize(_CENT)
        if abs(document.vat - expected) > RATE_TOLERANCE:
            flags.append(VAT_NOT_STANDARD)
    if document.kind == "invoice" and purchase_subtotal is not None:
        if abs(purchase_subtotal - document.net) > Decimal("0.05"):
            flags.append(NET_VS_PO)
    return flags
