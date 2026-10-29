"""VAT dashboard M9: late supplier documents, amended returns, the
monthly/quarterly switch, the opening credit and box 14 corrections."""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from purchasing.models import Purchase

from . import locks
from .models import PeriodStatus, VatAuditLog, VatFiling, VatLockOverride, VatPeriod
from .test_figures import VatFixture, line, riyadh

D = Decimal


class VatM9Fixture(VatFixture):
    def file(self, period, amount, **body):
        response = self.api.post(f"/api/vat/periods/{period.pk}/file/",
                                 {"filed_on": "2026-10-20", "zatca_amount": amount, "confirm_warnings": True, **body},
                                 format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def amend(self, period, **body):
        return self.api.post(f"/api/vat/periods/{period.pk}/amend/",
                             {"filed_on": "2026-10-25", "confirm_warnings": True, **body}, format="json")

    def adjust(self, period, kind, amount, reason="Agreed with the accountant"):
        return self.api.post(f"/api/vat/periods/{period.pk}/adjustment/",
                             {"kind": kind, "amount": amount, "reason": reason}, format="json")

    def detail(self, period):
        response = self.api.get(f"/api/vat/periods/{period.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def put_invoice(self, purchase, issue_date, net="100.00", vat="15.00", **extra):
        body = {"number": "SINV-9", "issue_date": issue_date, "net": net, "vat": vat,
                "total": str(D(net) + D(vat)), "qr_text": "", **extra}
        return self.api.put(f"/api/purchases/{purchase.pk}/supplier-invoice/", body, format="json")

    def settings_patch(self, **body):
        return self.api.patch("/api/vat/settings/", body, format="json")


class LatePurchaseTests(VatM9Fixture, TestCase):
    def test_a_late_invoice_is_claimed_in_the_oldest_open_period(self):
        self.file(self.q3, "0.00")
        purchase = Purchase.objects.create(branch=self.branch, supplier=self.supplier)
        response = self.put_invoice(purchase, "2026-09-15")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["issue_date"], response.data["claim_date"]), ("2026-09-15", "2026-10-01"))
        self.assertEqual((response.data["claim_period"]["id"], response.data["claim_period"]["locked"]),
                         (self.q4.pk, False))
        self.assertEqual((response.data["issue_period"]["id"], response.data["issue_period"]["locked"]),
                         (self.q3.pk, True))
        self.assertFalse(VatLockOverride.objects.exists())
        self.assertEqual(self.detail(self.q4)["figures"]["cards"]["input_vat"], D("15.00"))
        self.assertIsNone(self.detail(self.q3)["figures"]["changed_since_filing"])

    def test_an_edit_that_keeps_the_date_keeps_the_claim_period(self):
        self.file(self.q3, "0.00")
        purchase = Purchase.objects.create(branch=self.branch, supplier=self.supplier)
        self.put_invoice(purchase, "2026-09-15")
        response = self.put_invoice(purchase, "2026-09-15", net="200.00", vat="30.00")
        self.assertEqual((response.status_code, response.data["claim_date"]), (200, "2026-10-01"))

    def test_a_document_claimed_before_filing_stays_in_its_period(self):
        document = self.purchase_doc(date(2026, 9, 15), "100.00", "15.00")
        self.file(self.q3, "-15.00")
        document.refresh_from_db()
        self.assertEqual(document.claim_date, date(2026, 9, 15))
        response = self.api.get(f"/api/purchases/{document.purchase_id}/supplier-invoice/")
        self.assertEqual((response.data["claim_period"]["id"], response.data["issue_period"]), (self.q3.pk, None))

    def test_after_several_filed_periods(self):
        VatPeriod.objects.filter(pk__in=[self.q3.pk, self.q4.pk]).update(locked=True)
        self.assertEqual(locks.claim_date_for(self.branch, date(2026, 9, 15)), date(2027, 1, 1))
        self.assertEqual(locks.claim_date_for(self.branch, date(2027, 1, 5)), date(2027, 1, 5))


