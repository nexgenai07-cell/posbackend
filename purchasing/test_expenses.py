"""VAT dashboard M11a: business expenses with VAT (VAT plan §12). The
claimable rules shared with supplier documents, the flags only expenses
have, the supplier snapshot, void instead of delete, permissions and
period locks."""

from datetime import date
from decimal import Decimal

from django.db.models import ProtectedError
from django.test import TestCase

from purchasing.models import Expense, Supplier
from purchasing.test_vat import supplier_qr
from vat_returns.models import VatAuditLog, VatLockOverride, VatPeriod
from vat_returns.test_figures import VatFixture

D = Decimal
URL = "/api/expenses/"


class ExpenseFixture(VatFixture):
    def setUp(self):
        super().setUp()
        self.landlord = Supplier.objects.create(name_en="Olaya Properties", name_ar="العليا العقارية",
                                                vat_number="300000000000003", crn="1010020000")

    def post(self, **overrides):
        body = {"supplier": self.landlord.pk, "number": "RENT-10", "issue_date": "2026-10-05", "category": "rent",
                "net": "1000.00", "vat": "150.00", "total": "1150.00", **overrides}
        return self.api.post(URL, body, format="json")

    def add(self, **overrides):
        response = self.post(**overrides)
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def patch(self, expense_id, **body):
        return self.api.patch(f"{URL}{expense_id}/", body, format="json")

    def void(self, expense_id, reason="Keyed in twice by mistake", **body):
        return self.api.post(f"{URL}{expense_id}/void/", {"reason": reason, **body}, format="json")


