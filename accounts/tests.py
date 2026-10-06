from django.core.cache import cache
from math import degrees
import time
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from branches.models import Branch

from .models import Staff, StaffRole


def make_staff(branch, name, role, pin, is_active=True):
    staff = Staff(branch=branch, name=name, role=role, is_active=is_active)
    staff.set_pin(pin)
    staff.save()
    return staff


class AccountsApiTestCase(TestCase):
    """Shared fixtures. Two branches on purpose — the login-choices fallback
    ("no ?branch= given") has to be provably *not* "the only row"."""

    @classmethod
    def setUpTestData(cls):
        cls.riyadh = Branch.objects.create(
            name_en="Smoke & Char — Riyadh",
            name_ar="سموك آند تشار — الرياض",
            address="King Fahd Road, Riyadh",
            timezone="Asia/Riyadh",
            currency="SAR",
            latitude="0.000000", longitude="0.000000",
        )
        cls.jeddah = Branch.objects.create(
            name_en="Smoke & Char — Jeddah",
            name_ar="سموك آند تشار — جدة",
            address="Tahlia Street, Jeddah",
            timezone="Asia/Riyadh",
            currency="SAR",
        )
        cls.owner = make_staff(cls.riyadh, "Amir", StaffRole.OWNER, "1111")
        cls.cashier = make_staff(cls.riyadh, "Junaid", StaffRole.CASHIER, "3333")
        cls.retired = make_staff(cls.riyadh, "Old Timer", StaffRole.MANAGER, "9999", is_active=False)
        cls.jeddah_owner = make_staff(cls.jeddah, "Faisal", StaffRole.OWNER, "5555")

    def setUp(self):
        self.client = APIClient()
        # Throttle history lives in the shared LocMemCache and is *not* reset
        # between tests, so pin-login's 10/min limit would otherwise leak
        # across the classes in this file and start 429ing a suite that never
        # issues 10 requests to any one endpoint.
        cache.clear()

    def pin_login(self, staff_id, pin):
        return self.client.post("/api/auth/pin-login/", {"staff_id": staff_id, "pin": pin}, format="json")


def error_code(response):
    """
    The API's convention is `{"error": "<code>"}` (see common/exceptions.py and
    every view-level Response). A *serializer*-level ValidationError goes
    through DRF's own error detail collector, which wraps the value in a list:
    `{"error": ["error.invalidCredentials"]}`. Both shapes are real, and the
    frontend's client normalises them (see src/lib/api/client.ts extractCode) —
    this helper does the same so assertions don't encode the difference.
    """
    value = response.json()["error"]
    return value[0] if isinstance(value, list) else value


