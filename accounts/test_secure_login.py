"""
Secure login (docs/secure-login-plan.md): usernames, password rules,
the 3-strikes lockout, Owner-only unlock/reset, forced password change
and the PIN -> password migration contract.
"""

from io import StringIO
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework import status

from .models import Staff, StaffRole
from .services import authenticate_staff, LoginRejected
from .tests import AccountsApiTestCase, error_code, make_staff
from .usernames import unique_username, username_from_name


class UsernameGenerationTests(AccountsApiTestCase):
    def test_slug_rules(self):
        self.assertEqual(username_from_name("Ali Khan"), "ali.khan")
        self.assertEqual(username_from_name("  Zoë  O'Brien "), "zoe.obrien")
        self.assertEqual(username_from_name("Al"), "alstaff")
        # A name with no transliterable characters falls back, not blank.
        self.assertEqual(username_from_name("أحمد"), "staff")

    def test_duplicates_get_a_numeric_suffix(self):
        taken = {"ali.khan", "ali.khan2"}
        self.assertEqual(unique_username("Ali Khan", taken.__contains__), "ali.khan3")

    def test_saving_without_a_username_generates_one_from_the_name(self):
        first = make_staff(self.riyadh, "Ali Khan", StaffRole.WAITER, "waiter-secret-1")
        second = make_staff(self.jeddah, "Ali Khan", StaffRole.WAITER, "waiter-secret-2")
        self.assertEqual(first.username, "ali.khan")
        self.assertEqual(second.username, "ali.khan2")

    def test_username_is_stored_lowercase(self):
        staff = make_staff(self.riyadh, "Sara", StaffRole.WAITER, "sara-secret-1", username="  Sara.K ")
        self.assertEqual(staff.username, "sara.k")