class ExpenseRulesTests(ExpenseFixture, TestCase):

    def test_rent_invoice_with_qr_and_attachment(self):
        qr = supplier_qr(seller="Olaya Properties", vat="300000000000003", when="2026-10-05T10:00:00",
                         total="1150.00", tax="150.00")
        data = self.add(qr_text=qr, attachment_url="https://res.cloudinary.com/x/rent.pdf",
                        attachment_name="rent.pdf")
        self.assertEqual((data["claimable_vat"], data["claim_date"], data["supplier_vat_number"],
                          data["supplier_crn"]), ("150.00", "2026-10-05", "300000000000003", "1010020000"))
        self.assertEqual((data["qr_check"]["readable"], data["qr_check"]["mismatches"]), (True, []))
        self.assertEqual((data["flags"], data["why_not_claimed"], data["capital_asset"]), ([], None, False))
        self.assertEqual(data["created_by_name"], "Owner")

    def test_flags_without_qr_or_attachment(self):
        data = self.add()
        self.assertEqual(data["flags"], ["noQr", "noAttachment"])

    def test_supplier_without_vat_number_claims_nothing(self):
        data = self.add(supplier=self.no_vat_number.pk)
        self.assertEqual((data["claimable_vat"], data["why_not_claimed"]), ("0.00", "supplierVatMissing"))
        self.assertIn("supplierVatMissing", data["flags"])
        # Fixing the supplier makes the expense claimable, like supplier documents.
        response = self.api.patch(f"/api/suppliers/{self.no_vat_number.pk}/", {"vat_number": "300000000000003"},
                                  format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Expense.objects.get(pk=data["id"]).claimable_vat, D("150.00"))

    def test_not_registered_supplier_cannot_charge_vat(self):
        Supplier.objects.filter(pk=self.landlord.pk).update(vat_registered=False)
        response = self.post()
        self.assertEqual((response.status_code, response.data["vat"]), (400, ["error.supplierNotVatRegistered"]))
        data = self.add(vat="0.00", total="1000.00")
        self.assertEqual((data["vat_category"], data["claimable_vat"]), ("zero", "0.00"))
        self.assertIn("notRegistered", data["flags"])

    def test_not_claimable_keeps_the_vat_but_claims_none(self):
        response = self.post(not_claimable=True)
        self.assertEqual(response.data["not_claimable_reason"], ["error.expenseNotClaimableReasonRequired"])
        response = self.post(not_claimable=True, not_claimable_reason="other")
        self.assertEqual(response.data["not_claimable_note"], ["error.expenseNotClaimableNoteRequired"])
        data = self.add(category="services", not_claimable=True, not_claimable_reason="entertainment")
        self.assertEqual((data["vat"], data["claimable_vat"], data["why_not_claimed"]),
                         ("150.00", "0.00", "notClaimable"))
        self.assertIn("notClaimable", data["flags"])
        # Switching it back clears the reason and claims the VAT.
        data = self.patch(data["id"], not_claimable=False).data
        self.assertEqual((data["claimable_vat"], data["not_claimable_reason"]), ("150.00", ""))

    def test_equipment_is_a_capital_asset_by_default(self):
        self.assertTrue(self.add(category="equipment", number="EQ-1")["capital_asset"])
        self.assertFalse(self.add(category="equipment", number="EQ-2", capital_asset=False)["capital_asset"])
        self.assertFalse(self.add(category="maintenance", number="M-1")["capital_asset"])

    def test_other_needs_a_description(self):
        response = self.post(category="other")
        self.assertEqual(response.data["description"], ["error.expenseDescriptionRequired"])
        self.add(category="other", description="Pest control")

    def test_credit_note_is_negative(self):
        data = self.add(kind="credit_note", number="CN-1", net="100.00", vat="15.00", total="115.00")
        self.assertEqual(data["claimable_vat"], "-15.00")

    def test_possible_duplicate(self):
        first = self.add()
        second = self.add()
        self.assertIn("possibleDuplicate", second["flags"])
        self.void(first["id"])
        self.assertNotIn("possibleDuplicate", self.api.get(f"{URL}{second['id']}/").data["flags"])

    def test_list_filters(self):
        self.add()
        self.add(number="EQ-1", category="equipment", issue_date="2026-10-20")
        self.add(number="X-1", category="services", not_claimable=True, not_claimable_reason="gifts")
        self.add(number="S-1", supplier=self.no_vat_number.pk)

        def numbers(**params):
            return sorted(row["number"] for row in self.api.get(URL, params).data["results"])

        self.assertEqual(numbers(category="equipment"), ["EQ-1"])
        self.assertEqual(numbers(capital_asset="true"), ["EQ-1"])
        self.assertEqual(numbers(claim="not_claimable"), ["X-1"])
        self.assertEqual(numbers(claim="review"), ["S-1"])
        self.assertEqual(numbers(claim="claimable"), ["EQ-1", "RENT-10"])
        self.assertEqual(numbers(**{"from": "2026-10-10"}), ["EQ-1"])
        self.assertEqual(numbers(search="olaya"), ["EQ-1", "RENT-10", "X-1"])


class ExpenseVoidAndAccessTests(ExpenseFixture, TestCase):

    def test_void_not_delete(self):
        data = self.add()
        self.assertEqual(self.api.delete(f"{URL}{data['id']}/").status_code, 405)
        self.assertEqual(self.void(data["id"], "wrong").data, {"reason": "error.vatVoidReasonShort"})
        response = self.void(data["id"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["voided"], response.data["claimable_vat"], response.data["voided_by_name"]),
                         (True, "0.00", "Owner"))
        self.assertEqual(self.void(data["id"]).data["error"], "error.vatAlreadyVoided")
        self.assertEqual(self.patch(data["id"], number="R-11").data["error"], "error.expenseVoided")
        self.assertTrue(VatAuditLog.objects.filter(action="expense_voided").exists())
        # Hidden from the list unless asked for, struck through on the page.
        self.assertEqual(self.api.get(URL).data["count"], 0)
        self.assertEqual(self.api.get(URL, {"voided": "true"}).data["count"], 1)
        with self.assertRaises(ProtectedError):
            Expense.objects.get(pk=data["id"]).delete()

    def test_owner_and_manager_only(self):
        data = self.add()
        self.api.force_authenticate(self.manager)
        self.assertEqual(self.api.get(URL).status_code, 200)
        self.assertEqual(self.void(data["id"]).status_code, 200)
        self.api.force_authenticate(self.cashier)
        self.assertEqual(self.api.get(URL).status_code, 403)
        self.assertEqual(self.post(number="R-2").status_code, 403)

    def test_supplier_with_expenses_cannot_be_deleted(self):
        self.add()
        response = self.api.delete(f"/api/suppliers/{self.landlord.pk}/")
        self.assertEqual((response.status_code, response.data["error"]), (409, "error.supplierInUse"))


