"""VAT dashboard M8: reminder banners (Production only)."""

from datetime import date
from unittest import mock

from django.test import TestCase

from zatca.environments import Environment
from zatca.models import ZatcaDevice, ZatcaSettings

from .models import VatPeriod
from .test_figures import VatFixture, line, riyadh


class VatRemindersTests(VatFixture, TestCase):
    """"Today" is 29 Oct 2026: Q3 is due on 31 Oct (2 days left)."""

    def go_live(self):
        ZatcaSettings.objects.filter(pk=self.settings.pk).update(environment=Environment.PRODUCTION)
        self.device = ZatcaDevice.objects.create(branch=self.branch, environment=Environment.PRODUCTION)
        self.api.get("/api/vat/periods/")
        self.q3 = VatPeriod.objects.get(environment=Environment.PRODUCTION, start_date=date(2026, 7, 1))

    def reminders(self):
        response = self.api.get("/api/vat/reminders/")
        self.assertEqual(response.status_code, 200)
        return response.data["reminders"]

    def file(self, amount, **body):
        response = self.api.post(f"/api/vat/periods/{self.q3.pk}/file/",
                                 {"filed_on": "2026-10-20", "zatca_amount": amount, "confirm_warnings": True, **body},
                                 format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_no_reminders_for_practice_periods(self):
        self.sale(riyadh(2026, 8, 1, 12), [line("S", "1000.00", "150.00")])
        self.assertEqual(self.reminders(), [])

    def test_due_soon_while_not_filed(self):
        self.go_live()
        self.sale(riyadh(2026, 8, 1, 12), [line("S", "1000.00", "150.00")])
        (reminder,) = self.reminders()
        self.assertEqual(
            {key: reminder[key] for key in ("id", "name", "due_date", "days_left", "level", "status", "balance")},
            {"id": self.q3.pk, "name": "Q3 2026", "due_date": date(2026, 10, 31), "days_left": 2,
             "level": "due_soon", "status": "open", "balance": 150})

    def test_overdue_after_the_due_date(self):
        self.go_live()
        with mock.patch("vat_returns.periods.today", return_value=date(2026, 11, 5)):
            (reminder,) = self.reminders()
        self.assertEqual((reminder["level"], reminder["days_left"]), ("overdue", -5))

    def test_filed_reminds_until_paid(self):
        self.go_live()
        self.sale(riyadh(2026, 8, 1, 12), [line("S", "1000.00", "150.00")])
        self.file("150.00")
        (reminder,) = self.reminders()
        self.assertEqual((reminder["status"], reminder["balance"]), ("filed", 150))
        self.api.post(f"/api/vat/periods/{self.q3.pk}/payments/",
                      {"amount": "150.00", "paid_on": "2026-10-25", "sadad_reference": "SADAD-1"}, format="json")
        self.assertEqual(self.reminders(), [])

    def test_a_credit_period_stops_reminding_once_filed(self):
        self.go_live()
        self.assertEqual(len(self.reminders()), 1)  # nothing to pay, but still to file
        self.file("0.00")
        self.assertEqual(self.reminders(), [])

    def test_reminder_days(self):
        self.go_live()
        self.api.patch("/api/vat/settings/", {"reminder_days": 1}, format="json")
        self.assertEqual(self.reminders(), [])

    def test_access(self):
        self.api.force_authenticate(self.manager)
        self.assertEqual(self.api.get("/api/vat/reminders/").status_code, 200)
        self.api.force_authenticate(self.cashier)
        self.assertEqual(self.api.get("/api/vat/reminders/").status_code, 403)
