"""VAT dashboard M3: VAT filing settings and periods (dates, due dates,
days left, rebuilding, environments, access)."""

from datetime import date
from unittest import mock

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from accounts.models import StaffRole
from branches.models import Branch
from zatca.environments import Environment
from zatca.models import ZatcaSettings
from zatca.tests import make_staff

from . import periods
from .models import VatAuditLog, VatPeriod

TODAY = date(2026, 10, 29)


class PeriodDateTests(SimpleTestCase):
    def test_period_end(self):
        self.assertEqual(periods.period_end(date(2026, 8, 17), "quarterly"), date(2026, 9, 30))
        self.assertEqual(periods.period_end(date(2026, 8, 17), "monthly"), date(2026, 8, 31))
        self.assertEqual(periods.period_end(date(2026, 1, 1), "quarterly"), date(2026, 3, 31))
        self.assertEqual(periods.period_end(date(2028, 2, 1), "monthly"), date(2028, 2, 29))

    def test_due_date_is_the_last_day_of_the_next_month(self):
        self.assertEqual(periods.due_date(date(2026, 9, 30)), date(2026, 10, 31))
        self.assertEqual(periods.due_date(date(2026, 12, 31)), date(2027, 1, 31))
        self.assertEqual(periods.due_date(date(2027, 1, 31)), date(2027, 2, 28))