class ExpenseLockTests(ExpenseFixture, TestCase):
    """Q3 2026 filed (locked); Q4 open."""

    def lock_q3(self):
        VatPeriod.objects.filter(pk=self.q3.pk).update(locked=True)

    def test_a_late_expense_is_claimed_in_the_open_period(self):
        self.lock_q3()
        data = self.add(issue_date="2026-09-15")
        self.assertEqual((data["claim_date"], data["issue_period"]["id"] if data["issue_period"] else None),
                         ("2026-10-01", self.q3.pk))
        self.assertFalse(VatLockOverride.objects.exists())

    def test_a_filed_period_is_locked_until_the_owner_overrides(self):
        data = self.add(issue_date="2026-09-15")
        self.lock_q3()
        response = self.patch(data["id"], net="900.00", vat="135.00", total="1035.00")
        self.assertEqual((response.status_code, response.data["error"]), (409, "error.vatPeriodLocked"))

        self.api.force_authenticate(self.manager)
        response = self.patch(data["id"], net="900.00", lock_override_reason="Landlord reissued the invoice")
        self.assertEqual((response.status_code, response.data["error"]), (403, "error.vatOverrideOwnerOnly"))

        self.api.force_authenticate(self.owner)
        response = self.patch(data["id"], net="900.00", vat="135.00", total="1035.00",
                              lock_override_reason="Landlord reissued the invoice")
        self.assertEqual(response.status_code, 200, response.data)
        override = VatLockOverride.objects.get()
        self.assertEqual((override.action, override.object_type, override.object_id, override.period),
                         ("update", "purchasing.Expense", data["id"], self.q3))
        self.assertEqual((override.before["claimable_vat"], override.after["claimable_vat"]), ("150.00", "135.00"))

    def test_voiding_in_a_filed_period_needs_an_override(self):
        data = self.add(issue_date="2026-08-15")
        self.lock_q3()
        self.assertEqual(self.void(data["id"]).status_code, 409)
        self.assertFalse(Expense.objects.get(pk=data["id"]).voided)
        response = self.void(data["id"], lock_override_reason="Paid personally, not a business cost")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(VatLockOverride.objects.get().action, "void")

    def test_keeping_a_late_expense_in_the_filed_period(self):
        self.lock_q3()
        response = self.post(issue_date="2026-09-15", keep_in_filed_period=True)
        self.assertEqual(response.status_code, 409)
        response = self.post(issue_date="2026-09-15", keep_in_filed_period=True,
                             lock_override_reason="Found in the September post")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["claim_date"], "2026-09-15")
        self.assertEqual(VatLockOverride.objects.get().action, "create")

    def test_supplier_fix_leaves_filed_expenses_as_filed(self):
        data = self.add(issue_date="2026-08-15", supplier=self.no_vat_number.pk)
        self.lock_q3()
        self.api.patch(f"/api/suppliers/{self.no_vat_number.pk}/", {"vat_number": "300000000000003"}, format="json")
        self.assertEqual(Expense.objects.get(pk=data["id"]).claimable_vat, D("0.00"))
        self.assertEqual(Expense.objects.get(pk=data["id"]).claim_date, date(2026, 8, 15))
