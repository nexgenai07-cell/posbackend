"""
VAT dashboard M1: every ZATCA document stores its net and per-category VAT
split, exactly as its signed XML states them, and the 0007 migration fills
them in for documents issued before.
"""

import contextlib
import importlib
import io
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.apps import apps
from django.test import SimpleTestCase, TestCase, override_settings

from branches.models import Branch

from . import breakdown, compliance, documents, ubl
from .client import ZatcaClient
from .environments import Environment
from .models import ZatcaDevice, ZatcaDocument, ZatcaSettings
from .onboarding import onboard
from .tests import TEST_KEY, FakeZatca
from .vat import SaleLine, VatCategory, calculate

D = Decimal
backfill_migration = importlib.import_module("zatca.migrations.0007_backfill_vat_breakdown")

SELLER = ubl.Party(
    name="Burger House", vat_number="399999999900003", other_id="1010010000",
    address=ubl.Address(street="King Fahd Road", building_no="2322", district="Al-Olaya", city="Riyadh",
                        postal_code="12222", additional_no="1234"),
)


def unsigned_xml(vat, *, exemptions=None):
    names = {line.key: f"Item {line.key}" for line in vat.lines}
    return ubl.build(ubl.Document(
        kind=ubl.DocumentKind.INVOICE, simplified=True, number="INV-T-1", icv=1, pih="x",
        issued_at=datetime(2026, 10, 1, 9, 0, tzinfo=dt_timezone.utc), seller=SELLER, vat=vat, names=names,
        exemptions=exemptions or {},
    ))


class BreakdownFromXmlTests(SimpleTestCase):
    def test_standard_rated(self):
        vat = calculate([SaleLine(key=1, quantity=3, unit_gross=D("10.00"))], standard_rate=D("15"))
        net_total, rows = breakdown.from_xml(unsigned_xml(vat))
        self.assertEqual(net_total, D("26.09"))
        self.assertEqual(rows, [{"category": "S", "rate": "15.00", "taxable": "26.09", "vat": "3.91"}])
        self.assertEqual(breakdown.from_vat_result(vat), (net_total, rows))

    def test_standard_and_zero_rated_are_kept_apart(self):
        vat = calculate([
            SaleLine(key=1, quantity=1, unit_gross=D("11.50")),
            SaleLine(key=2, quantity=2, unit_gross=D("5.00"), category=VatCategory.ZERO),
        ], standard_rate=D("15"))
        xml = unsigned_xml(vat, exemptions={VatCategory.ZERO: ("VATEX-SA-32", "Export of goods")})
        net_total, rows = breakdown.from_xml(xml)
        self.assertEqual(net_total, D("20.00"))
        self.assertEqual(rows, [
            {"category": "S", "rate": "15.00", "taxable": "10.00", "vat": "1.50"},
            {"category": "Z", "rate": "0.00", "taxable": "10.00", "vat": "0.00"},
        ])
        self.assertEqual(breakdown.vat_of(rows), D("1.50"))

    def test_unreadable_xml_raises(self):
        with self.assertRaises(breakdown.BreakdownError):
            breakdown.from_xml("<not xml")
        vat = calculate([SaleLine(key=1, quantity=1, unit_gross=D("11.50"))], standard_rate=D("15"))
        without_subtotals = unsigned_xml(vat).replace("TaxSubtotal", "Removed")
        with self.assertRaisesMessage(breakdown.BreakdownError, "TaxSubtotal is missing"):
            breakdown.from_xml(without_subtotals)


@override_settings(ZATCA_ENCRYPTION_KEY=TEST_KEY)
class StoredBreakdownTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Burger House", name_ar="بيت البرجر")
        ZatcaSettings.for_branch(self.branch)
        client = ZatcaClient(Environment.SANDBOX, session=FakeZatca(sandbox_demo_cert=True))
        onboard(self.branch, otp="", client=client, activate=True)
        self.device = ZatcaDevice.objects.get(is_active=True)

    def issue(self, sample=compliance.SPLIT_10, simplified=True, **kwargs):
        totals, names = compliance.sale(sample[1], D("15.00"))
        return documents.issue(self.branch, kind=kwargs.pop("kind", ubl.DocumentKind.INVOICE), simplified=simplified,
                               vat=totals, names=names, buyer=None if simplified else compliance.SAMPLE_BUYER, **kwargs)

    def assert_matches_its_xml(self, document):
        document.refresh_from_db()
        net_total, rows = breakdown.from_xml(document.signed_xml)
        self.assertEqual(document.net_total, net_total)
        self.assertEqual(document.vat_breakdown, rows)
        self.assertEqual(breakdown.vat_of(document.vat_breakdown), document.vat_total)

    def test_issue_stores_the_split_the_xml_states(self):
        invoice = self.issue()
        self.assertEqual((invoice.net_total, invoice.vat_total, invoice.total), (D("26.09"), D("3.91"), D("30.00")))
        self.assert_matches_its_xml(invoice)
        self.assert_matches_its_xml(self.issue(compliance.MIXED, simplified=False))

    def test_rounding_is_not_part_of_the_net(self):
        document = self.issue(compliance.ROUNDING)
        # 1.11 charged: net + VAT is one halala off, PayableRoundingAmount holds it.
        self.assertEqual(document.total, D("1.11"))
        self.assertNotEqual(document.net_total + document.vat_total, document.total)
        self.assert_matches_its_xml(document)

    def test_credit_note_is_stored_positive(self):
        invoice = self.issue()
        note = self.issue(kind=ubl.DocumentKind.CREDIT_NOTE, original=invoice, reason="Order cancelled")
        self.assertEqual((note.net_total, note.vat_total), (D("26.09"), D("3.91")))
        self.assert_matches_its_xml(note)

    def run_backfill(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            backfill_migration.backfill(apps, None)
        return out.getvalue()

    def test_backfill_fills_old_documents_from_their_xml(self):
        old, other = self.issue(), self.issue(compliance.SPLIT_13_99, simplified=False)
        ZatcaDocument.objects.update(net_total=None, vat_breakdown=None)

        output = self.run_backfill()

        self.assertIn("2 documents filled, 0 left empty", output)
        self.assert_matches_its_xml(old)
        self.assert_matches_its_xml(other)
        # Nothing left to do the second time.
        self.assertIn("0 documents filled, 0 left empty", self.run_backfill())

    def test_backfill_leaves_unreadable_or_mismatched_documents_empty(self):
        good, mismatched, broken = self.issue(), self.issue(), self.issue()
        ZatcaDocument.objects.update(net_total=None, vat_breakdown=None)
        ZatcaDocument.objects.filter(pk=mismatched.pk).update(vat_total=D("9.99"))
        ZatcaDocument.objects.filter(pk=broken.pk).update(signed_xml="<broken")

        output = self.run_backfill()

        self.assertIn("1 documents filled, 2 left empty", output)
        self.assertIn(f"{mismatched.number}: XML VAT 3.91 != stored vat_total 9.99", output)
        self.assertIn(f"{broken.number}: XML can't be parsed", output)
        self.assert_matches_its_xml(good)
        for document in (mismatched, broken):
            document.refresh_from_db()
            self.assertIsNone(document.net_total)
            self.assertIsNone(document.vat_breakdown)