class LockoutTests(AccountsApiTestCase):
    def test_three_wrong_passwords_lock_the_account(self):
        first = self.login("junaid", "wrong-1")
        second = self.login("junaid", "wrong-2")
        third = self.login("junaid", "wrong-3")
        self.assertEqual((first.status_code, first.json()["attempts_left"]), (400, 2))
        self.assertEqual((second.status_code, second.json()["attempts_left"]), (400, 1))
        self.assertEqual(third.status_code, status.HTTP_423_LOCKED)
        self.assertEqual(error_code(third), "error.accountLocked")
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.is_locked)
        self.assertEqual(self.cashier.locked_reason, Staff.LOCK_REASON_FAILED_LOGINS)
        self.assertEqual(self.cashier.failed_login_attempts, 3)

    def test_correct_password_while_locked_is_still_refused(self):
        for attempt in range(3):
            self.login("junaid", f"wrong-{attempt}")
        response = self.login("junaid", "junaid-secret-3")
        self.assertEqual(response.status_code, status.HTTP_423_LOCKED)
        self.assertEqual(error_code(response), "error.accountLocked")

    def test_counter_resets_on_a_correct_password(self):
        self.login("junaid", "wrong-1")
        self.login("junaid", "wrong-2")
        self.assertEqual(self.login("junaid", "junaid-secret-3").status_code, 200)
        self.cashier.refresh_from_db()
        self.assertEqual(self.cashier.failed_login_attempts, 0)
        # Two more wrong tries are again "2 left", "1 left" - not a lock.
        self.login("junaid", "wrong-3")
        response = self.login("junaid", "wrong-4")
        self.assertEqual(response.json()["attempts_left"], 1)
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.is_locked)

    def test_locking_runs_under_a_row_lock(self):
        """Parallel wrong attempts must not each read "2" and all write "3"
        (or, worse, all read "1" and none lock). The counter is read and
        written under select_for_update inside one transaction; this pins
        that contract, since SQLite can't exercise real contention."""
        with patch.object(Staff.objects, "select_for_update", wraps=Staff.objects.select_for_update) as locked:
            with self.assertRaises(LoginRejected):
                authenticate_staff("junaid", "wrong")
        locked.assert_called_once()

    def test_lock_notifies_every_owner_of_the_branch(self):
        second_owner = make_staff(self.riyadh, "Noor", StaffRole.OWNER, "noor-secret-2")
        with patch("accounts.services.publish_staff_event") as publish:
            for attempt in range(3):
                self.login("junaid", f"wrong-{attempt}")
        recipients = {call.args[0] for call in publish.call_args_list}
        self.assertEqual(recipients, {self.owner.pk, second_owner.pk})
        event_name, payload = publish.call_args.args[1], publish.call_args.args[2]
        self.assertEqual(event_name, "staff:locked")
        self.assertEqual(payload["locked_staff_id"], self.cashier.pk)
        self.assertEqual(payload["staff_name"], "Junaid")
        # Not `staff_id`: the admin app treats that key as "intended recipient".
        self.assertNotIn("staff_id", payload)

    def test_a_locked_staff_members_existing_token_is_rejected(self):
        access = self.login("junaid", "junaid-secret-3").json()["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(self.client.get("/api/branches/active/").status_code, 200)
        self.cashier.lock(Staff.LOCK_REASON_FAILED_LOGINS)
        self.cashier.save()
        response = self.client.get("/api/branches/active/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.json()["error"], "error.notAuthenticated")

    def test_a_locked_staff_members_refresh_token_is_rejected(self):
        refresh = self.login("junaid", "junaid-secret-3").json()["refresh"]
        self.cashier.lock(Staff.LOCK_REASON_FAILED_LOGINS)
        self.cashier.save()
        response = self.client.post("/api/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_lock_does_not_deactivate(self):
        for attempt in range(3):
            self.login("junaid", f"wrong-{attempt}")
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.is_active)


class UnlockTests(AccountsApiTestCase):
    def setUp(self):
        super().setUp()
        self.cashier.lock(Staff.LOCK_REASON_FAILED_LOGINS)
        self.cashier.failed_login_attempts = 3
        self.cashier.save()
        self.url = f"/api/staff/{self.cashier.pk}/unlock/"

    def test_owner_can_unlock_and_the_audit_fields_are_set(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["is_locked"])
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.is_locked)
        self.assertEqual(self.cashier.failed_login_attempts, 0)
        self.assertEqual(self.cashier.unlocked_by, self.owner)
        self.assertIsNotNone(self.cashier.unlocked_at)
        # ...and the old password works again.
        self.client.credentials()
        self.client.force_authenticate(user=None)
        self.assertEqual(self.login("junaid", "junaid-secret-3").status_code, 200)

    def test_unlock_with_a_temporary_password_forces_a_change(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(self.url, {"new_password": "temporary-pass-1"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["must_change_password"])
        self.client.force_authenticate(user=None)
        self.assertEqual(self.login("junaid", "junaid-secret-3").status_code, 400)
        self.assertEqual(self.login("junaid", "temporary-pass-1").status_code, 200)

    def test_unlock_temporary_password_must_meet_the_rules(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(self.url, {"new_password": "1234"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(response.json()["new_password"][0], ("error.passwordTooShort", "error.passwordNumeric"))
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.is_locked)

    def test_manager_and_cashier_get_403(self):
        manager = make_staff(self.riyadh, "Mona", StaffRole.MANAGER, "mona-secret-1")
        other_cashier = make_staff(self.riyadh, "Omar", StaffRole.CASHIER, "omar-secret-1")
        for actor in (manager, other_cashier):
            with self.subTest(role=actor.role):
                self.client.force_authenticate(user=actor)
                response = self.client.post(self.url, {}, format="json")
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.is_locked)

    def test_an_owner_of_another_branch_cannot_unlock(self):
        self.client.force_authenticate(user=self.jeddah_owner)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, status.HTTP_404_NOT_FOUND)

    def test_unlocking_an_unlocked_account_is_a_400(self):
        self.cashier.unlock()
        self.cashier.save()
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["error"], "error.accountNotLocked")

    def test_management_command_unlocks(self):
        out = StringIO()
        call_command("unlock_staff", "Junaid", stdout=out)
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.is_locked)
        self.assertIsNone(self.cashier.unlocked_by)
        self.assertIn("unlocked", out.getvalue())

    def test_management_command_with_temporary_password(self):
        call_command("unlock_staff", "junaid", password="temporary-pass-1", stdout=StringIO())
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.check_password("temporary-pass-1"))
        self.assertTrue(self.cashier.must_change_password)

    def test_management_command_rejects_unknown_username_and_weak_password(self):
        with self.assertRaises(CommandError):
            call_command("unlock_staff", "nobody", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("unlock_staff", "junaid", password="1234", stdout=StringIO())
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.is_locked)


