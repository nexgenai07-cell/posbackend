"""VAT dashboard M7: the history table, its CSV and PDF, and the PDF of one
period."""

import csv
import io
from datetime import date

from django.test import TestCase

from zatca.environments import Environment
from zatca.models import ZatcaSettings

from .models import VatAuditLog, VatPayment
from .test_figures import VatFixture, line, riyadh

CARD_FIELDS = ("output_vat", "input_vat", "credit_in", "credit_source", "vat_due", "zatca_amount", "paid", "balance")


class VatHistoryTests(VatFixture, TestCase):
    def file(self, period, amount, **body):
        response = self.api.post(f"/api/vat/periods/{period.pk}/file/",
                                 {"filed_on": "2026-10-20", "zatca_amount": amount, "confirm_warnings": True, **body},
                                 format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def pay(self, period, amount, reference):
        response = self.api.post(f"/api/vat/periods/{period.pk}/payments/",
                                 {"amount": amount, "paid_on": "2026-10-25", "sadad_reference": reference},
                                 format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def scenario(self):
        """Q3: VAT 150 filed and paid (plus a voided payment). Q4: a purchase, not filed."""
        self.sale(riyadh(2026, 8, 1, 12), [line("S", "1000.00", "150.00")])
        self.file(self.q3, "150.00")
        self.pay(self.q3, "150.00", "SADAD-1")
        self.pay(self.q3, "10.00", "WRONG")
        wrong = VatPayment.objects.get(sadad_reference="WRONG")
        self.api.post(f"/api/vat/payments/{wrong.pk}/void/", {"reason": "Entered by mistake"}, format="json")
        self.purchase_doc(date(2026, 10, 10), "100.00", "15.00")

    def history(self):
        response = self.api.get("/api/vat/history/")
        self.assertEqual(response.status_code, 200)
        return response.data["rows"]

    def test_each_row_equals_its_periods_cards(self):
        self.purchase_doc(date(2026, 8, 10), "100.00", "15.00")
        self.file(self.q3, "-14.00", difference_reason="ZATCA disallowed part of it")
        self.sale(riyadh(2026, 10, 5, 12), [line("S", "100.00", "15.00")])
        rows = self.history()
        self.assertEqual([row["id"] for row in rows], [self.q4.pk, self.q3.pk])  # newest first
        for row in rows:
            cards = self.api.get(f"/api/vat/periods/{row['id']}/").data["figures"]["cards"]
            self.assertEqual({field: row[field] for field in CARD_FIELDS},
                             {field: cards[field] for field in CARD_FIELDS}, row["name"])

    def test_rows(self):
        self.scenario()
        q4, q3 = self.history()
        self.assertEqual((q3["name"], q3["status"], q3["paid"], q3["balance"], q3["sadad_references"]),
                         ("Q3 2026", "paid", 150, 0, ["SADAD-1"]))
        self.assertEqual((q4["name"], q4["status"], q4["vat_due"], q4["credit_source"], q4["filed_on"]),
                         ("Q4 2026", "open", -15, "filed", None))

    def test_csv(self):
        self.scenario()
        response = self.api.get("/api/vat/history/csv/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="vat-history-sandbox.csv"')
        text = response.content.decode("utf-8")
        self.assertTrue(text.startswith("﻿Restaurant,Burger House / بيت البرجر\r\n"))
        lines = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        self.assertEqual([row[0] for row in lines[:5]], ["Restaurant", "VAT number", "Report", "Generated", "Environment"])
        self.assertEqual(lines[5], [])
        self.assertEqual(lines[6][:3], ["Period", "From", "To"])
        self.assertEqual(lines[7:], [
            ["Q3 2026", "2026-07-01", "2026-09-30", "Quarterly", "2026-10-31", "150.00", "0.00", "0.00", "150.00",
             "150.00", "150.00", "0.00", "2026-10-20", "SADAD-1", "Paid"],
            ["Q4 2026", "2026-10-01", "2026-12-31", "Quarterly", "2027-01-31", "0.00", "15.00", "0.00", "-15.00",
             "", "0.00", "0.00", "", "", "Not filed"],
            ["TOTALS", "", "", "", "", "150.00", "15.00", "", "", "", "150.00", "", "", "", ""],
        ])
        log = VatAuditLog.objects.get(action="history_exported")
        self.assertEqual((log.actor, log.data), (self.owner, {"format": "csv", "periods": 2}))

    def test_history_pdf(self):
        self.scenario()
        response = self.api.get("/api/vat/history/pdf/")
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "application/pdf"))
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertTrue(VatAuditLog.objects.filter(action="history_exported", data__format="pdf").exists())

    def test_period_pdf(self):
        self.scenario()
        response = self.api.get(f"/api/vat/periods/{self.q3.pk}/pdf/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="vat-return-2026-07-01-2026-09-30.pdf"')
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertTrue(VatAuditLog.objects.filter(action="period_pdf_exported", period=self.q3).exists())
        # An unfiled period exports too.
        self.assertEqual(self.api.get(f"/api/vat/periods/{self.q4.pk}/pdf/").status_code, 200)

    def test_production_history_leaves_out_practice_periods(self):
        self.scenario()
        ZatcaSettings.objects.filter(pk=self.settings.pk).update(environment=Environment.PRODUCTION)
        rows = self.history()
        self.assertEqual(len(rows), 2)
        self.assertNotIn(self.q3.pk, [row["id"] for row in rows])
        self.assertEqual({row["is_test"] for row in rows}, {False})
        self.assertEqual({row["status"] for row in rows}, {"open"})

    def test_access(self):
        self.api.force_authenticate(self.manager)
        for url in ("/api/vat/history/", "/api/vat/history/csv/", "/api/vat/history/pdf/",
                    f"/api/vat/periods/{self.q3.pk}/pdf/"):
            self.assertEqual(self.api.get(url).status_code, 200, url)
        self.api.force_authenticate(self.cashier)
        for url in ("/api/vat/history/", "/api/vat/history/csv/", f"/api/vat/periods/{self.q3.pk}/pdf/"):
            self.assertEqual(self.api.get(url).status_code, 403, url)
