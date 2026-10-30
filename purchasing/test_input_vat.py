"""VAT dashboard M2: input VAT fixes. Claimable VAT is a stored figure from
the supplier's details as saved on the document; missing supplier VAT
details hold the VAT back for review; "VAT not 15%"; zero rated/exempt;
deleted purchases; and the accountant's CSV."""

import csv
import io
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from inventory.models import InventoryItem
from purchasing.models import Purchase, PurchaseItem, Supplier, SupplierDocument
from purchasing.test_vat import SUPPLIER_VAT, supplier_qr

D = Decimal


class InputVatFixesTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Main", name_ar="الرئيسي")
        self.owner = Staff(branch=self.branch, name="Owner", role=StaffRole.OWNER)
        self.owner.set_pin("1234")
        self.owner.save()
        self.supplier = Supplier.objects.create(name_en="Fresh Meat Co", name_ar="لحوم", vat_number=SUPPLIER_VAT,
                                                crn="1010010000")
        self.ingredient = InventoryItem.objects.create(branch=self.branch, name="Beef", unit="kg",
                                                       current_stock=D("5.000"), cost_per_unit=D("10.00"))
        self.purchase = Purchase.objects.create(branch=self.branch, supplier=self.supplier)
        PurchaseItem.objects.create(purchase=self.purchase, inventory_item=self.ingredient, quantity=D("10.000"),
                                    unit_cost=D("10.00"))
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.invoice_url = f"/api/purchases/{self.purchase.pk}/supplier-invoice/"
        self.today = str(timezone.localdate())

    def put_invoice(self, **overrides):
        body = {"number": "SINV-77", "issue_date": self.today, "net": "100.00", "vat": "15.00", "total": "115.00",
                "qr_text": "", **overrides}
        return self.api.put(self.invoice_url, body, format="json")

    def report(self):
        return self.api.get("/api/purchases/input-vat/").data

    def test_claimable_is_stored_from_the_suppliers_details_when_saved(self):
        response = self.put_invoice()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["claimable_vat"], response.data["supplier_vat_number"],
                          response.data["supplier_crn"]), ("15.00", SUPPLIER_VAT, "1010010000"))
        # Changing the supplier behind the API's back doesn't rewrite the stored figure.
        Supplier.objects.filter(pk=self.supplier.pk).update(vat_registered=False, vat_number="")
        self.assertEqual(self.report()["totals"]["claimable_vat"], D("15.00"))

    def test_missing_supplier_vat_number_holds_the_vat_for_review(self):
        Supplier.objects.filter(pk=self.supplier.pk).update(vat_number="")
        self.put_invoice()
        report = self.report()
        [row] = report["rows"]
        self.assertEqual((row["claimable_vat"], row["needs_review"]), (D("0.00"), True))
        self.assertIn("supplierVatMissing", row["flags"])
        self.assertEqual(report["totals"]["claimable_vat"], D("0.00"))
        self.assertEqual(report["review"], {"count": 1, "vat": D("15.00")})

        # Adding the VAT number on the supplier makes the VAT claimable.
        response = self.api.patch(f"/api/suppliers/{self.supplier.pk}/", {"vat_number": SUPPLIER_VAT}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        report = self.report()
        self.assertEqual(report["totals"]["claimable_vat"], D("15.00"))
        self.assertEqual(report["review"], {"count": 0, "vat": D("0.00")})

    def test_not_registered_supplier_is_listed_for_review(self):
        self.api.patch(f"/api/suppliers/{self.supplier.pk}/", {"vat_registered": False}, format="json")
        self.put_invoice(vat="0.00", total="100.00")
        report = self.report()
        [row] = report["rows"]
        self.assertEqual((row["claimable_vat"], row["vat_category"], row["needs_review"]), (D("0.00"), "zero", True))
        self.assertEqual(row["flags"], ["notRegistered"])
        self.assertEqual(report["review"], {"count": 1, "vat": D("0.00")})

    def test_vat_not_15_percent_warns_only(self):
        self.put_invoice(vat="14.00", total="114.00")
        [row] = self.report()["rows"]
        self.assertIn("vatNot15", row["flags"])
        self.assertEqual((row["claimable_vat"], row["vat_rate"]), (D("14.00"), D("14.00")))
        # Within 0.05 of 15%: no flag.
        self.put_invoice(vat="15.04", total="115.04")
        [row] = self.report()["rows"]
        self.assertNotIn("vatNot15", row["flags"])

    def test_vat_zero_defaults_to_zero_rated_and_can_be_exempt(self):
        response = self.put_invoice(vat="0.00", total="100.00")
        self.assertEqual((response.data["vat_category"], response.data["claimable_vat"]), ("zero", "0.00"))
        [row] = self.report()["rows"]
        self.assertNotIn("vatNot15", row["flags"])
        response = self.put_invoice(vat="0.00", total="100.00", vat_category="exempt")
        self.assertEqual(response.data["vat_category"], "exempt")
        # Entering VAT again makes it standard rated.
        self.assertEqual(self.put_invoice().data["vat_category"], "standard")

    def test_zero_rated_or_exempt_cannot_carry_vat(self):
        response = self.put_invoice(vat_category="zero")
        self.assertEqual((response.status_code, response.data["vat_category"]),
                         (400, ["error.vatCategoryNeedsZeroVat"]))

    def test_purchase_with_supplier_documents_cannot_be_deleted(self):
        self.put_invoice()
        response = self.api.delete(f"/api/purchases/{self.purchase.pk}/")
        self.assertEqual((response.status_code, response.data["error"]), (409, "error.purchaseHasSupplierDocuments"))
        self.assertEqual(self.api.delete(self.invoice_url).status_code, 204)
        self.assertEqual(self.api.delete(f"/api/purchases/{self.purchase.pk}/").status_code, 204)

    def test_deleted_purchases_are_not_in_the_report(self):
        self.put_invoice()
        Purchase.objects.get(pk=self.purchase.pk).delete()  # soft delete, as before this fix
        self.assertEqual(self.report()["rows"], [])
        self.assertEqual(self.report()["totals"]["claimable_vat"], D("0.00"))
        self.assertTrue(SupplierDocument.objects.exists())  # the tax record itself is kept

    def test_csv_for_the_accountant(self):
        self.put_invoice(issue_date="2026-10-15", qr_text=supplier_qr())
        self.api.post(f"/api/purchases/{self.purchase.pk}/supplier-credit-notes/",
                      {"number": "SCN-1", "issue_date": "2026-10-16", "net": "10.00", "vat": "1.40", "total": "11.50",
                       "reason": "Return"}, format="json")

        response = self.api.get("/api/purchases/input-vat/csv/?from=2026-10-01&to=2026-10-31")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn('filename="input-vat_2026-10-01_2026-10-31.csv"', response["Content-Disposition"])
        text = response.content.decode("utf-8")
        self.assertTrue(text.startswith("﻿"))
        self.assertIn("\r\n", text)
        lines = list(csv.reader(io.StringIO(text[1:])))
        self.assertEqual(lines[0], ["Restaurant", "Main / الرئيسي"])
        self.assertEqual(lines[1], ["VAT number", ""])
        self.assertEqual(lines[2], ["Period", "2026-10-01 to 2026-10-31"])
        self.assertEqual(lines[4], [])
        self.assertEqual(lines[5], ["Date", "Source", "Type", "Supplier", "Supplier VAT number", "Supplier CR",
                                    "Document number", "Purchase", "Expense category", "Capital asset",
                                    "VAT category", "Net", "VAT rate %", "VAT", "Total", "Claimable VAT",
                                    "Not claimable reason", "Flags"])
        po = f"PO-{self.purchase.pk}"
        self.assertEqual(lines[6], ["2026-10-15", "Purchase order", "Tax invoice", "Fresh Meat Co", SUPPLIER_VAT,
                                    "1010010000", "SINV-77", po, "", "", "Standard rated", "100.00", "15.00",
                                    "15.00", "115.00", "15.00", "", ""])
        self.assertEqual(lines[7], ["2026-10-16", "Purchase order", "Credit note", "Fresh Meat Co", SUPPLIER_VAT,
                                    "1010010000", "SCN-1", po, "", "", "Standard rated", "-10.00", "14.00", "-1.40",
                                    "-11.50", "-1.40", "", "Net + VAT doesn't equal total; VAT not 15%"])
        self.assertEqual(lines[8], ["TOTALS", "", "", "", "", "", "", "", "", "", "", "90.00", "", "13.60", "103.50",
                                    "13.60", "", ""])
        self.assertEqual(len(lines), 9)