class AmendedReturnTests(VatM9Fixture, TestCase):
    def test_amend_supersedes_the_filing(self):
        self.sale(riyadh(2026, 8, 1, 12), [line("S", "100.00", "15.00")])
        pending = self.sale(riyadh(2026, 9, 2, 12), [line("S", "40.00", "6.00")], status="pending")
        first = self.file(self.q3, "15.00")["filing"]
        self.api.post(f"/api/vat/periods/{self.q3.pk}/payments/",
                      {"amount": "15.00", "paid_on": "2026-10-21", "sadad_reference": "SADAD-1"}, format="json")
        self.q3.refresh_from_db()
        self.assertEqual(self.q3.status, PeriodStatus.PAID)

        pending.status = "reported"
        pending.save(update_fields=["status"])
        self.assertEqual(self.detail(self.q3)["figures"]["changed_since_filing"]["vat_due"], D("6.00"))

        response = self.amend(self.q3, zatca_amount="21.00", amendment_reason="Pending invoice accepted by ZATCA")
        self.assertEqual(response.status_code, 201, response.data)
        filing = response.data["filing"]
        self.assertEqual((filing["zatca_amount"], filing["supersedes"], filing["is_current"]),
                         (D("21.00"), first["id"], True))
        self.assertEqual(filing["amendment_reason"], "Pending invoice accepted by ZATCA")
        self.assertEqual([(row["id"], row["is_current"]) for row in response.data["filings"]],
                         [(filing["id"], True), (first["id"], False)])
        self.assertIsNone(response.data["figures"]["changed_since_filing"])
        # Paid 15 of ZATCA's new 21: back to Filed, 6 to pay.
        self.assertEqual((response.data["status"], response.data["figures"]["cards"]["balance"]),
                         (PeriodStatus.FILED, D("6.00")))
        self.assertEqual(VatFiling.objects.filter(period=self.q3).count(), 2)
        log = VatAuditLog.objects.get(action="amended")
        self.assertEqual((log.data["supersedes"], log.data["previous_zatca_amount"]), (first["id"], "15.00"))

    def test_amend_errors(self):
        response = self.amend(self.q3, zatca_amount="1.00", amendment_reason="Pending invoice accepted")
        self.assertEqual((response.status_code, response.data), (409, {"error": "error.vatPeriodNotFiled"}))
        self.file(self.q3, "0.00")
        response = self.amend(self.q3, zatca_amount="1.00", amendment_reason="short", filed_on="2026-10-19",
                              difference_reason="Rounding")
        self.assertEqual((response.status_code, response.data),
                         (400, {"amendment_reason": "error.vatAmendmentReasonShort",
                                "filed_on": "error.vatAmendedBeforeFiling"}))
        self.api.force_authenticate(self.manager)
        self.assertEqual(self.amend(self.q3, zatca_amount="0.00", amendment_reason="Pending invoice accepted")
                         .status_code, 403)
        self.assertEqual(VatFiling.objects.count(), 1)


@mock.patch("vat_returns.periods.today")
class FrequencySwitchTests(VatM9Fixture, TestCase):
    def test_switch_after_filing_takes_effect_on_a_later_quarter(self, today):
        today.return_value = date(2026, 10, 29)
        self.file(self.q3, "0.00")
        self.assertEqual(self.settings_patch(frequency="monthly").data,
                         {"frequency_effective_from": "error.vatSwitchDateRequired"})
        self.assertEqual(self.settings_patch(frequency="monthly", frequency_effective_from="2026-11-01").data,
                         {"frequency_effective_from": "error.vatSwitchNotQuarterStart"})
        self.assertEqual(self.settings_patch(frequency="monthly", frequency_effective_from="2026-10-01").data,
                         {"frequency_effective_from": "error.vatSwitchTooEarly"})
        response = self.settings_patch(frequency="monthly", frequency_effective_from="2027-01-01")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual({key: response.data[key] for key in
                          ("frequency", "current_frequency", "frequency_effective_from", "earliest_switch")},
                         {"frequency": "monthly", "current_frequency": "quarterly",
                          "frequency_effective_from": date(2027, 1, 1), "earliest_switch": date(2027, 1, 1)})

        today.return_value = date(2027, 2, 15)
        periods = self.api.get("/api/vat/periods/").data["periods"]
        self.assertEqual([(row["start_date"], row["end_date"], row["frequency"]) for row in reversed(periods)], [
            (date(2026, 7, 1), date(2026, 9, 30), "quarterly"),
            (date(2026, 10, 1), date(2026, 12, 31), "quarterly"),
            (date(2027, 1, 1), date(2027, 1, 31), "monthly"),
            (date(2027, 2, 1), date(2027, 2, 28), "monthly"),
        ])

    def test_switching_back_cancels_the_switch(self, today):
        today.return_value = date(2026, 10, 29)
        self.file(self.q3, "0.00")
        self.settings_patch(frequency="monthly", frequency_effective_from="2027-01-01")
        response = self.settings_patch(frequency="quarterly")
        self.assertEqual((response.data["frequency"], response.data["frequency_effective_from"]), ("quarterly", None))
        today.return_value = date(2027, 2, 15)
        periods = self.api.get("/api/vat/periods/").data["periods"]
        self.assertEqual((periods[0]["start_date"], periods[0]["frequency"]), (date(2027, 1, 1), "quarterly"))

    def test_monthly_to_quarterly_fills_to_the_quarter_start(self, today):
        today.return_value = date(2026, 10, 29)
        self.settings_patch(frequency="monthly")  # nothing filed: rebuilt as monthly from July
        july = VatPeriod.objects.get(start_date=date(2026, 7, 1))
        self.assertEqual(july.frequency, "monthly")
        self.file(july, "0.00")
        response = self.settings_patch(frequency="quarterly", frequency_effective_from="2027-01-01")
        self.assertEqual(response.status_code, 200, response.data)
        today.return_value = date(2027, 1, 15)
        periods = self.api.get("/api/vat/periods/").data["periods"]
        self.assertEqual([(row["start_date"], row["frequency"]) for row in reversed(periods)][-4:], [
            (date(2026, 10, 1), "monthly"), (date(2026, 11, 1), "monthly"), (date(2026, 12, 1), "monthly"),
            (date(2027, 1, 1), "quarterly"),
        ])
        self.assertEqual(periods[0]["end_date"], date(2027, 3, 31))


