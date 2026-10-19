"""
Milestones 7-8: the go-live wizard (practice on Sandbox, Simulation, the
Production guard), repeating compliance checks after go-live, partial
refunds with their own credit notes, and debit notes. ZATCA is FakeZatca.
"""

from unittest import mock

from django.test import override_settings
from rest_framework.test import APIClient

from accounts.models import StaffRole

from . import queue
from .client import ZatcaClient
from .environments import LEGAL_WARNING, Environment
from .models import DeviceStatus, DocumentStatus, ZatcaAuditLog, ZatcaDevice, ZatcaDocument, ZatcaSettings
from .test_pos import D, PosTestBase
from .tests import FakeZatca, make_staff, seller_vats

REAL_DETAILS = {
    "seller_name_en": "Burger House LLC", "seller_name_ar": "شركة بيت البرجر", "vat_number": "310175397400003",
    "other_id": "1010987654", "other_id_scheme": "CRN", "street": "King Fahd Road", "building_no": "1234",
    "additional_no": "5678", "district": "Al Olaya", "city": "Riyadh", "postal_code": "12211",
}


class GoLiveTestBase(PosTestBase):
    def setUp(self):
        super().setUp()
        self.owner = make_staff(self.branch, "Owner", StaffRole.OWNER)
        self.owner_api = APIClient()
        self.owner_api.force_authenticate(user=self.owner)
        self.first_device = self.settings.active_device()

        patcher = mock.patch("zatca.golive.ZatcaClient",
                             side_effect=lambda environment, **kwargs: ZatcaClient(environment, session=self.fake))
        patcher.start()
        self.addCleanup(patcher.stop)

    def step(self, name, data=None, expect=200):
        response = self.owner_api.post(f"/api/zatca/go-live/{name}/", data or {}, format="json")
        self.assertEqual(response.status_code, expect, getattr(response, "data", None))
        return response.data

    def start(self, environment, expect=201, **extra):
        response = self.owner_api.post("/api/zatca/go-live/", {"environment": environment, **extra}, format="json")
        self.assertEqual(response.status_code, expect, response.data)
        return response.data

    def details(self, data, expect=200):
        response = self.owner_api.patch("/api/zatca/go-live/", data, format="json")
        self.assertEqual(response.status_code, expect, response.data)
        return response.data

    def run_to_ready(self, otp="", **csid):
        self.step("keys")
        self.step("compliance-csid", {"otp": otp, **csid})
        checks = self.step("checks")
        self.assertTrue(all(check["passed"] for check in checks["checks"]))
        return self.step("production-csid")


class PracticeModeTests(GoLiveTestBase):
    def test_practice_run_on_sandbox_leaves_device_one_in_use(self):
        self.fake = FakeZatca(sandbox_demo_cert=True)
        before = ZatcaSettings.objects.filter(pk=self.settings.pk).values().get()
        wizard = self.start(Environment.SANDBOX)
        self.assertEqual((wizard["practice"], wizard["step"], wizard["default_otp"]), (True, "details", "123345"))
        self.assertEqual(wizard["details"]["vat_number"], "399999999900003")
        self.assertEqual(wizard["fixed_fields"], ["vat_number", "other_id", "other_id_scheme"])

        # The sandbox identity is fixed; the names may change.
        self.assertEqual(self.details({"vat_number": "310175397400003"}, expect=400)["fields"]["vat_number"],
                         "error.zatcaPracticeIdentityFixed")
        self.details({"seller_name_en": "Practice Burger"})

        ready = self.run_to_ready()  # blank OTP -> the sandbox's 123345
        self.assertEqual(ready["step"], "activate")
        self.assertEqual(self.fake.calls[0]["headers"]["OTP"], "123345")
        done = self.step("finish", {"activate": False})
        self.assertEqual((done["status"], done["activated"]), ("completed", False))

        new = ZatcaDevice.objects.get(pk=ready["device"]["id"])
        self.assertEqual((new.status, new.is_active, new.archived_at), (DeviceStatus.READY, False, None))
        self.assertEqual(ZatcaSettings.for_branch(self.branch).active_device(), self.first_device)
        self.assertEqual(ZatcaSettings.objects.filter(pk=self.settings.pk).values().get(), before)

        # Sales keep going through device #1.
        order = self.served_order()
        self.pay(order, "30.00")
        self.assertEqual(self.invoice_of(order).device, self.first_device)

    def test_practice_run_can_be_activated(self):
        self.fake = FakeZatca(sandbox_demo_cert=True)
        self.start(Environment.SANDBOX)
        ready = self.run_to_ready(otp="123345")
        self.step("finish", {"activate": True})
        new = ZatcaDevice.objects.get(pk=ready["device"]["id"])
        self.first_device.refresh_from_db()
        self.assertTrue(new.is_active)
        self.assertIsNotNone(self.first_device.archived_at)
        order = self.served_order()
        self.pay(order, "30.00")
        invoice = self.invoice_of(order)
        self.assertEqual((invoice.device, invoice.icv, invoice.status), (new, 1, DocumentStatus.REPORTED))

    def test_one_wizard_at_a_time_and_cancel(self):
        self.start(Environment.SANDBOX)
        self.assertEqual(self.start(Environment.SIMULATION, expect=409)["error"], "error.zatcaGoLiveInProgress")
        self.step("keys")
        device = ZatcaDevice.objects.exclude(pk=self.first_device.pk).get()
        self.assertEqual(self.details({"seller_name_en": "x"}, expect=409)["error"], "error.zatcaGoLiveDetailsLocked")
        response = self.owner_api.delete("/api/zatca/go-live/")
        self.assertEqual(response.status_code, 200)
        device.refresh_from_db()
        self.assertIsNotNone(device.archived_at)
        self.assertTrue(self.first_device.__class__.objects.get(pk=self.first_device.pk).is_active)

    def test_managers_watch_but_cannot_act(self):
        api = APIClient()
        api.force_authenticate(user=self.manager)
        self.assertEqual(api.get("/api/zatca/go-live/").status_code, 200)
        self.assertEqual(api.post("/api/zatca/go-live/", {"environment": "sandbox"}, format="json").status_code, 403)
        self.assertEqual(api.post("/api/zatca/go-live/keys/", {}, format="json").status_code, 403)
        self.assertEqual(api.post(f"/api/zatca/devices/{self.first_device.pk}/compliance-checks/").status_code, 403)