class LoginChoicesTests(AccountsApiTestCase):
    url = "/api/auth/login-choices/"

    def test_is_public(self):
        """The whole point of this endpoint: it must work with no token, since
        the login screen can't have one yet."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_returns_default_branch_and_its_active_staff(self):
        body = self.client.get(self.url).json()
        self.assertEqual(body["branch"]["id"], self.riyadh.id)
        self.assertEqual(body["branch"]["name_en"], "Smoke & Char — Riyadh")
        self.assertEqual(body["branch"]["currency"], "SAR")
        self.assertEqual({row["name"] for row in body["staff"]}, {"Amir", "Junaid"})

    def test_excludes_inactive_and_other_branches_staff(self):
        names = [row["name"] for row in self.client.get(self.url).json()["staff"]]
        self.assertNotIn("Old Timer", names)  # is_active=False
        self.assertNotIn("Faisal", names)  # a different branch

    def test_never_exposes_credentials_or_internal_fields(self):
        row = self.client.get(self.url).json()["staff"][0]
        self.assertEqual(set(row), {"id", "name", "role"})

    def test_branch_query_param_selects_that_branch(self):
        body = self.client.get(self.url, {"branch": self.jeddah.id}).json()
        self.assertEqual(body["branch"]["id"], self.jeddah.id)
        self.assertEqual([row["name"] for row in body["staff"]], ["Faisal"])

    def test_unknown_branch_404s(self):
        response = self.client.get(self.url, {"branch": 999999})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_no_branches_at_all_is_a_flagged_404(self):
        """Not reachable in production (a branch is seeded first), but the
        frontend needs a code rather than a 500 if it ever is."""
        Branch.objects.all().delete()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json()["error"], "error.noBranch")


class PinLoginTests(AccountsApiTestCase):
    url = "/api/auth/pin-login/"

    def test_success_returns_tokens_and_staff(self):
        body = self.pin_login(self.owner.id, "1111").json()
        self.assertTrue(body["access"])
        self.assertTrue(body["refresh"])
        self.assertEqual(body["staff"]["id"], self.owner.id)
        self.assertEqual(body["staff"]["role"], StaffRole.OWNER)
        # StaffSerializer's `pin` is write_only — the credential must never
        # come back out, not even hashed.
        self.assertNotIn("pin", body["staff"])

    def test_wrong_pin_is_rejected(self):
        response = self.pin_login(self.owner.id, "0000")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(error_code(response), "error.invalidCredentials")

    def test_unknown_staff_id_is_rejected_with_the_same_code(self):
        """Same code as a wrong PIN on purpose — otherwise the response itself
        tells an attacker which staff ids exist."""
        response = self.pin_login(999999, "1111")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(error_code(response), "error.invalidCredentials")

    def test_inactive_staff_cannot_log_in(self):
        response = self.pin_login(self.retired.id, "9999")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(error_code(response), "error.invalidCredentials")

    def test_one_branchs_pin_does_not_open_another_branchs_staff(self):
        """Same PIN string, different branch's staff id."""
        response = self.pin_login(self.jeddah_owner.id, "1111")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_access_token_authenticates_subsequent_requests(self):
        access = self.pin_login(self.owner.id, "1111").json()["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        response = self.client.get("/api/branches/active/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["id"], self.riyadh.id)

    def test_refresh_mints_a_new_access_token(self):
        refresh = self.pin_login(self.owner.id, "1111").json()["refresh"]
        response = self.client.post("/api/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["access"])

    def test_refresh_rejects_a_bogus_token(self):
        response = self.client.post("/api/auth/refresh/", {"refresh": "not-a-token"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class StaffAccessGateTests(AccountsApiTestCase):
    """Guards the existing role gate. The login screen's staff *picker* is
    public (login-choices), but the real staff resource must stay
    owner/manager-only — easy to loosen by accident while wiring the former."""

    url = "/api/staff/"

    def auth_as(self, staff, pin):
        access = self.pin_login(staff.id, pin).json()["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_cashier_is_forbidden(self):
        self.auth_as(self.cashier, "3333")
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_sees_only_their_own_branch(self):
        self.auth_as(self.owner, "1111")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [row["name"] for row in response.json()["results"]]
        self.assertIn("Amir", names)
        self.assertNotIn("Faisal", names)

    def test_anonymous_is_unauthorized(self):
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_requires_a_token(self):
        self.assertEqual(self.client.post("/api/auth/logout/").status_code, status.HTTP_401_UNAUTHORIZED)


class AttendanceGeofenceTests(AccountsApiTestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_staff(self.riyadh, "Employee", StaffRole.CASHIER, "4444")
        self.url = f"/api/staff/{self.staff.pk}"

    @staticmethod
    def fix(latitude=0, longitude=0, accuracy=0.1, timestamp=None):
        return {"latitude": latitude, "longitude": longitude, "accuracy_m": accuracy,
                "location_timestamp": timestamp if timestamp is not None else time.time()}

    def test_check_in_and_out_within_configured_radius_succeed(self):
        self.client.force_authenticate(user=self.staff)
        within = self.fix(latitude=degrees(1 / 6_371_000))
        self.assertEqual(self.client.post(f"{self.url}/clock-in/", within, format="json").status_code, 201)
        self.assertEqual(self.client.post(f"{self.url}/clock-out/", self.fix(), format="json").status_code, 200)

    def test_check_in_and_out_outside_radius_fail_without_mutation(self):
        self.client.force_authenticate(user=self.staff)
        # The branch's attendance radius defaults to 100 m; move clearly
        # beyond that configured limit rather than beyond the separate 2 m QR radius.
        outside = self.fix(latitude=degrees(103 / 6_371_000))
        response = self.client.post(f"{self.url}/clock-in/", outside, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.staff.shifts.exists())

        self.client.post(f"{self.url}/clock-in/", self.fix(), format="json")
        response = self.client.post(f"{self.url}/clock-out/", outside, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.staff.shifts.get().clock_out)

    def test_missing_invalid_stale_and_unconfigured_locations_fail_closed(self):
        self.client.force_authenticate(user=self.staff)
        for payload in ({}, self.fix(latitude=91), self.fix(timestamp=time.time() - 180)):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(f"{self.url}/clock-in/", payload, format="json").status_code, 400)
        self.riyadh.latitude = None
        self.riyadh.longitude = None
        self.riyadh.save(update_fields=["latitude", "longitude", "updated_at"])
        self.assertEqual(self.client.post(f"{self.url}/clock-in/", self.fix(), format="json").status_code, 400)

