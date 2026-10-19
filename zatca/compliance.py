"""
ZATCA compliance checks: the documents a device must get accepted by
/compliance/invoices before it may ask for its production CSID — one each
of invoice, credit note and debit note, for every invoice kind the device
issues (standard 1000, simplified 0100, both 1100).

The samples use the restaurant's own arithmetic on purpose: split lines
(3 x 10.00 -> 2 x 8.70 + 1 x 8.69; 7 x 13.99 -> 4 x 12.17 + 3 x 12.16) and a
PayableRoundingAmount, so the checks prove ZATCA accepts exactly what the
POS will send. They are signed with the compliance certificate and are not
part of the device's chain (they get their own ICV 1..n from the initial
PIH).

Sandbox only (two_vat_fallback): the seller VAT is the one inside the
compliance certificate when it differs from Business details — the case
for credentials onboarded by hand with the SDK's sample CSR.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.utils import timezone

from . import certs, ubl, vat
from .client import ZatcaClient
from .documents import Signer, seller_party
from .environments import profile
from .models import INITIAL_PIH

SAMPLE_BUYER = ubl.Party(
    name="شركة نماذج فاتورة المحدودة | Fatoora Samples LTD",
    vat_number="399999999800003",
    address=ubl.Address(
        street="صلاح الدين | Salah Al-Din", building_no="1111", additional_no="1234",
        district="المروج | Al-Murooj", city="الرياض | Riyadh", postal_code="12222",
    ),
)

# (label, lines as (name, quantity, menu price incl. VAT))
SPLIT_10 = ("split 3 x 10.00", [("برجر لحم | Beef burger", 3, "10.00")])
SPLIT_13_99 = ("split 7 x 13.99", [("بطاطس | Fries", 7, "13.99")])
ROUNDING = ("PayableRoundingAmount (1.11)", [("ماء | Water", 1, "1.11")])
MIXED = ("split 3 x 10.00 + 7 x 13.99", SPLIT_10[1] + SPLIT_13_99[1])


@dataclass
class CheckResult:
    label: str
    kind: str
    simplified: bool
    number: str
    http_status: int = 0
    status: str = ""
    reporting_status: str = ""
    clearance_status: str = ""
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    payable_rounding: Decimal = Decimal("0.00")
    lines: list = field(default_factory=list)  # (quantity, unit_net) as sent

    @property
    def passed(self):
        accepted = self.reporting_status == "REPORTED" if self.simplified else self.clearance_status == "CLEARED"
        return self.http_status in (200, 202) and self.status in ("PASS", "WARNING") and accepted


def sale(lines, rate):
    sale_lines = [vat.SaleLine(key=i, quantity=q, unit_gross=Decimal(price)) for i, (_n, q, price) in enumerate(lines)]
    return vat.calculate(sale_lines, standard_rate=rate), {i: name for i, (name, _q, _p) in enumerate(lines)}


def required_kinds(invoice_types):
    """[(simplified, kind)] the device must pass, in ZATCA's usual order."""
    profiles = []
    if invoice_types[0] == "1":
        profiles.append(False)
    if invoice_types[1] == "1":
        profiles.append(True)
    kinds = (ubl.DocumentKind.INVOICE, ubl.DocumentKind.CREDIT_NOTE, ubl.DocumentKind.DEBIT_NOTE)
    return [(simplified, kind) for simplified in profiles for kind in kinds]


def standard_checks(invoice_types):
    """The onboarding set: (label, simplified, kind, sample)."""
    samples = {ubl.DocumentKind.INVOICE: MIXED, ubl.DocumentKind.CREDIT_NOTE: ROUNDING,
               ubl.DocumentKind.DEBIT_NOTE: SPLIT_10}
    return [(f"{'Simplified' if s else 'Standard'} {kind.replace('_', ' ')} — {samples[kind][0]}", s, kind, samples[kind])
            for s, kind in required_kinds(invoice_types)]


def run(device, settings, checks, *, client=None):
    """Sign and send each check with the device's compliance credentials."""
    client = client or ZatcaClient(device.environment)
    signer = Signer.for_device(device, compliance=True)
    auth = device.compliance_auth()
    seller_vat = settings.vat_number
    if profile(device.environment).two_vat_fallback:
        seller_vat = certs.info(device.compliance_cert).vat_number or seller_vat
    seller = seller_party(settings, vat_number=seller_vat)

    results, pih = [], INITIAL_PIH
    for icv, (label, simplified, kind, (_sample_label, lines)) in enumerate(checks, start=1):
        totals, names = sale(lines, settings.default_vat_rate)
        number = f"CMP-{device.pk}-{icv:03d}"
        doc = ubl.Document(
            kind=kind, simplified=simplified, number=number, issued_at=timezone.now(), icv=icv, pih=pih,
            seller=seller, vat=totals, names=names, buyer=None if simplified else SAMPLE_BUYER,
            billing_reference=f"CMP-{device.pk}-001" if kind != ubl.DocumentKind.INVOICE else "",
            reason="Cancellation" if kind != ubl.DocumentKind.INVOICE else "", timezone=settings.timezone,
        )
        signed = signer.sign(ubl.build(doc))
        response = client.compliance_check(signed, doc.uuid, auth)
        results.append(CheckResult(
            label=label, kind=kind, simplified=simplified, number=number,
            http_status=response.http_status, status=response.status,
            reporting_status=response.reporting_status, clearance_status=response.clearance_status,
            warnings=response.warnings, errors=response.errors, payable_rounding=totals.payable_rounding,
            lines=[(line.quantity, line.unit_net) for line in totals.lines],
        ))
        pih = signed.invoice_hash
    return results
