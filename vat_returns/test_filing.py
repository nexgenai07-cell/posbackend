"""VAT dashboard M5: the ready-to-file checklist, Mark as filed, period
locks, owner overrides and the audit trail."""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from purchasing.models import Purchase, PurchaseStatus, SupplierDocument
from purchasing.test_vat import SUPPLIER_VAT

from .models import PeriodStatus, VatAuditLog, VatFiling, VatLockOverride
from .test_figures import VatFixture, line, riyadh

D = Decimal


class VatFilingTests(VatFixture, TestCase):
    def file(self, period, **body):
        return self.api.post(f"/api/vat/periods/{period.pk}/file/", {"filed_on": "2026-10-20", **body}, format="json")

    def detail(self, period):
        return self.api.get(f"/api/vat/periods/{period.pk}/").data

    def checklist(self, period):
        return {item["key"]: item["count"] for item in self.detail(period)["checklist"]}

    def one_sale(self):
        """A clean Q3: one reported sale (VAT 15), nothing to warn about."""
        return self.sale(riyadh(2026, 8, 1, 12), [line("S", "100.00", "15.00")])

    # -- checklist -----------------------------------------------------------------

    def test_checklist(self):
        self.q3_scenario()
        received = Purchase.objects.create(branch=self.branch, supplier=self.supplier, status=PurchaseStatus.RECEIVED)
        Purchase.objects.filter(pk=received.pk).update(received_at=riyadh(2026, 9, 1, 12))
        self.assertEqual(self.checklist(self.q3), {
            "pendingDocuments": 1, "rejectedDocuments": 1, "purchasesWithoutInvoice": 1,
            # No QR on the three invoices; one supplier also has no VAT number.
            "supplierDocumentsFlagged": 3, "missingBreakdown": 0, "previousNotFiled": 0, "periodNotEnded": 0,
        })
        q4 = self.checklist(self.q4)
        self.assertEqual((q4["previousNotFiled"], q4["periodNotEnded"]), (1, 1))

    # -- Mark as filed -------------------------------------------------------------

    def test_filing_locks_the_period_and_keeps_a_snapshot(self):
        self.one_sale()
        response = self.file(self.q3, zatca_amount="15.00")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["status"], response.data["locked"]), ("filed", True))
        self.assertEqual(response.data["filing"]["zatca_amount"], D("15.00"))
        self.assertEqual(response.data["filing"]["difference"], D("0.00"))
        self.assertEqual(response.data["filing"]["filed_by"], "Owner")
        cards = response.data["figures"]["cards"]
        self.assertEqual((cards["zatca_amount"], cards["balance"]), (D("15.00"), D("15.00")))
        self.assertIsNone(response.data["figures"]["changed_since_filing"])

        filing = VatFiling.objects.get()
        self.assertEqual(filing.calculated_net_due, D("15.00"))
        self.assertEqual(filing.calculated["cards"]["output_vat"], "15.00")
        self.assertEqual(len(filing.calculated["documents"]["output"]), 1)
        log = VatAuditLog.objects.get(action="filed")
        self.assertEqual((log.period_id, log.actor, log.data["zatca_amount"]), (self.q3.pk, self.owner, "15.00"))
        audit = self.api.get(f"/api/vat/periods/{self.q3.pk}/audit/").data
        self.assertEqual([row["action"] for row in audit], ["filed"])

    def test_open_warnings_need_one_more_confirmation(self):
        self.q3_scenario()
        response = self.file(self.q3, zatca_amount="32.70")
        self.assertEqual((response.status_code, response.data["error"]), (409, "error.vatChecklistOpen"))
        self.assertEqual(response.data["warnings"], ["pendingDocuments", "rejectedDocuments",
                                                     "supplierDocumentsFlagged"])
        self.assertFalse(VatFiling.objects.exists())
        response = self.file(self.q3, zatca_amount="32.70", confirm_warnings=True)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(VatFiling.objects.get().warnings, ["pendingDocuments", "rejectedDocuments",
                                                           "supplierDocumentsFlagged"])

    def test_a_different_zatca_amount_needs_a_reason_and_is_what_is_owed(self):
        self.one_sale()
        response = self.file(self.q3, zatca_amount="13.50")
        self.assertEqual((response.status_code, response.data), (400, {"difference_reason": "error.vatDifferenceReasonRequired"}))
        response = self.file(self.q3, zatca_amount="13.50", difference_reason="Portal rounded a credit note")
        self.assertEqual(response.status_code, 201, response.data)
        cards = response.data["figures"]["cards"]
        self.assertEqual((cards["vat_due"], cards["zatca_amount"], cards["difference"], cards["balance"]),
                         (D("15.00"), D("13.50"), D("-1.50"), D("13.50")))

    def test_validation(self):
        self.one_sale()
        response = self.file(self.q3, filed_on="2026-11-01", zatca_amount="abc")
        self.assertEqual(response.data, {"filed_on": "error.vatFiledOnInFuture", "zatca_amount": "error.vatAmountInvalid"})
        response = self.file(self.q3, filed_on="2026-06-30", zatca_amount="15.001")
        self.assertEqual(response.data, {"filed_on": "error.vatFiledOnBeforePeriod",
                                         "zatca_amount": "error.vatAmountInvalid"})
        response = self.file(self.q3, zatca_amount="15.00", attachment_url="http://example.com/x.pdf")
        self.assertEqual(response.data, {"attachment_url": "error.attachmentUrlInvalid"})

    def test_a_period_is_filed_once(self):
        self.one_sale()
        self.file(self.q3, zatca_amount="15.00")
        response = self.file(self.q3, zatca_amount="15.00")
        self.assertEqual((response.status_code, response.data["error"]), (409, "error.vatPeriodAlreadyFiled"))

    def test_only_the_owner_files(self):
        self.one_sale()
        self.api.force_authenticate(self.manager)
        self.assertEqual(self.file(self.q3, zatca_amount="15.00").status_code, 403)
        self.assertEqual(self.api.get(f"/api/vat/periods/{self.q3.pk}/audit/").status_code, 200)
        self.api.force_authenticate(self.cashier)
        self.assertEqual(self.api.get(f"/api/vat/periods/{self.q3.pk}/audit/").status_code, 403)

    # -- credit carried forward ----------------------------------------------------

    def test_zatcas_credit_is_carried_into_the_next_period(self):
        self.purchase_doc(date(2026, 8, 10), "100.00", "15.00")
        response = self.file(self.q3, zatca_amount="-14.00", difference_reason="ZATCA disallowed part of it",
                             confirm_warnings=True)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["status"], response.data["filing"]["credit_treatment"]),
                         ("credit", "carry_forward"))
        self.assertEqual(response.data["figures"]["cards"]["balance"], D("0.00"))
        cards = self.detail(self.q4)["figures"]["cards"]
        self.assertEqual((cards["credit_in"], cards["credit_source"]), (D("14.00"), "filed"))

    def test_a_refund_request_carries_nothing(self):
        self.purchase_doc(date(2026, 8, 10), "100.00", "15.00")
        self.file(self.q3, zatca_amount="-15.00", credit_treatment="refund_requested", confirm_warnings=True)
        cards = self.detail(self.q4)["figures"]["cards"]
        self.assertEqual((cards["credit_in"], cards["credit_source"]), (D("0.00"), "filed"))
        self.assertEqual(self.file(self.q4, zatca_amount="-1.00", credit_treatment="later",
                                   difference_reason="x", confirm_warnings=True).data,
                         {"credit_treatment": "error.vatCreditTreatmentInvalid"})

    # -- locks and overrides -------------------------------------------------------

    def put_invoice(self, purchase, issue_date, **extra):
        body = {"number": "SINV-9", "issue_date": issue_date, "net": "100.00", "vat": "15.00", "total": "115.00",
                "qr_text": "", **extra}
        return self.api.put(f"/api/purchases/{purchase.pk}/supplier-invoice/", body, format="json")

    def test_a_filed_period_is_locked_until_the_owner_overrides(self):
        self.one_sale()
        self.file(self.q3, zatca_amount="15.00")
        purchase = Purchase.objects.create(branch=self.branch, supplier=self.supplier)
        # Kept in Q3 rather than claimed in Q4 (the default, test_late_amend.py).

        response = self.put_invoice(purchase, "2026-09-15", keep_in_filed_period=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"error": "error.vatPeriodLocked", "period_id": str(self.q3.pk),
                                         "period_start": "2026-07-01", "period_end": "2026-09-30"})
        self.assertEqual(self.put_invoice(purchase, "2026-09-15", keep_in_filed_period=True, lock_override_reason="late").data,
                         {"lock_override_reason": ["error.vatOverrideReasonShort"]})
        self.api.force_authenticate(self.manager)
        response = self.put_invoice(purchase, "2026-09-15", keep_in_filed_period=True, lock_override_reason="Found in the September post")
        self.assertEqual((response.status_code, response.data["error"]), (403, "error.vatOverrideOwnerOnly"))
        self.assertFalse(SupplierDocument.objects.exists())

        self.api.force_authenticate(self.owner)
        response = self.put_invoice(purchase, "2026-09-15", keep_in_filed_period=True, lock_override_reason="Found in the September post")
        self.assertEqual(response.status_code, 201, response.data)
        override = VatLockOverride.objects.get()
        self.assertEqual((override.period, override.action, override.object_id, override.actor),
                         (self.q3, "create", response.data["id"], self.owner))
        self.assertEqual((override.before, override.after["claimable_vat"]), (None, "15.00"))
        self.assertEqual(VatAuditLog.objects.filter(action="lock_override", period=self.q3).count(), 1)

        changed = self.detail(self.q3)["figures"]["changed_since_filing"]
        self.assertEqual((changed["input_vat"], changed["vat_due"]), (D("15.00"), D("-15.00")))
        self.assertEqual(changed["documents"]["input"], {"added": [response.data["id"]], "removed": []})

    def test_moving_a_document_out_of_a_filed_period_is_an_edit_of_it(self):
        document = self.purchase_doc(date(2026, 9, 15), "100.00", "15.00")
        self.file(self.q3, zatca_amount="0.00", difference_reason="Claimed next quarter", confirm_warnings=True)
        response = self.put_invoice(document.purchase, "2026-10-02")
        self.assertEqual((response.status_code, response.data["error"]), (409, "error.vatPeriodLocked"))
        document.refresh_from_db()
        self.assertEqual(document.issue_date, date(2026, 9, 15))

    def test_documents_in_open_periods_are_free(self):
        self.one_sale()
        self.file(self.q3, zatca_amount="15.00")
        purchase = Purchase.objects.create(branch=self.branch, supplier=self.supplier)
        self.assertEqual(self.put_invoice(purchase, "2026-10-05").status_code, 201)
        self.assertFalse(VatLockOverride.objects.exists())

    def test_deleting_a_credit_note_in_a_filed_period(self):
        invoice = self.purchase_doc(date(2026, 8, 10), "100.00", "15.00")
        note = self.purchase_doc(date(2026, 8, 20), "20.00", "3.00", kind="credit_note", purchase=invoice.purchase)
        self.file(self.q3, zatca_amount="-12.00", confirm_warnings=True)
        url = f"/api/purchases/{invoice.purchase_id}/supplier-credit-notes/{note.pk}/"
        self.assertEqual(self.api.delete(url).status_code, 409)
        response = self.api.delete(url + "?lock_override_reason=Keyed in twice by mistake")
        self.assertEqual(response.status_code, 204)
        override = VatLockOverride.objects.get()
        self.assertEqual((override.action, override.object_id, override.after), ("delete", note.pk, None))
        self.assertEqual(override.before["claimable_vat"], "-3.00")

    def test_a_supplier_fix_leaves_filed_periods_as_filed(self):
        filed = self.purchase_doc(date(2026, 8, 10), "40.00", "6.00", supplier=self.no_vat_number)
        self.file(self.q3, zatca_amount="0.00", confirm_warnings=True)
        later = self.purchase_doc(date(2026, 10, 10), "40.00", "6.00", supplier=self.no_vat_number)
        response = self.api.patch(f"/api/suppliers/{self.no_vat_number.pk}/", {"vat_number": SUPPLIER_VAT},
                                  format="json")
        self.assertEqual(response.status_code, 200, response.data)
        filed.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual((filed.claimable_vat, filed.supplier_vat_number), (D("0.00"), ""))
        self.assertEqual(later.claimable_vat, D("6.00"))

    def test_a_sale_accepted_after_filing_shows_as_changed(self):
        self.one_sale()
        pending = self.sale(riyadh(2026, 9, 2, 12), [line("S", "40.00", "6.00")], status="pending")
        self.file(self.q3, zatca_amount="15.00", confirm_warnings=True)
        pending.status = "reported"
        pending.save(update_fields=["status"])
        changed = self.detail(self.q3)["figures"]["changed_since_filing"]
        self.assertEqual((changed["output_vat"], changed["vat_due"]), (D("6.00"), D("6.00")))
        self.assertEqual(changed["documents"]["output"], {"added": [pending.pk], "removed": []})

    def test_filed_periods_keep_their_dates(self):
        self.one_sale()
        self.file(self.q3, zatca_amount="15.00")
        response = self.api.patch("/api/vat/settings/", {"tracking_start": "2026-07-02"}, format="json")
        self.assertEqual((response.status_code, response.data["error"]), (409, "error.vatPeriodsFiled"))
        self.q3.refresh_from_db()
        self.assertEqual((self.q3.status, self.q3.locked), (PeriodStatus.FILED, True))
