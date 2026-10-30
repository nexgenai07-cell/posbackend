"""VAT dashboard M4: the cards, return boxes and "Show documents" lists,
from stored figures only; and the ZATCA XML download."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import StaffRole
from branches.models import Branch
from purchasing.models import Purchase, Supplier, SupplierDocument
from zatca.models import ZatcaDevice, ZatcaDocument, ZatcaSettings
from zatca.tests import make_staff

from . import figures
from .models import VatPeriod

D = Decimal
TODAY = date(2026, 10, 29)
RIYADH = ZoneInfo("Asia/Riyadh")


def riyadh(*args):
    return datetime(*args, tzinfo=RIYADH)


def line(category, taxable, vat, rate="15.00"):
    return {"category": category, "rate": rate, "taxable": taxable, "vat": vat}


class VatFixture:
    """A branch tracking VAT returns from 1 Jul 2026, "today" 29 Oct 2026
    (Q3 ended, Q4 current), with helpers to add sales and supplier
    documents. Shared with test_filing.py."""

    def setUp(self):
        patcher = mock.patch("vat_returns.periods.today", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.branch = Branch.objects.create(name_en="Burger House", name_ar="بيت البرجر")
        self.settings = ZatcaSettings.for_branch(self.branch)
        self.owner = make_staff(self.branch, "Owner", StaffRole.OWNER)
        self.manager = make_staff(self.branch, "Manager", StaffRole.MANAGER)
        self.cashier = make_staff(self.branch, "Cashier", StaffRole.CASHIER)
        self.device = ZatcaDevice.objects.create(branch=self.branch, environment="sandbox")
        self.supplier = Supplier.objects.create(name_en="Fresh Meat Co", name_ar="لحوم", vat_number="300000000000003")
        self.no_vat_number = Supplier.objects.create(name_en="Corner Shop", name_ar="بقالة", vat_number="")
        self.icv = 0
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.api.patch("/api/vat/settings/", {"tracking_start": "2026-07-01"}, format="json")
        self.api.get("/api/vat/periods/")
        self.q3 = VatPeriod.objects.get(start_date=date(2026, 7, 1))
        self.q4 = VatPeriod.objects.get(start_date=date(2026, 10, 1))

    # -- helpers -------------------------------------------------------------------

    def sale(self, issued_at, lines, kind="invoice", status="reported", source="pos", simplified=True):
        self.icv += 1
        net = sum(D(row["taxable"]) for row in lines)
        vat = sum(D(row["vat"]) for row in lines)
        return ZatcaDocument.objects.create(
            branch=self.branch, device=self.device, environment=self.device.environment, kind=kind, simplified=simplified,
            number=f"DOC-{self.icv}", uuid=str(uuid.uuid4()), icv=self.icv, pih="pih", invoice_hash="hash",
            issued_at=issued_at, total=net + vat, vat_total=vat, net_total=net, vat_breakdown=lines,
            source=source, signed_xml=f"<Invoice>{self.icv}</Invoice>", qr="qr", status=status,
        )

    def purchase_doc(self, issue_date, net, vat, kind="invoice", supplier=None, purchase=None):
        purchase = purchase or Purchase.objects.create(branch=self.branch, supplier=supplier or self.supplier)
        return SupplierDocument.objects.create(purchase=purchase, kind=kind, number=f"S-{SupplierDocument.objects.count() + 1}",
                                               issue_date=issue_date, net=D(net), vat=D(vat), total=D(net) + D(vat))

    def q3_scenario(self):
        self.sale(riyadh(2026, 7, 5, 12), [line("S", "100.00", "15.00")])
        self.sale(riyadh(2026, 8, 1, 12), [line("S", "200.00", "30.00"), line("Z", "50.00", "0.00", "0.00")],
                  status="cleared", simplified=False)
        self.sale(riyadh(2026, 8, 2, 12), [line("S", "10.00", "1.50")], kind="debit_note")
        self.sale(riyadh(2026, 8, 3, 12), [line("S", "20.00", "3.00")], kind="credit_note")
        self.sale(riyadh(2026, 8, 4, 12), [line("S", "40.00", "6.00")], status="pending")
        self.sale(riyadh(2026, 8, 5, 12), [line("S", "60.00", "9.00")], status="rejected")
        self.sale(riyadh(2026, 8, 6, 12), [line("S", "1000.00", "150.00")], source="selftest")
        # Saudi midnight is the boundary, not UTC's: both are 30 Sep in UTC.
        self.sale(riyadh(2026, 9, 30, 23, 30), [line("S", "8.00", "1.20")])
        self.sale(riyadh(2026, 10, 1, 0, 30), [line("S", "8.00", "1.20")])

        invoice = self.purchase_doc(date(2026, 7, 10), "100.00", "15.00")
        self.purchase_doc(date(2026, 8, 10), "20.00", "3.00", kind="credit_note", purchase=invoice.purchase)
        self.purchase_doc(date(2026, 8, 11), "50.00", "0.00")  # VAT 0: zero rated by default
        self.purchase_doc(date(2026, 8, 12), "40.00", "6.00", supplier=self.no_vat_number)
        deleted = self.purchase_doc(date(2026, 8, 13), "100.00", "15.00")
        Purchase.all_objects.filter(pk=deleted.purchase_id).update(is_deleted=True)
        self.purchase_doc(date(2026, 10, 2), "10.00", "1.50")  # Q4

    def period(self, period):
        response = self.api.get(f"/api/vat/periods/{period.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["figures"]

    def boxes(self, data):
        return {row["box"]: (row["amount"], row["adjustment"], row["vat"]) for row in data["boxes"]}

    def documents(self, period, which, page=1):
        return self.api.get(f"/api/vat/periods/{period.pk}/documents/", {"list": which, "page": page}).data


class VatFiguresTests(VatFixture, TestCase):

    # -- cards and boxes -----------------------------------------------------------

    def test_cards(self):
        self.q3_scenario()
        data = self.period(self.q3)
        self.assertEqual(data["cards"], {
            "output_vat": D("44.70"), "input_vat": D("12.00"), "input_vat_purchases": D("12.00"),
            "input_vat_expenses": D("0.00"), "credit_in": D("0.00"), "credit_source": "none",
            "previous_period": None, "correction": D("0.00"), "vat_due": D("32.70"), "zatca_amount": None, "difference": None,
            "paid": D("0.00"), "balance": D("32.70"),
        })
        self.assertEqual(data["not_counted"], {"pending": {"count": 1, "vat": D("6.00")},
                                               "rejected": {"count": 1, "vat": D("9.00")}})
        self.assertEqual(data["review"], {"count": 1, "vat": D("6.00")})
        self.assertEqual(data["missing_breakdown"], 0)

    def test_return_boxes(self):
        self.q3_scenario()
        boxes = self.boxes(self.period(self.q3))
        self.assertEqual(boxes["1"], (D("308.00"), D("-10.00"), D("44.70")))
        self.assertEqual(boxes["3"], (D("50.00"), D("0.00"), D("0.00")))
        self.assertEqual(boxes["5"], (D("0.00"), D("0.00"), D("0.00")))
        self.assertEqual(boxes["6"], (D("358.00"), D("-10.00"), D("44.70")))
        self.assertEqual(boxes["7"], (D("100.00"), D("-20.00"), D("12.00")))
        self.assertEqual(boxes["10"], (D("50.00"), D("0.00"), D("0.00")))
        self.assertEqual(boxes["12"], (D("150.00"), D("-20.00"), D("12.00")))
        self.assertEqual(boxes["13"], (None, None, D("32.70")))
        self.assertEqual(boxes["14"], (None, None, D("0.00")))
        self.assertEqual(boxes["15"], (None, None, D("0.00")))
        self.assertEqual(boxes["16"], (None, None, D("32.70")))
        self.assertEqual(list(boxes), [str(n) for n in range(1, 17)])

    def test_next_period_takes_what_falls_after_saudi_midnight(self):
        self.q3_scenario()
        cards = self.period(self.q4)["cards"]
        self.assertEqual((cards["output_vat"], cards["input_vat"]), (D("1.20"), D("1.50")))
        self.assertEqual((cards["credit_in"], cards["credit_source"], cards["previous_period"]),
                         (D("0.00"), "estimated", self.q3.pk))

    def test_a_credit_is_carried_into_the_next_period(self):
        self.purchase_doc(date(2026, 8, 10), "100.00", "15.00")
        q3 = self.period(self.q3)["cards"]
        self.assertEqual((q3["vat_due"], q3["balance"]), (D("-15.00"), D("0.00")))
        q4 = self.period(self.q4)
        self.assertEqual((q4["cards"]["credit_in"], q4["cards"]["vat_due"], q4["cards"]["balance"]),
                         (D("15.00"), D("-15.00"), D("0.00")))
        boxes = self.boxes(q4)
        self.assertEqual((boxes["15"][2], boxes["16"][2]), (D("15.00"), D("-15.00")))

    def test_documents_without_a_breakdown_are_counted_but_flagged(self):
        document = self.sale(riyadh(2026, 7, 5, 12), [line("S", "100.00", "15.00")])
        ZatcaDocument.objects.filter(pk=document.pk).update(vat_breakdown=None)
        data = self.period(self.q3)
        self.assertEqual((data["cards"]["output_vat"], data["missing_breakdown"]), (D("15.00"), 1))
        self.assertEqual(self.boxes(data)["1"], (D("0.00"), D("0.00"), D("0.00")))

    def test_a_later_supplier_edit_doesnt_change_the_figures(self):
        self.purchase_doc(date(2026, 8, 10), "100.00", "15.00")
        Supplier.objects.filter(pk=self.supplier.pk).update(vat_number="")
        self.assertEqual(self.period(self.q3)["cards"]["input_vat"], D("15.00"))

    # -- "Show documents" ----------------------------------------------------------

    def test_document_lists_add_up_to_the_cards(self):
        self.q3_scenario()
        cards = self.period(self.q3)

        output = self.documents(self.q3, "output")
        self.assertEqual((output["count"], output["totals"]["vat"]), (5, cards["cards"]["output_vat"]))
        self.assertEqual(sum(row["vat"] for row in output["rows"]), cards["cards"]["output_vat"])
        credit = next(row for row in output["rows"] if row["kind"] == "credit_note")
        self.assertEqual((credit["net"], credit["vat"], credit["total"]), (D("-20.00"), D("-3.00"), D("-23.00")))

        not_counted = self.documents(self.q3, "not_counted")
        self.assertEqual(sorted(row["status"] for row in not_counted["rows"]), ["pending", "rejected"])

        purchases = self.documents(self.q3, "input")
        self.assertEqual((purchases["count"], purchases["totals"]["claimable_vat"]), (4, cards["cards"]["input_vat"]))
        note = next(row for row in purchases["rows"] if row["kind"] == "credit_note")
        self.assertEqual((note["net"], note["vat"], note["claimable_vat"]), (D("-20.00"), D("-3.00"), D("-3.00")))

        review = self.documents(self.q3, "review")
        self.assertEqual((review["count"], review["totals"]["vat"]), (1, cards["review"]["vat"]))
        self.assertEqual(review["rows"][0]["flags"], ["noQr", "supplierVatMissing"])

    def test_document_lists_are_paged(self):
        for day in range(figures.PAGE_SIZE + 5):
            self.sale(riyadh(2026, 7, 1 + day % 28, 12), [line("S", "10.00", "1.50")])
        first = self.documents(self.q3, "output")
        self.assertEqual((len(first["rows"]), first["has_more"], first["count"]), (100, True, 105))
        self.assertEqual(first["totals"]["vat"], D("157.50"))  # the whole list, not the page
        second = self.documents(self.q3, "output", page=2)
        self.assertEqual((len(second["rows"]), second["has_more"]), (5, False))

    def test_unknown_list(self):
        response = self.api.get(f"/api/vat/periods/{self.q3.pk}/documents/", {"list": "everything"})
        self.assertEqual((response.status_code, response.data), (400, {"list": "error.vatListInvalid"}))

    def test_access(self):
        self.api.force_authenticate(self.manager)
        self.assertEqual(self.api.get(f"/api/vat/periods/{self.q3.pk}/").status_code, 200)
        self.api.force_authenticate(self.cashier)
        self.assertEqual(self.api.get(f"/api/vat/periods/{self.q3.pk}/").status_code, 403)
        response = self.api.get(f"/api/vat/periods/{self.q3.pk}/documents/", {"list": "output"})
        self.assertEqual(response.status_code, 403)


class DocumentXmlDownloadTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Burger House", name_ar="بيت البرجر")
        self.device = ZatcaDevice.objects.create(branch=self.branch, environment="sandbox")
        self.document = ZatcaDocument.objects.create(
            branch=self.branch, device=self.device, environment="sandbox", kind="invoice", simplified=False,
            number="INV-7", uuid=str(uuid.uuid4()), icv=1, pih="pih", invoice_hash="hash",
            issued_at=riyadh(2026, 8, 1, 12), total=D("115.00"), vat_total=D("15.00"),
            signed_xml="<Invoice>signed</Invoice>", qr="qr", status="cleared",
        )
        self.api = APIClient()
        self.api.force_authenticate(make_staff(self.branch, "Manager", StaffRole.MANAGER))
        self.url = f"/api/zatca/documents/{self.document.pk}/xml/"

    def test_signed_xml(self):
        response = self.api.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"<Invoice>signed</Invoice>")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="INV-7.xml"')

    def test_cleared_xml_only_when_zatca_sent_one(self):
        self.assertEqual(self.api.get(self.url, {"which": "cleared"}).status_code, 404)
        ZatcaDocument.objects.filter(pk=self.document.pk).update(cleared_xml="<Invoice>cleared</Invoice>")
        response = self.api.get(self.url, {"which": "cleared"})
        self.assertEqual(response.content, b"<Invoice>cleared</Invoice>")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="INV-7_cleared.xml"')

    def test_other_branches_and_cashiers_get_nothing(self):
        other = Branch.objects.create(name_en="Other", name_ar="آخر")
        self.api.force_authenticate(make_staff(other, "Owner", StaffRole.OWNER))
        self.assertEqual(self.api.get(self.url).status_code, 404)
        self.api.force_authenticate(make_staff(self.branch, "Cashier", StaffRole.CASHIER))
        self.assertEqual(self.api.get(self.url).status_code, 403)
