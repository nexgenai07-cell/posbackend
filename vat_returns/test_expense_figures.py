"""VAT dashboard M11b: business expenses in the VAT figures. The input VAT
card's two lines, boxes 7/10/11, the "Show documents" lists, the
checklist, the filing snapshot, the Input VAT report and the archive."""

import zipfile
from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from purchasing.models import Expense, Supplier
from vat_returns import archive_zip

from .test_archive import ArchiveFixture

D = Decimal


class ExpenseFiguresTests(ArchiveFixture, TestCase):

    def setUp(self):
        super().setUp()
        self.landlord = Supplier.objects.create(name_en="Olaya Properties", name_ar="العليا العقارية",
                                                vat_number="300000000000003")

    def expense(self, issue_date, net, vat, **fields):
        fields.setdefault("supplier", self.landlord)
        fields.setdefault("category", "rent")
        return Expense.objects.create(branch=self.branch, number=f"E-{Expense.objects.count() + 1}",
                                      issue_date=issue_date, net=D(net), vat=D(vat), total=D(net) + D(vat), **fields)

    def scenario(self):
        """Q3: a supplier invoice (VAT 15) and expenses: rent (150), an
        equipment credit note (-30), zero-rated water (0), exempt
        insurance (0), a not-claimable dinner (45), one from a supplier with
        no VAT number (60) and a voided one (999)."""
        self.purchase_doc(date(2026, 8, 10), "100.00", "15.00")
        self.expense(date(2026, 7, 1), "1000.00", "150.00", attachment_url="https://files.example/rent.pdf")
        self.expense(date(2026, 8, 1), "200.00", "30.00", kind="credit_note", category="equipment")
        self.expense(date(2026, 8, 2), "80.00", "0.00", category="utilities")
        self.expense(date(2026, 8, 3), "50.00", "0.00", category="services", vat_category="exempt")
        self.expense(date(2026, 8, 4), "300.00", "45.00", category="services", not_claimable=True,
                     not_claimable_reason="entertainment")
        self.expense(date(2026, 8, 5), "400.00", "60.00", supplier=self.no_vat_number)
        self.expense(date(2026, 8, 6), "1.00", "999.00", voided=True, voided_reason="Keyed in twice")

    def test_input_vat_card_has_two_lines(self):
        self.scenario()
        data = self.period(self.q3)
        cards = data["cards"]
        # 150 - 30 from expenses; the dinner, the no-VAT-number one and the voided one claim 0.
        self.assertEqual((cards["input_vat_purchases"], cards["input_vat_expenses"], cards["input_vat"]),
                         (D("15.00"), D("120.00"), D("135.00")))
        self.assertEqual(data["review"], {"count": 1, "vat": D("60.00")})
        self.assertEqual(data["not_claimable"], {"count": 1, "vat": D("45.00")})

    def test_boxes_take_claimable_expenses_by_vat_category(self):
        self.scenario()
        boxes = self.boxes(self.period(self.q3))
        self.assertEqual(boxes["7"], (D("1100.00"), D("-200.00"), D("135.00")))
        self.assertEqual(boxes["10"], (D("80.00"), D("0.00"), D("0.00")))
        self.assertEqual(boxes["11"], (D("50.00"), D("0.00"), D("0.00")))
        self.assertEqual(boxes["12"], (D("1230.00"), D("-200.00"), D("135.00")))

    def test_document_lists_add_up_to_the_card_lines(self):
        self.scenario()
        cards = self.period(self.q3)
        expenses = self.documents(self.q3, "expenses")
        # Every non-voided expense, the not-claimable one included (claiming 0).
        self.assertEqual((expenses["count"], expenses["totals"]["claimable_vat"]),
                         (6, cards["cards"]["input_vat_expenses"]))
        note = next(row for row in expenses["rows"] if row["kind"] == "credit_note")
        self.assertEqual((note["source"], note["net"], note["claimable_vat"], note["category"]),
                         ("expense", D("-200.00"), D("-30.00"), "equipment"))
        purchases = self.documents(self.q3, "input")
        self.assertEqual(purchases["totals"]["claimable_vat"], cards["cards"]["input_vat_purchases"])

        review = self.documents(self.q3, "review")
        self.assertEqual((review["count"], review["totals"]["vat"]), (1, cards["review"]["vat"]))
        self.assertEqual(review["rows"][0]["source"], "expense")
        blocked = self.documents(self.q3, "not_claimable")
        self.assertEqual((blocked["count"], blocked["totals"]["vat"], blocked["rows"][0]["not_claimable_reason"]),
                         (1, D("45.00"), "entertainment"))

    def test_checklist_counts_expenses(self):
        self.scenario()
        checklist = {item["key"]: item["count"] for item in self.api.get(f"/api/vat/periods/{self.q3.pk}/").data["checklist"]}
        # Five non-voided expenses without an attachment.
        self.assertEqual(checklist["expensesWithoutAttachment"], 5)
        # Supplier invoice + rent: no QR; the no-VAT-number expense; the 15% checks.
        self.assertGreaterEqual(checklist["supplierDocumentsFlagged"], 3)

    def test_filing_snapshot_includes_expenses(self):
        rent = self.expense(date(2026, 8, 1), "1000.00", "150.00")
        response = self.api.post(f"/api/vat/periods/{self.q3.pk}/file/",
                                 {"filed_on": "2026-10-20", "zatca_amount": "-150.00", "confirm_warnings": True},
                                 format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(self.period(self.q3)["changed_since_filing"])
        late = self.api.post("/api/expenses/", {
            "supplier": self.landlord.pk, "number": "LATE-1", "issue_date": "2026-09-01", "category": "rent",
            "net": "100.00", "vat": "15.00", "total": "115.00", "keep_in_filed_period": True,
            "lock_override_reason": "Found in the September post"}, format="json")
        self.assertEqual(late.status_code, 201, late.data)
        changed = self.period(self.q3)["changed_since_filing"]
        self.assertEqual(changed["input_vat"], D("15.00"))
        self.assertEqual(changed["documents"]["expenses"], {"added": [late.data["id"]], "removed": []})
        self.assertNotIn(rent.pk, changed["documents"]["expenses"]["added"])

    def test_input_vat_report_and_csv_list_expenses(self):
        self.purchase_doc(date(2026, 10, 3), "100.00", "15.00")
        self.expense(date(2026, 10, 2), "500.00", "75.00", category="equipment", capital_asset=True)
        self.expense(date(2026, 10, 4), "100.00", "15.00", category="services", not_claimable=True,
                     not_claimable_reason="gifts")
        self.expense(date(2026, 10, 5), "1.00", "1.00", voided=True)
        report = self.api.get("/api/purchases/input-vat/", {"from": "2026-10-01", "to": "2026-10-31"}).data
        self.assertEqual([(row["source"], row["issue_date"]) for row in report["rows"]],
                         [("expense", date(2026, 10, 2)), ("purchase", date(2026, 10, 3)),
                          ("expense", date(2026, 10, 4))])
        self.assertEqual(report["totals"]["claimable_vat"], D("90.00"))

        text = self.api.get("/api/purchases/input-vat/csv/?from=2026-10-01&to=2026-10-31").content.decode("utf-8")
        lines = text.splitlines()
        self.assertIn("supplier invoices and expenses, less credit notes", text)
        equipment = next(line for line in lines if line.startswith("2026-10-02"))
        self.assertTrue(equipment.startswith("2026-10-02,Expense,Tax invoice,Olaya Properties,"))
        self.assertIn(",Equipment,Yes,Standard rated,", equipment)
        gifts = next(line for line in lines if line.startswith("2026-10-04"))
        self.assertIn(",Gifts,", gifts)

    def test_archive_expenses_tab_and_zip_folder(self):
        self.scenario()
        listing = self.listing("expenses", period=self.q3.pk)
        self.assertEqual(listing["totals"]["count"], 6)
        self.assertEqual(self.listing("expenses", category="services")["totals"]["count"], 2)
        self.assertEqual(self.listing("expenses", capital_asset="true")["totals"]["count"], 0)
        bad = self.api.get("/api/vat/archive/expenses/", {"category": "x"})
        self.assertEqual(bad.data, {"category": "error.archiveCategoryInvalid"})

        with mock.patch("vat_returns.archive_zip._download", return_value=b"%PDF-rent"):
            export = self.export(period=self.q3.pk)
        self.assertEqual(export.sides, ["sales", "purchases", "expenses"])
        self.assertEqual(archive_zip.expenses_count(export), 6)
        with zipfile.ZipFile(archive_zip.path_of(export)) as zip_file:
            names = set(zip_file.namelist())
            index = zip_file.read("index.csv").decode("utf-8")
        rent = Expense.objects.get(attachment_url__gt="")
        self.assertIn(f"expenses/Olaya Properties_{rent.number}.pdf", names)
        self.assertIn("TOTALS expenses", index)
        self.assertIn("missing (not uploaded)", index)
        self.assertIn("Entertainment / hospitality", index)