@mock.patch("vat_returns.periods.today", return_value=TODAY)
class VatPeriodsApiTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Burger House", name_ar="بيت البرجر")
        self.settings = ZatcaSettings.for_branch(self.branch)
        self.owner = make_staff(self.branch, "Owner", StaffRole.OWNER)
        self.manager = make_staff(self.branch, "Manager", StaffRole.MANAGER)
        self.cashier = make_staff(self.branch, "Cashier", StaffRole.CASHIER)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def patch(self, **body):
        return self.api.patch("/api/vat/settings/", body, format="json")

    def periods(self):
        return self.api.get("/api/vat/periods/").data

    def test_no_periods_until_a_start_date_is_set(self, _today):
        data = self.periods()
        self.assertEqual((data["setup_needed"], data["periods"], data["current"]), (True, [], None))

    def test_quarterly_periods_from_a_mid_quarter_start(self, _today):
        response = self.patch(registration_date="2026-01-01", tracking_start="2026-08-17")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["first_period_start"], date(2026, 8, 17))

        data = self.periods()
        q4, q3 = data["periods"]  # newest first
        self.assertEqual((q3["start_date"], q3["end_date"], q3["due_date"], q3["days_left"]),
                         (date(2026, 8, 17), date(2026, 9, 30), date(2026, 10, 31), 2))
        self.assertEqual(q3["label"], {"year": 2026, "quarter": 3, "month": None, "partial_from": "2026-08-17"})
        self.assertEqual((q3["has_ended"], q3["is_current"], q3["status"]), (True, False, "open"))
        self.assertEqual((q4["start_date"], q4["end_date"], q4["due_date"], q4["days_left"]),
                         (date(2026, 10, 1), date(2026, 12, 31), date(2027, 1, 31), 94))
        self.assertEqual(q4["label"]["partial_from"], None)
        self.assertEqual(data["current"], q4["id"])
        self.assertTrue(q4["is_test"])  # a fresh branch is in Sandbox

    def test_the_later_of_the_two_dates_starts_the_first_period(self, _today):
        self.patch(registration_date="2026-09-05", tracking_start="2026-08-01")
        self.assertEqual(self.periods()["periods"][-1]["start_date"], date(2026, 9, 5))

    def test_switching_to_monthly_rebuilds_the_periods_and_is_audited(self, _today):
        self.patch(tracking_start="2026-08-17")
        self.patch(frequency="monthly")
        data = self.periods()
        self.assertEqual([(p["start_date"], p["end_date"]) for p in reversed(data["periods"])], [
            (date(2026, 8, 17), date(2026, 8, 31)), (date(2026, 9, 1), date(2026, 9, 30)),
            (date(2026, 10, 1), date(2026, 10, 31)),
        ])
        self.assertEqual(data["periods"][0]["label"], {"year": 2026, "quarter": None, "month": 10,
                                                       "partial_from": None})
        actions = list(VatAuditLog.objects.order_by("id").values_list("action", flat=True))
        self.assertEqual(actions, ["settings_changed", "periods_rebuilt", "settings_changed", "periods_rebuilt"])
        # The log is ordered newest first, so .first() is the frequency switch.
        change = VatAuditLog.objects.filter(action="settings_changed").first()
        self.assertEqual(change.actor, self.owner)
        self.assertEqual(change.data, {"before": {"vat_filing_frequency": "quarterly"},
                                       "after": {"vat_filing_frequency": "monthly"}})

    def test_reminder_days_change_doesnt_touch_periods(self, _today):
        self.patch(tracking_start="2026-08-17")
        self.periods()
        ids = list(VatPeriod.objects.values_list("id", flat=True))
        self.assertEqual(self.patch(reminder_days=7).data["reminder_days"], 7)
        self.assertEqual(list(VatPeriod.objects.values_list("id", flat=True)), ids)

    def test_validation(self, _today):
        response = self.patch(frequency="weekly", tracking_start="2026-11-01", reminder_days=0)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"frequency": "error.vatFrequencyInvalid",
                                         "tracking_start": "error.vatTrackingStartInFuture",
                                         "reminder_days": "error.vatReminderDaysInvalid"})
        self.assertEqual(self.patch(registration_date="not a date").data,
                         {"registration_date": "error.dateInvalid"})

    def test_filed_periods_keep_their_dates(self, _today):
        self.patch(tracking_start="2026-08-17")
        self.periods()
        VatPeriod.objects.filter(start_date=date(2026, 8, 17)).update(locked=True)
        logs = VatAuditLog.objects.count()
        response = self.patch(tracking_start="2026-08-20")
        self.assertEqual((response.status_code, response.data["error"]), (409, "error.vatPeriodsFiled"))
        # A frequency change now needs an effective date (test_late_amend.py).
        response = self.patch(frequency="monthly")
        self.assertEqual((response.status_code, response.data),
                         (400, {"frequency_effective_from": "error.vatSwitchDateRequired"}))
        self.settings.refresh_from_db()
        self.assertEqual((self.settings.vat_filing_frequency, self.settings.vat_tracking_start),
                         ("quarterly", date(2026, 8, 17)))
        self.assertEqual(VatPeriod.objects.count(), 2)
        self.assertEqual(VatAuditLog.objects.count(), logs)  # the refused change left nothing behind

    def test_each_environment_has_its_own_periods(self, _today):
        self.patch(tracking_start="2026-08-17")
        sandbox_ids = {p["id"] for p in self.periods()["periods"]}
        ZatcaSettings.objects.filter(pk=self.settings.pk).update(environment=Environment.PRODUCTION)
        production = self.periods()["periods"]
        self.assertFalse(sandbox_ids & {p["id"] for p in production})
        self.assertEqual([p["is_test"] for p in production], [False, False])
        # Sandbox periods are kept, just not listed in Production.
        self.assertEqual(VatPeriod.objects.filter(environment=Environment.SANDBOX).count(), 2)
        response = self.api.get(f"/api/vat/periods/{min(sandbox_ids)}/")
        self.assertEqual(response.status_code, 404)

    def test_periods_are_created_once(self, _today):
        self.patch(tracking_start="2026-08-17")
        self.periods()
        self.periods()
        self.assertEqual(VatPeriod.objects.count(), 2)

    def test_access(self, _today):
        self.patch(tracking_start="2026-08-17")
        self.api.force_authenticate(self.manager)
        self.assertEqual(self.api.get("/api/vat/periods/").status_code, 200)
        self.assertEqual(self.api.get("/api/vat/settings/").status_code, 200)
        self.assertEqual(self.patch(frequency="monthly").status_code, 403)
        self.api.force_authenticate(self.cashier)
        for url in ("/api/vat/periods/", "/api/vat/settings/"):
            self.assertEqual(self.api.get(url).status_code, 403, url)
        period = VatPeriod.objects.first()
        self.assertEqual(self.api.get(f"/api/vat/periods/{period.pk}/").status_code, 403)