class ResetPasswordTests(AccountsApiTestCase):
    def setUp(self):
        super().setUp()
        self.url = f"/api/staff/{self.cashier.pk}/reset-password/"

    def test_owner_sets_a_temporary_password(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(self.url, {"new_password": "temporary-pass-1"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["must_change_password"])
        self.client.force_authenticate(user=None)
        self.assertEqual(self.login("junaid", "temporary-pass-1").status_code, 200)

    def test_password_is_required_and_validated(self):
        self.client.force_authenticate(user=self.owner)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 400)
        response = self.client.post(self.url, {"new_password": "84629173"}, format="json")
        self.assertEqual(response.json()["new_password"], ["error.passwordNumeric"])

    def test_manager_gets_403(self):
        manager = make_staff(self.riyadh, "Mona", StaffRole.MANAGER, "mona-secret-1")
        self.client.force_authenticate(user=manager)
        response = self.client.post(self.url, {"new_password": "temporary-pass-1"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_reset_does_not_unlock(self):
        self.cashier.lock(Staff.LOCK_REASON_FAILED_LOGINS)
        self.cashier.save()
        self.client.force_authenticate(user=self.owner)
        self.client.post(self.url, {"new_password": "temporary-pass-1"}, format="json")
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.is_locked)


class MustChangePasswordTests(AccountsApiTestCase):
    def setUp(self):
        super().setUp()
        self.cashier.must_change_password = True
        self.cashier.save()
        access = self.login("junaid", "junaid-secret-3").json()["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_login_reports_the_flag(self):
        self.client.credentials()
        body = self.login("junaid", "junaid-secret-3").json()
        self.assertTrue(body["staff"]["must_change_password"])

    def test_other_apis_are_blocked_with_a_dedicated_code(self):
        response = self.client.get("/api/branches/active/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["error"], "error.passwordChangeRequired")

    def test_me_and_logout_still_work(self):
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout/").status_code, 205)

    def test_change_password_clears_the_flag_and_opens_the_api(self):
        response = self.client.post(
            "/api/auth/change-password/",
            {"current_password": "junaid-secret-3", "new_password": "my-own-choice-7"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["must_change_password"])
        self.assertEqual(self.client.get("/api/branches/active/").status_code, 200)
        self.client.credentials()
        self.assertEqual(self.login("junaid", "junaid-secret-3").status_code, 400)
        self.assertEqual(self.login("junaid", "my-own-choice-7").status_code, 200)

    def test_change_password_rejections(self):
        url = "/api/auth/change-password/"
        cases = {
            "wrong current": ({"current_password": "nope", "new_password": "my-own-choice-7"}, "current_password", "error.invalidCurrentPassword"),
            "same as current": ({"current_password": "junaid-secret-3", "new_password": "junaid-secret-3"}, "new_password", "error.passwordSameAsCurrent"),
            "too short": ({"current_password": "junaid-secret-3", "new_password": "short"}, "new_password", "error.passwordTooShort"),
            "all digits": ({"current_password": "junaid-secret-3", "new_password": "20261111"}, "new_password", "error.passwordNumeric"),
            "like username": ({"current_password": "junaid-secret-3", "new_password": "junaid123"}, "new_password", "error.passwordTooSimilar"),
        }
        for label, (body, field, code) in cases.items():
            with self.subTest(label):
                response = self.client.post(url, body, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.json()[field], [code])
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.must_change_password)


class PinMigrationContractTests(AccountsApiTestCase):
    """The data migration copies pin_hash into password_hash unchanged and
    sets must_change_password. This is what that row must then do."""

    def test_old_pin_works_as_the_first_password_and_forces_a_change(self):
        staff = Staff.objects.create(
            branch=self.riyadh, name="Legacy Cashier", role=StaffRole.CASHIER,
            password_hash=make_password("4321"), must_change_password=True,
        )
        self.assertEqual(staff.username, "legacy.cashier")
        body = self.login("legacy.cashier", "4321").json()
        self.assertTrue(body["staff"]["must_change_password"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
        self.assertEqual(self.client.get("/api/orders/").status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.post(
            "/api/auth/change-password/",
            {"current_password": "4321", "new_password": "legacy-but-strong-8"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_row_with_no_password_cannot_log_in(self):
        Staff.objects.create(branch=self.riyadh, name="No Pin", role=StaffRole.WAITER)
        response = self.login("no.pin", "")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class StaffCredentialFieldTests(AccountsApiTestCase):
    url = "/api/staff/"

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.owner)

    def test_create_with_username_and_password(self):
        response = self.client.post(self.url, {
            "branch": self.riyadh.id, "name": "Hamza Ali", "role": "waiter",
            "username": "Hamza.A", "password": "hamza-strong-9",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        body = response.json()
        self.assertEqual(body["username"], "hamza.a")
        self.assertTrue(body["must_change_password"])
        self.assertFalse(body["is_locked"])
        self.assertNotIn("password", body)

    def test_create_generates_a_username_when_blank(self):
        response = self.client.post(self.url, {
            "branch": self.riyadh.id, "name": "Hamza Ali", "role": "waiter", "password": "hamza-strong-9",
        }, format="json")
        self.assertEqual(response.json()["username"], "hamza.ali")

    def test_create_requires_a_strong_password(self):
        base = {"branch": self.riyadh.id, "name": "Hamza Ali", "role": "waiter"}
        self.assertEqual(self.client.post(self.url, base, format="json").json()["password"], ["error.passwordRequired"])
        self.assertEqual(
            self.client.post(self.url, {**base, "password": "1234"}, format="json").json()["password"],
            ["error.passwordTooShort"],
        )
        self.assertEqual(
            self.client.post(self.url, {**base, "username": "hamza", "password": "hamza.ali"}, format="json").json()["password"],
            ["error.passwordTooSimilar"],
        )

    def test_username_must_be_valid_and_unique(self):
        base = {"branch": self.riyadh.id, "name": "Hamza Ali", "role": "waiter", "password": "hamza-strong-9"}
        self.assertEqual(
            self.client.post(self.url, {**base, "username": "ab"}, format="json").json()["username"],
            ["error.usernameInvalid"],
        )
        self.assertEqual(
            self.client.post(self.url, {**base, "username": "JUNAID"}, format="json").json()["username"],
            ["error.usernameTaken"],
        )

    def test_owner_can_rename_a_username(self):
        response = self.client.patch(f"{self.url}{self.cashier.pk}/", {"username": "junaid.k"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["username"], "junaid.k")

    def test_password_cannot_be_changed_through_patch(self):
        response = self.client.patch(f"{self.url}{self.cashier.pk}/", {"password": "sneaky-pass-1"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["password"], ["error.useResetPassword"])

    def test_list_exposes_lock_state_but_never_hashes(self):
        self.cashier.lock(Staff.LOCK_REASON_FAILED_LOGINS)
        self.cashier.save()
        rows = {row["username"]: row for row in self.client.get(self.url).json()["results"]}
        self.assertTrue(rows["junaid"]["is_locked"])
        self.assertIsNotNone(rows["junaid"]["locked_at"])
        self.assertNotIn("password_hash", rows["junaid"])

    def test_search_matches_username(self):
        names = [row["name"] for row in self.client.get(self.url, {"search": "junai"}).json()["results"]]
        self.assertEqual(names, ["Junaid"])