class SimulationAndProductionTests(GoLiveTestBase):
    def go_live(self, environment, otp="654321", **csid):
        self.fake = FakeZatca()
        wizard = self.start(environment)
        # Test values cleared, branch name kept.
        self.assertEqual((wizard["details"]["vat_number"], wizard["details"]["other_id"]), ("", ""))
        self.assertEqual(wizard["details"]["seller_name_en"], "Burger House")
        self.details(REAL_DETAILS)
        return self.run_to_ready(otp=otp, **csid)

    def test_simulation_switches_environment_identity_and_starts_a_new_chain(self):
        order = self.served_order()
        self.pay(order, "30.00")  # device #1's chain is at ICV 1
        ready = self.go_live(Environment.SIMULATION)
        self.assertTrue(all("simulation" in call["url"] for call in self.fake.calls))
        # Not finished yet: the sandbox device still issues.
        self.assertEqual(ZatcaSettings.for_branch(self.branch).environment, Environment.SANDBOX)
        self.assertEqual(self.step("finish", {"activate": False}, expect=400)["error"], "error.zatcaGoLiveMustActivate")
        self.step("finish", {"activate": True})

        settings = ZatcaSettings.for_branch(self.branch)
        new = ZatcaDevice.objects.get(pk=ready["device"]["id"])
        self.first_device.refresh_from_db()
        self.assertEqual((settings.environment, settings.vat_number, settings.other_id),
                         (Environment.SIMULATION, "310175397400003", "1010987654"))
        self.assertEqual(settings.active_device(), new)
        self.assertIsNotNone(self.first_device.archived_at)
        self.assertTrue(self.first_device.documents.exists())  # kept, read-only

        later = self.served_order()
        self.pay(later, "30.00")
        invoice = self.invoice_of(later)
        self.assertEqual((invoice.device, invoice.icv, invoice.status), (new, 1, DocumentStatus.REPORTED))
        self.assertEqual(seller_vats(self.fake)[-1], "310175397400003")
        # The identity is locked again by the live device.
        response = self.owner_api.patch("/api/zatca/settings/", {"vat_number": "300075397400003"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_live_environments_refuse_test_identities_and_otps(self):
        self.fake = FakeZatca()
        self.start(Environment.SIMULATION)
        sandbox_vat = {**REAL_DETAILS, "vat_number": "399999999900003"}
        self.assertEqual(self.details(sandbox_vat, expect=400)["fields"]["vat_number"], "error.zatcaTestVatNotAllowed")
        self.details(REAL_DETAILS)
        self.step("keys")
        self.assertEqual(self.step("compliance-csid", {"otp": "123345"}, expect=400)["error"], "error.zatcaOtpInvalid")
        self.assertEqual(self.step("compliance-csid", {"otp": ""}, expect=400)["error"], "error.zatcaOtpRequired")
        self.assertEqual(self.step("compliance-csid", {"otp": "12ab"}, expect=400)["error"], "error.zatcaOtpInvalid")
        self.assertEqual(self.fake.calls, [])

    def test_production_needs_the_legal_confirmation(self):
        self.fake = FakeZatca()
        wizard = self.start(Environment.PRODUCTION)
        self.assertEqual((wizard["needs_legal_confirmation"], wizard["legal_warning"]), (True, LEGAL_WARNING))
        self.details(REAL_DETAILS)
        self.step("keys")
        self.assertEqual(self.step("compliance-csid", {"otp": "654321"}, expect=400)["error"],
                         "error.zatcaLegalConfirmRequired")
        self.assertEqual(self.fake.calls, [])
        self.step("compliance-csid", {"otp": "654321", "confirm_legal": True})
        self.assertTrue(all("/core/" in call["url"] for call in self.fake.calls))

    def test_back_to_sandbox_needs_confirmation_and_never_after_production_invoices(self):
        self.go_live(Environment.PRODUCTION, confirm_legal=True)
        self.step("finish", {"activate": True})
        self.assertEqual(self.start(Environment.SANDBOX, expect=400)["error"], "error.zatcaBackToSandboxConfirm")
        order = self.served_order()
        self.pay(order, "30.00")  # a Production invoice
        self.assertEqual(self.start(Environment.SANDBOX, expect=409, confirm_back_to_sandbox=True)["error"],
                         "error.zatcaBackToSandboxProduction")

    def test_checks_can_be_repeated_without_a_new_otp(self):
        self.fake = FakeZatca(reject_checks=True)
        self.start(Environment.SIMULATION)
        self.details(REAL_DETAILS)
        self.step("keys")
        self.step("compliance-csid", {"otp": "654321"})
        failed = self.step("checks", expect=400)
        self.assertEqual(failed["error"], "error.zatcaChecksFailed")
        self.assertEqual(failed["checks"][0]["errors"][0]["code"], "BR-KSA-TEST")
        self.fake.reject_checks = False
        csid_calls = sum(call["url"].endswith("/compliance") for call in self.fake.calls)
        self.assertTrue(all(check["passed"] for check in self.step("checks")["checks"]))
        self.assertEqual(sum(call["url"].endswith("/compliance") for call in self.fake.calls), csid_calls)
        self.step("production-csid")
        self.step("finish", {"activate": True})

        # After go-live too, with the same compliance CSID; the chain is untouched.
        device = ZatcaSettings.for_branch(self.branch).active_device()
        response = self.owner_api.post(f"/api/zatca/devices/{device.pk}/compliance-checks/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["passed"])
        device.refresh_from_db()
        self.assertEqual((device.status, device.last_icv), (DeviceStatus.ACTIVE, 0))
        self.assertEqual(sum(call["url"].endswith("/compliance") for call in self.fake.calls), csid_calls)

    def test_no_otp_or_secret_in_the_audit_log(self):
        self.go_live(Environment.SIMULATION, otp="654321")
        self.step("finish", {"activate": True})
        summaries = " ".join(ZatcaAuditLog.objects.values_list("summary", flat=True))
        for secret in ("654321", "compliance-secret-FAKE", "production-secret-FAKE"):
            self.assertNotIn(secret, summaries)
        self.assertIn("go_live_complete", ZatcaAuditLog.objects.values_list("action", flat=True))


class PartialRefundTests(PosTestBase):
    def refund(self, order, lines, expect=201, reason="Cold fries"):
        api = APIClient()
        api.force_authenticate(user=self.manager)
        with self.captureOnCommitCallbacks(execute=True):
            response = api.post(f"/api/orders/{order.pk}/refund/",
                                {"lines": lines, "method": "cash", "reason": reason}, format="json")
        self.assertEqual(response.status_code, expect, response.data)
        return response.data

    def cancel(self, order):
        api = APIClient()
        api.force_authenticate(user=self.manager)
        with self.captureOnCommitCallbacks(execute=True):
            response = api.post(f"/api/orders/{order.pk}/cancel/", {"reason": "Closed early"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_partial_refund_credits_only_those_lines(self):
        order = self.served_order(quantity=3)
        self.pay(order, "30.00")
        invoice = self.invoice_of(order)
        item = order.items.get()
        summary = self.refund(order, [{"item": item.pk, "quantity": 1}])
        self.assertEqual((summary["refunded"], summary["lines"][0]["refundable"]), (D("10.00"), 2))

        note = ZatcaDocument.objects.get(kind="credit_note")
        self.assertEqual((note.original, note.status, note.icv), (invoice, DocumentStatus.REPORTED, invoice.icv + 1))
        self.assertEqual(note.receipt["totals"]["payable"], "10.00")
        self.assertEqual([(i["quantity"], i["total"]) for i in note.receipt["items"]], [(1, "10.00")])
        self.assertIn("<cbc:InstructionNote>Cold fries</cbc:InstructionNote>", note.signed_xml)
        self.assertIn('<cbc:InvoicedQuantity unitCode="PCE">1</cbc:InvoicedQuantity>', note.signed_xml)

        # Then cancelling credits only the rest.
        self.cancel(order)
        rest = ZatcaDocument.objects.filter(kind="credit_note").exclude(pk=note.pk).get()
        self.assertEqual(rest.receipt["totals"]["payable"], "20.00")

    def test_cannot_refund_more_than_was_sold(self):
        order = self.served_order(quantity=2)
        self.pay(order, "20.00")
        item = order.items.get()
        self.assertEqual(self.refund(order, [{"item": item.pk, "quantity": 3}], expect=400)["error"],
                         "error.refundQuantityTooHigh")
        self.refund(order, [{"item": item.pk, "quantity": 2}])
        self.assertEqual(self.refund(order, [{"item": item.pk, "quantity": 1}], expect=400)["error"],
                         "error.refundQuantityTooHigh")
        unpaid = self.served_order()
        self.assertEqual(self.refund(unpaid, [{"item": unpaid.items.get().pk, "quantity": 1}], expect=409)["error"],
                         "error.refundNotPaid")
        self.assertEqual(self.refund(order, [{"item": item.pk, "quantity": 1}], expect=400, reason="")["error"],
                         "error.refundReasonRequired")

    def test_zatca_trouble_never_blocks_a_refund_and_the_job_issues_the_note(self):
        order = self.served_order(quantity=3)
        self.pay(order, "30.00")
        with mock.patch("zatca.pos.documents.issue", side_effect=RuntimeError("boom")):
            self.refund(order, [{"item": order.items.get().pk, "quantity": 1}])
        self.assertFalse(ZatcaDocument.objects.filter(kind="credit_note").exists())
        self.assertEqual(len(queue.run_once().credited), 1)
        self.assertEqual(queue.run_once().credited, [])

    def test_cashiers_cannot_refund(self):
        order = self.served_order()
        self.pay(order, "30.00")
        response = self.api.post(f"/api/orders/{order.pk}/refund/",
                                 {"lines": [{"item": order.items.get().pk, "quantity": 1}], "method": "cash",
                                  "reason": "x"}, format="json")
        self.assertEqual(response.status_code, 403)


class DebitNoteTests(PosTestBase):
    def setUp(self):
        super().setUp()
        self.owner = make_staff(self.branch, "Owner", StaffRole.OWNER)
        self.owner_api = APIClient()
        self.owner_api.force_authenticate(user=self.owner)

    def test_owner_issues_a_debit_note_for_an_undercharge(self):
        order = self.served_order()
        self.pay(order, "30.00")
        invoice = self.invoice_of(order)
        response = self.owner_api.post(f"/api/zatca/documents/{invoice.pk}/debit-note/",
                                       {"description": "Extra cheese not charged", "amount": "5.75",
                                        "reason": "Undercharged"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        note = ZatcaDocument.objects.get(kind="debit_note")
        self.assertEqual((note.original, note.status, note.icv), (invoice, DocumentStatus.REPORTED, invoice.icv + 1))
        self.assertIn('name="0200000">383</cbc:InvoiceTypeCode>', note.signed_xml)
        self.assertIn(f"<cbc:ID>{invoice.number}</cbc:ID>", note.signed_xml)
        self.assertIn('<cbc:PayableAmount currencyID="SAR">5.75</cbc:PayableAmount>', note.signed_xml)
        self.assertEqual(note.receipt["totals"]["inclusive"], "5.75")

    def test_rules(self):
        order = self.served_order()
        self.pay(order, "30.00")
        invoice = self.invoice_of(order)
        url = f"/api/zatca/documents/{invoice.pk}/debit-note/"
        api = APIClient()
        api.force_authenticate(user=self.manager)
        self.assertEqual(api.post(url, {}, format="json").status_code, 403)
        response = self.owner_api.post(url, {"description": "", "amount": "0", "reason": ""}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.data), {"amount", "description", "reason"})
        self.owner_api.post(url, {"description": "x", "amount": "1.00", "reason": "y"}, format="json")
        debit = ZatcaDocument.objects.get(kind="debit_note")
        response = self.owner_api.post(f"/api/zatca/documents/{debit.pk}/debit-note/",
                                       {"description": "x", "amount": "1.00", "reason": "y"}, format="json")
        self.assertEqual(response.data["error"], "error.zatcaDebitNeedsInvoice")
