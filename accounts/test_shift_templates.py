import time
from datetime import datetime, time as day_time, timezone as datetime_timezone
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Shift, ShiftTemplate, Staff, StaffRole
from branches.models import Branch


class ShiftTemplateApiTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(
            name_en="Main", name_ar="Main", timezone="Asia/Riyadh",
            latitude="0", longitude="0",
        )
        self.other_branch = Branch.objects.create(name_en="Other", name_ar="Other")
        self.owner = self.make_staff(self.branch, "Owner", StaffRole.OWNER)
        self.member = self.make_staff(self.branch, "Member", StaffRole.CASHIER)
        self.other_member = self.make_staff(self.other_branch, "Other member", StaffRole.CASHIER)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    @staticmethod
    def make_staff(branch, name, role):
        staff = Staff(branch=branch, name=name, role=role, pin_hash="unused")
        staff.save()
        return staff

    def test_create_edit_activate_and_assign_overnight_template(self):
        response = self.client.post("/api/shift-templates/", {
            "name": "Evening", "start_time": "16:00:00", "end_time": "01:00:00",
            "active": True, "assigned_staff_ids": [self.member.pk],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        template_id = response.data["id"]
        self.assertTrue(response.data["crosses_midnight"])
        self.assertEqual([member["id"] for member in response.data["assigned_staff"]], [self.member.pk])
        self.member.refresh_from_db()
        self.assertEqual(self.member.shift_template_id, template_id)

        response = self.client.patch(f"/api/shift-templates/{template_id}/", {
            "name": "Evening Updated", "active": False,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["active"])
        self.assertEqual(len(response.data["assigned_staff"]), 1)

    def test_shift_template_assignments_and_visibility_are_branch_scoped(self):
        response = self.client.post("/api/shift-templates/", {
            "name": "Morning", "start_time": "09:00:00", "end_time": "17:00:00",
            "assigned_staff_ids": [self.other_member.pk],
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/shift-templates/").data["results"], [])
        other = ShiftTemplate.objects.create(
            branch=self.other_branch, name="Other Shift",
            start_time=day_time(9), end_time=day_time(17),
        )
        self.assertEqual(self.client.get(f"/api/shift-templates/{other.pk}/").status_code, 404)

    def test_attendance_settings_defaults_and_manager_only_write(self):
        url = "/api/branches/active/attendance/"
        self.assertEqual(self.client.get(url).data, {
            "max_clock_ins_per_day": 1,
            "max_clock_outs_per_day": 1,
            "late_threshold_minutes": 15,
        })
        response = self.client.patch(url, {
            "max_clock_ins_per_day": 2, "max_clock_outs_per_day": 3,
            "late_threshold_minutes": 7,
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["late_threshold_minutes"], 7)
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.patch(url, {"late_threshold_minutes": 1}, format="json").status_code, 403)


class AttendanceRuleApiTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(
            name_en="Main", name_ar="Main", timezone="Asia/Riyadh",
            latitude="0", longitude="0",
        )
        self.staff = ShiftTemplateApiTests.make_staff(self.branch, "Staff", StaffRole.CASHIER)
        self.client = APIClient()
        self.client.force_authenticate(self.staff)
        self.url = f"/api/staff/{self.staff.pk}"

    @staticmethod
    def location(timestamp=None):
        return {"latitude": 0, "longitude": 0, "accuracy_m": 1,
                "location_timestamp": time.time() if timestamp is None else timestamp}

    def test_daily_clock_in_and_clock_out_limits(self):
        self.assertEqual(self.client.post(f"{self.url}/clock-in/", self.location(), format="json").status_code, 201)
        self.assertEqual(self.client.post(f"{self.url}/clock-out/", self.location(), format="json").status_code, 200)
        response = self.client.post(f"{self.url}/clock-in/", self.location(), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "error.maxDailyClockInsReached")

        self.branch.max_clock_ins_per_day = 2
        self.branch.save(update_fields=["max_clock_ins_per_day", "updated_at"])
        self.assertEqual(self.client.post(f"{self.url}/clock-in/", self.location(), format="json").status_code, 201)
        response = self.client.post(f"{self.url}/clock-out/", self.location(), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "error.maxDailyClockOutsReached")

    def test_default_and_custom_late_thresholds_are_snapshotted_at_clock_in(self):
        template = ShiftTemplate.objects.create(
            branch=self.branch, name="Morning", start_time=day_time(9), end_time=day_time(17),
        )
        self.staff.shift_template = template
        self.staff.save(update_fields=["shift_template", "updated_at"])
        self.branch.late_threshold_minutes = 15
        self.branch.save(update_fields=["late_threshold_minutes", "updated_at"])

        # 09:10 local is within the default 15-minute grace period.
        first_now = datetime(2026, 10, 6, 6, 10, tzinfo=datetime_timezone.utc)
        with patch("accounts.views.timezone.now", return_value=first_now):
            on_time = self.client.post(f"{self.url}/clock-in/", self.location(first_now.timestamp()), format="json")
        self.assertEqual(on_time.status_code, 201, on_time.data)
        self.assertFalse(on_time.data["is_late"])
        clockout_now = datetime(2026, 10, 6, 6, 11, tzinfo=datetime_timezone.utc)
        with patch("accounts.views.timezone.now", return_value=clockout_now):
            self.client.post(f"{self.url}/clock-out/", self.location(clockout_now.timestamp()), format="json")

        self.branch.late_threshold_minutes = 5
        self.branch.max_clock_ins_per_day = 2
        self.branch.max_clock_outs_per_day = 2
        self.branch.save(update_fields=["late_threshold_minutes", "max_clock_ins_per_day", "max_clock_outs_per_day", "updated_at"])
        with patch("accounts.views.timezone.now", return_value=first_now):
            late = self.client.post(f"{self.url}/clock-in/", self.location(first_now.timestamp()), format="json")
        self.assertEqual(late.status_code, 201, late.data)
        self.assertTrue(late.data["is_late"])

    def test_overnight_clock_in_after_midnight_uses_previous_start_date(self):
        template = ShiftTemplate.objects.create(
            branch=self.branch, name="Evening", start_time=day_time(16), end_time=day_time(1),
        )
        self.staff.shift_template = template
        self.staff.save(update_fields=["shift_template", "updated_at"])
        overnight_now = datetime(2026, 10, 6, 21, 30, tzinfo=datetime_timezone.utc)
        with patch("accounts.views.timezone.now", return_value=overnight_now):
            response = self.client.post(f"{self.url}/clock-in/", self.location(overnight_now.timestamp()), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["is_late"])