class AdjustmentTests(VatM9Fixture, TestCase):
    def test_opening_credit_on_the_first_period(self):
        self.sale(riyadh(2026, 8, 1, 12), [line("S", "100.00", "15.00")])
        response = self.adjust(self.q3, "opening_credit", "100.00", "Credit from the old accounting system")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["is_first"], response.data["opening_credit"]), (True, D("100.00")))
        cards = response.data["figures"]["cards"]
        self.assertEqual((cards["credit_in"], cards["credit_source"], cards["vat_due"]),
                         (D("100.00"), "opening", D("-85.00")))
        q4 = self.detail(self.q4)["figures"]["cards"]
        self.assertEqual((q4["credit_in"], q4["credit_source"]), (D("85.00"), "estimated"))
        rows = {row["id"]: row for row in self.api.get("/api/vat/history/").data["rows"]}
        self.assertEqual((rows[self.q3.pk]["credit_in"], rows[self.q4.pk]["credit_in"]), (D("100.00"), D("85.00")))
        log = VatAuditLog.objects.get(action="opening_credit_set")
        self.assertEqual((log.period, log.data["after"]["amount"]), (self.q3, "100.00"))

        self.assertEqual(self.adjust(self.q4, "opening_credit", "10.00").data,
                         {"error": "error.vatOpeningCreditFirstOnly"})
        self.assertEqual(self.adjust(self.q3, "opening_credit", "-1.00").data,
                         {"amount": "error.vatOpeningCreditNegative"})

    def test_box_14_correction(self):
        self.sale(riyadh(2026, 10, 5, 12), [line("S", "100.00", "15.00")])
        response = self.adjust(self.q4, "correction", "6.00", "Q3 sale accepted after filing")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["figures"]
        self.assertEqual((data["cards"]["correction"], data["cards"]["vat_due"]), (D("6.00"), D("21.00")))
        boxes = self.boxes(data)
        self.assertEqual((boxes["13"][2], boxes["14"][2], boxes["16"][2]), (D("15.00"), D("6.00"), D("21.00")))
        self.assertEqual(response.data["correction_reason"], "Q3 sale accepted after filing")

        self.assertEqual(self.adjust(self.q4, "correction", "-5000.01").data, {"amount": "error.vatCorrectionLimit"})
        self.assertEqual(self.adjust(self.q4, "correction", "1.00", "short").data,
                         {"reason": "error.vatAdjustmentReasonShort"})
        self.assertEqual(self.adjust(self.q4, "refund", "1.00").data, {"kind": "error.vatAdjustmentKindInvalid"})
        self.file(self.q3, "0.00")
        self.assertEqual(self.adjust(self.q3, "correction", "1.00").data, {"error": "error.vatAdjustmentPeriodFiled"})
        self.api.force_authenticate(self.manager)
        self.assertEqual(self.adjust(self.q4, "correction", "1.00").status_code, 403)

    def test_rebuilding_keeps_the_opening_credit(self):
        self.adjust(self.q3, "opening_credit", "100.00", "Credit from the old accounting system")
        self.settings_patch(frequency="monthly")
        july = VatPeriod.objects.get(start_date=date(2026, 7, 1))
        self.assertEqual((july.frequency, july.opening_credit), ("monthly", D("100.00")))
