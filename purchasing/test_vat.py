"""Milestone 9: supplier VAT details, supplier invoices and credit notes,
the QR check and the input VAT report. Nothing here talks to ZATCA."""

import base64
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from inventory.models import InventoryItem, StockMovement, StockMovementReason
from purchasing.models import Purchase, PurchaseItem, PurchaseStatus, Supplier, SupplierDocument
from purchasing.vat import check_qr, decode_qr

D = Decimal
SUPPLIER_VAT = "310122393500003"


def supplier_qr(seller="Fresh Meat Co", vat=SUPPLIER_VAT, when="2026-10-15T09:30:00", total="115.00",
                tax="15.00", phase2=False):
    data = b""
    for tag, value in enumerate((seller, vat, when, total, tax), start=1):
        raw = value.encode("utf-8")
        data += bytes([tag, len(raw)]) + raw
    if phase2:
        data += bytes([6, 3]) + b"abc"
    return base64.b64encode(data).decode()


class QrCheckTests(TestCase):
    def test_decodes_and_matches(self):
        result = check_qr(supplier_qr(phase2=True), supplier_vat=SUPPLIER_VAT, issue_date=date(2026, 10, 15),
                          total=D("115.00"), vat=D("15.00"))
        self.assertEqual((result["readable"], result["phase"], result["mismatches"]), (True, 2, []))
        self.assertEqual(result["fields"]["seller_name"], "Fresh Meat Co")

    def test_every_mismatch_is_reported(self):
        result = check_qr(supplier_qr(vat="300000000000003", total="120.00", tax="16.00"),
                          supplier_vat=SUPPLIER_VAT, issue_date=date(2026, 10, 14), total=D("115.00"),
                          vat=D("15.00"))
        self.assertEqual(result["mismatches"], ["vatNumber", "total", "vat", "date"])
        self.assertEqual(check_qr(supplier_qr())["mismatches"], ["supplierVatMissing"])

    def test_garbage_and_empty(self):
        self.assertIsNone(decode_qr("not a qr"))
        self.assertEqual(check_qr("%%%")["readable"], False)
        self.assertEqual(check_qr("")["present"], False)


class PurchaseVatApiTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Main", name_ar="Main")
        self.owner = Staff(branch=self.branch, name="Owner", role=StaffRole.OWNER)
        self.owner.set_pin("1234")
        self.owner.save()
        self.supplier = Supplier.objects.create(name_en="Fresh Meat Co", name_ar="لحوم", vat_number=SUPPLIER_VAT)
        self.ingredient = InventoryItem.objects.create(branch=self.branch, name="Beef", unit="kg",
                                                       current_stock=D("5.000"), cost_per_unit=D("10.00"))
        self.purchase = Purchase.objects.create(branch=self.branch, supplier=self.supplier)
        self.line = PurchaseItem.objects.create(purchase=self.purchase, inventory_item=self.ingredient,
                                                quantity=D("10.000"), unit_cost=D("10.00"))
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def invoice_body(self, **overrides):
        return {"number": "SINV-77", "issue_date": "2026-10-15", "net": "100.00", "vat": "15.00", "total": "115.00",
                "qr_text": supplier_qr(), **overrides}

    def test_supplier_vat_fields_are_validated(self):
        response = self.api.post("/api/suppliers/", {"name_en": "A", "name_ar": "ا", "vat_number": "12345",
                                                      "crn": "12"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.data), {"vat_number", "crn"})
        response = self.api.post("/api/suppliers/", {"name_en": "A", "name_ar": "ا", "vat_number": SUPPLIER_VAT,
                                                      "crn": "1010010000", "address": "Riyadh"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["vat_registered"])

    def test_receive_with_the_supplier_invoice(self):
        response = self.api.post(f"/api/purchases/{self.purchase.pk}/receive/",
                                 {"supplier_invoice": self.invoice_body()}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        invoice = response.data["supplier_invoice"]
        self.assertEqual((invoice["number"], invoice["qr_check"]["mismatches"]), ("SINV-77", []))
        self.assertEqual(self.purchase.supplier_documents.get().created_by, self.owner)

    def test_a_bad_invoice_stops_the_receive_before_stock_moves(self):
        response = self.api.post(f"/api/purchases/{self.purchase.pk}/receive/",
                                 {"supplier_invoice": self.invoice_body(number="")}, format="json")
        self.assertEqual(response.status_code, 400)
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.current_stock, D("5.000"))
        self.assertNotEqual(Purchase.objects.get(pk=self.purchase.pk).status, PurchaseStatus.RECEIVED)

    def test_invoice_upsert_rechecks_the_qr(self):
        url = f"/api/purchases/{self.purchase.pk}/supplier-invoice/"
        self.assertIsNone(self.api.get(url).data)
        self.assertEqual(self.api.put(url, self.invoice_body(total="116.00"), format="json").status_code, 201)
        self.assertEqual(self.api.get(url).data["qr_check"]["mismatches"], ["total"])
        response = self.api.put(url, self.invoice_body(), format="json")
        self.assertEqual((response.status_code, response.data["qr_check"]["mismatches"]), (200, []))
        self.assertEqual(SupplierDocument.objects.count(), 1)
        self.assertEqual(self.api.delete(url).status_code, 204)
        self.assertFalse(SupplierDocument.objects.exists())

    def test_not_registered_supplier_charges_no_vat(self):
        Supplier.objects.filter(pk=self.supplier.pk).update(vat_registered=False)
        url = f"/api/purchases/{self.purchase.pk}/supplier-invoice/"
        response = self.api.put(url, self.invoice_body(), format="json")
        self.assertEqual((response.status_code, response.data["vat"]), (400, ["error.supplierNotVatRegistered"]))
        body = self.invoice_body(vat="0.00", total="100.00", qr_text="")
        self.assertEqual(self.api.put(url, body, format="json").status_code, 201)

    def test_credit_note_with_returned_goods(self):
        self.api.post(f"/api/purchases/{self.purchase.pk}/receive/", {}, format="json")
        url = f"/api/purchases/{self.purchase.pk}/supplier-credit-notes/"
        body = {"number": "SCN-3", "issue_date": "2026-10-16", "net": "20.00", "vat": "3.00", "total": "23.00",
                "reason": "Two kg spoiled", "returned_lines": [{"purchase_item": self.line.pk, "quantity": "2"}]}
        self.assertEqual(self.api.post(url, {**body, "reason": ""}, format="json").status_code, 400)
        response = self.api.post(url, body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.current_stock, D("13.000"))
        movement = StockMovement.objects.filter(purchase=self.purchase, reason=StockMovementReason.ADJUSTMENT).get()
        self.assertEqual(movement.quantity_delta, D("-2.000"))
        self.assertEqual(len(self.api.get(url).data), 1)
        note_id = response.data["id"]
        self.assertEqual(self.api.patch(f"{url}{note_id}/", {"total": "23.00", "number": "SCN-3A"},
                                        format="json").data["number"], "SCN-3A")
        self.assertEqual(self.api.delete(f"{url}{note_id}/").status_code, 204)

    def test_qr_check_endpoint_saves_nothing(self):
        response = self.api.post("/api/purchases/qr-check/", {"qr_text": supplier_qr(), "supplier": self.supplier.pk,
                                                               "total": "115.00", "vat": "15.00",
                                                               "issue_date": "2026-10-15"}, format="json")
        self.assertEqual((response.status_code, response.data["mismatches"]), (200, []))
        self.assertFalse(SupplierDocument.objects.exists())

    def test_input_vat_report(self):
        self.api.post(f"/api/purchases/{self.purchase.pk}/receive/",
                      {"supplier_invoice": self.invoice_body(issue_date=str(timezone.localdate()))}, format="json")
        self.api.post(f"/api/purchases/{self.purchase.pk}/supplier-credit-notes/",
                      {"number": "SCN-1", "issue_date": str(timezone.localdate()), "net": "10.00", "vat": "1.50",
                       "total": "11.50", "reason": "Return"}, format="json")
        # A second, received purchase with no supplier invoice, from a non-registered supplier.
        small = Supplier.objects.create(name_en="Corner Shop", name_ar="بقالة", vat_registered=False)
        other = Purchase.objects.create(branch=self.branch, supplier=small, status=PurchaseStatus.RECEIVED,
                                        received_at=timezone.now())
        PurchaseItem.objects.create(purchase=other, inventory_item=self.ingredient, quantity=D("1"),
                                    unit_cost=D("8.00"))

        report = self.api.get("/api/purchases/input-vat/").data
        self.assertEqual([(r["kind"], r["vat"]) for r in report["rows"]],
                         [("invoice", D("15.00")), ("credit_note", D("-1.50"))])
        self.assertEqual(report["rows"][0]["flags"], ["qrMismatch"])  # the QR's date is 2026-10-15
        self.assertEqual(report["totals"]["claimable_vat"], D("13.50"))
        self.assertEqual([(m["purchase"], m["flags"]) for m in report["missing_invoices"]],
                         [(other.pk, ["noInvoice", "notRegistered"])])

    def test_report_flags(self):
        Supplier.objects.filter(pk=self.supplier.pk).update(vat_number="")
        self.api.put(f"/api/purchases/{self.purchase.pk}/supplier-invoice/",
                     self.invoice_body(issue_date=str(timezone.localdate()), net="90.00", qr_text=""), format="json")
        [row] = self.api.get("/api/purchases/input-vat/").data["rows"]
        # 15.00 on a net of 90.00 is 16.67%: "VAT not 15%" (VAT dashboard M2).
        self.assertEqual(row["flags"], ["totalsDontAdd", "noQr", "supplierVatMissing", "vatNot15",
                                        "netDiffersFromPurchase"])
