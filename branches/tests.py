from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from .models import Branch


class BranchLocationSettingsTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Test", name_ar="Test")
        self.owner = Staff.objects.create(branch=self.branch, name="Owner", role=StaffRole.OWNER, pin_hash="x")
        self.manager = Staff.objects.create(branch=self.branch, name="Manager", role=StaffRole.MANAGER, pin_hash="x")
        self.client = APIClient()
        self.url = "/api/branches/active/location/"

    def test_radius_defaults_to_two_meters(self):
        self.assertEqual(self.branch.geofence_radius_m, Decimal("2"))

    def test_owner_and_manager_can_read_and_update_location(self):
        for staff in (self.owner, self.manager):
            self.client.force_authenticate(user=staff)
            response = self.client.patch(self.url, {
                "latitude": "24.713600", "longitude": "46.675300", "geofence_radius_m": "2.00",
            }, format="json")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_non_managers_cannot_read_or_change_location(self):
        for role in (StaffRole.CASHIER, StaffRole.WAITER, StaffRole.KITCHEN):
            staff = Staff.objects.create(branch=self.branch, name=role, role=role, pin_hash="x")
            self.client.force_authenticate(user=staff)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.patch(self.url, {"latitude": 24}, format="json").status_code, 403)

    def test_general_branch_response_does_not_expose_exact_coordinates(self):
        cashier = Staff.objects.create(branch=self.branch, name="Cashier", role=StaffRole.CASHIER, pin_hash="x")
        self.client.force_authenticate(user=cashier)
        response = self.client.get("/api/branches/active/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("latitude", response.data)
        self.assertNotIn("longitude", response.data)

    def test_invalid_coordinates_radius_and_unpaired_coordinates_are_rejected(self):
        self.client.force_authenticate(user=self.owner)
        invalid_payloads = (
            {"latitude": "90.1", "longitude": "0"},
            {"latitude": "0", "longitude": "-180.1"},
            {"latitude": "0", "longitude": "0", "geofence_radius_m": "0"},
            {"latitude": "24"},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.patch(self.url, payload, format="json").status_code, 400)
