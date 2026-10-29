"""VAT dashboard M6: SADAD payments, ZATCA refunds, the period status and
the credit carried into the next period."""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from accounts.models import StaffRole
from branches.models import Branch
from zatca.tests import make_staff

from .models import VatAuditLog, VatPayment
from .test_figures import VatFixture, line, riyadh

D = Decimal


class VatPaymentsTests(VatFixture, TestCase):
    def file(self, period, amount, **body):
        response = self.api.post(f"/api/vat/periods/{period.pk}/file/",
                                 {"filed_on": "2026-10-20", "zatca_amount": amount, "confirm_warnings": True, **body},
                                 format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def pay(self, period, amount, **body):
        return self.api.post(f"/api/vat/periods/{period.pk}/payments/",
                             {"amount": amount, "paid_on": "2026-10-25", "sadad_reference": "SADAD-001", **body},
                             format="json")

    def refund(self, period, amount, **body):
        return self.api.post(f"/api/vat/periods/{period.pk}/refunds/",
                             {"amount": amount, "received_on": "2026-10-28", "reference": "RF-1", **body},
                             format="json")

    def void(self, kind, pk, reason="Entered against the wrong period"):
        return self.api.post(f"/api/vat/{kind}/{pk}/void/", {"reason": reason}, format="json")

    def due_150(self):
        """Q3 filed with VAT due 150.00."""
        self.sale(riyadh(2026, 8, 1, 12), [line("S", "1000.00", "150.00")])
        self.file(self.q3, "150.00")

    # -- payments ------------------------------------------------------------------

    def test_partial_payments_until_paid(self):
        self.due_150()
        first = self.pay(self.q3, "100.00")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["status"], "filed")
        self.assertEqual((first.data["figures"]["cards"]["paid"], first.data["figures"]["cards"]["balance"]),
                         (D("100.00"), D("50.00")))
        second = self.pay(self.q3, "50.00", sadad_reference="SADAD-002")
        self.assertEqual(second.data["status"], "paid")
        self.assertEqual(second.data["figures"]["cards"]["balance"], D("0.00"))
        self.assertEqual([p["sadad_reference"] for p in second.data["payments"]], ["SADAD-001", "SADAD-002"])
        self.assertEqual(VatAuditLog.objects.filter(action="payment_recorded", period=self.q3).count(), 2)

    def test_voiding_a_payment_keeps_it_and_reopens_the_balance(self):
        self.due_150()
        self.pay(self.q3, "100.00")
        self.pay(self.q3, "50.00")
        payment = VatPayment.objects.get(amount=D("50.00"))
        self.assertEqual(self.void("payments", payment.pk, "wrong").data, {"reason": "error.vatVoidReasonShort"})
        response = self.void("payments", payment.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["status"], response.data["figures"]["cards"]["balance"]), ("filed", D("50.00")))
        voided = next(p for p in response.data["payments"] if p["id"] == payment.pk)
        self.assertEqual((voided["voided"], voided["voided_reason"], voided["voided_by"]),
                         (True, "Entered against the wrong period", "Owner"))
        self.assertEqual(VatPayment.objects.count(), 2)
        self.assertEqual(self.void("payments", payment.pk).data["error"], "error.vatAlreadyVoided")
        self.assertTrue(VatAuditLog.objects.filter(action="payment_voided", period=self.q3).exists())

    def test_overpaying_shows_a_negative_balance(self):
        self.due_150()
        response = self.pay(self.q3, "200.00")
        self.assertEqual((response.data["status"], response.data["figures"]["cards"]["balance"]), ("paid", D("-50.00")))

    def test_only_filed_periods_with_something_to_pay(self):
        self.sale(riyadh(2026, 8, 1, 12), [line("S", "1000.00", "150.00")])
        self.assertEqual(self.pay(self.q3, "10.00").data["error"], "error.vatPeriodNotFiled")
        self.purchase_doc(date(2026, 10, 10), "100.00", "15.00")
        self.file(self.q4, "-15.00", filed_on="2026-10-28")
        self.assertEqual(self.pay(self.q4, "10.00").data["error"], "error.vatNothingToPay")

    def test_payment_validation(self):
        self.due_150()
        response = self.pay(self.q3, "0", paid_on="2026-11-01", sadad_reference=" ", receipt_url="ftp://x")
        self.assertEqual(response.data, {"amount": "error.vatPositiveAmount", "paid_on": "error.vatDateInFuture",
                                         "sadad_reference": "error.vatReferenceRequired",
                                         "receipt_url": "error.attachmentUrlInvalid"})
        self.assertEqual(self.pay(self.q3, "-5", paid_on="2026-06-30").data,
                         {"amount": "error.vatPositiveAmount", "paid_on": "error.vatDateTooEarly"})

    # -- refunds -------------------------------------------------------------------

    def test_refund_requested_then_received(self):
        self.purchase_doc(date(2026, 8, 10), "100.00", "15.00")
        data = self.file(self.q3, "-15.00", credit_treatment="refund_requested")
        self.assertEqual(data["status"], "credit")
        self.assertEqual(data["figures"]["refund"], {"requested": D("15.00"), "received": D("0.00"),
                                                     "outstanding": D("15.00"), "state": "requested"})
        self.assertEqual(self.refund(self.q3, "10.00").data["figures"]["refund"]["state"], "requested")
        response = self.refund(self.q3, "5.00", reference="RF-2")
        self.assertEqual(response.data["figures"]["refund"], {"requested": D("15.00"), "received": D("15.00"),
                                                              "outstanding": D("0.00"), "state": "received"})
        last = response.data["refunds"][-1]["id"]
        response = self.void("refunds", last)
        self.assertEqual(response.data["figures"]["refund"]["state"], "requested")
        self.assertEqual(len(response.data["refunds"]), 2)
        self.assertEqual(VatAuditLog.objects.filter(action__in=["refund_recorded", "refund_voided"]).count(), 3)

    def test_refunds_only_when_requested(self):
        self.purchase_doc(date(2026, 8, 10), "100.00", "15.00")
        self.file(self.q3, "-15.00")  # carried forward
        self.assertEqual(self.refund(self.q3, "15.00").data["error"], "error.vatNoRefundRequested")

    def test_a_refund_cant_be_dated_before_the_filing(self):
        self.purchase_doc(date(2026, 8, 10), "100.00", "15.00")
        self.file(self.q3, "-15.00", credit_treatment="refund_requested")
        self.assertEqual(self.refund(self.q3, "15.00", received_on="2026-10-19").data,
                         {"received_on": "error.vatDateTooEarly"})

    # -- credit carried forward ----------------------------------------------------

    def test_a_credit_period_lowers_the_next_periods_vat(self):
        self.purchase_doc(date(2026, 8, 10), "200.00", "30.00")
        self.file(self.q3, "-30.00")
        self.sale(riyadh(2026, 10, 5, 12), [line("S", "100.00", "15.00")])
        cards = self.api.get(f"/api/vat/periods/{self.q4.pk}/").data["figures"]["cards"]
        self.assertEqual((cards["output_vat"], cards["credit_in"], cards["vat_due"], cards["balance"]),
                         (D("15.00"), D("30.00"), D("-15.00"), D("0.00")))

    # -- access --------------------------------------------------------------------

    def test_access(self):
        self.due_150()
        self.pay(self.q3, "100.00")
        payment = VatPayment.objects.get()
        self.api.force_authenticate(self.manager)
        self.assertEqual(self.pay(self.q3, "50.00").status_code, 403)
        self.assertEqual(self.void("payments", payment.pk).status_code, 403)
        self.assertEqual(len(self.api.get(f"/api/vat/periods/{self.q3.pk}/").data["payments"]), 1)
        other = Branch.objects.create(name_en="Other", name_ar="آخر")
        self.api.force_authenticate(make_staff(other, "Other owner", StaffRole.OWNER))
        self.assertEqual(self.void("payments", payment.pk).status_code, 404)
