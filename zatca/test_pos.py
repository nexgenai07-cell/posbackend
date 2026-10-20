"""
Milestones 4-6: POS settlements -> invoices, B2B clearance, credit notes,
receipts and the retry job. ZATCA is the FakeZatca from tests.py, which
verifies every signature and hash like the real API.
"""

import io
import uuid
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from orders import billing
from orders.models import Bill, BillMethod, Order, OrderItem, OrderItemStatus, OrderStatus, Payment
from tables.models import Table

from . import pos, queue, qr
from .client import ZatcaClient
from .environments import Environment
from .models import DocumentStatus, ZatcaDocument, ZatcaDocumentOrder, ZatcaSettings
from .onboarding import onboard
from .tests import TEST_KEY, FakeZatca, make_staff

D = Decimal


@override_settings(ZATCA_ENCRYPTION_KEY=TEST_KEY, ZATCA_SUBMIT="inline")
class PosTestBase(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Burger House", name_ar="بيت البرجر")
        settings = ZatcaSettings.for_branch(self.branch)
        self.fake = FakeZatca(sandbox_demo_cert=True)
        onboard(self.branch, otp="", client=self.client_for_fake(), activate=True)
        settings.enabled, settings.enabled_at = True, timezone.now()
        settings.receipt_footer_en, settings.receipt_footer_ar = "Thank you", "شكراً لكم"
        settings.save()
        self.settings = settings

        category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="طعام")
        self.burger = Product.objects.create(branch=self.branch, category=category, name_en="Burger",
                                             name_ar="برجر", price=D("10.00"), cost_price=D("3.00"))
        self.table = Table.objects.create(branch=self.branch, label_en="T7", label_ar="T7")
        self.cashier = make_staff(self.branch, "Cashier", StaffRole.CASHIER)
        self.manager = make_staff(self.branch, "Manager", StaffRole.MANAGER)
        self.api = APIClient()
        self.api.force_authenticate(user=self.cashier)

        # Every ZatcaClient the code builds talks to the fake.
        def fake_client(environment, session=None, timeout=None):
            return ZatcaClient(environment, session=self.fake)

        for target in ("zatca.documents.ZatcaClient", "zatca.pos.ZatcaClient"):
            patcher = mock.patch(target, side_effect=fake_client)
            patcher.start()
            self.addCleanup(patcher.stop)

    def client_for_fake(self):
        return ZatcaClient(Environment.SANDBOX, session=self.fake)

    def served_order(self, quantity=3, price=D("10.00"), table=None, bill=None):
        order = Order.objects.create(branch=self.branch, table=table, bill=bill, status=OrderStatus.SERVED)
        OrderItem.objects.create(order=order, product=self.burger, name_en_snapshot="Burger", name_ar_snapshot="برجر",
                                 price_snapshot=price, quantity=quantity, status=OrderItemStatus.SERVED)
        return order

    def pay(self, order, amount, method="cash", tip=None):
        body = {"request_id": uuid.uuid4().hex, "amount": str(amount), "method": method}
        if tip is not None:
            body["tip"] = str(tip)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api.post(f"/api/orders/{order.pk}/payments/", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def invoice_of(self, order):
        return ZatcaDocument.objects.get(order_links__order=order, kind="invoice")

    def receipts(self, **params):
        return self.api.get("/api/zatca/receipts/", params).json()


class TillPaymentTests(PosTestBase):
    def test_cash_payment_issues_and_reports_one_simplified_invoice(self):
        order = self.served_order()
        self.pay(order, "30.00", tip="5.00")
        invoice = self.invoice_of(order)
        self.assertEqual((invoice.simplified, invoice.status, invoice.icv), (True, DocumentStatus.REPORTED, 1))
        self.assertIn("<cbc:PaymentMeansCode>10</cbc:PaymentMeansCode>", invoice.signed_xml)
        self.assertIn('<cbc:PayableAmount currencyID="SAR">30.00</cbc:PayableAmount>', invoice.signed_xml)

        [receipt] = self.receipts(order=order.pk)
        self.assertEqual(receipt["number"], invoice.number)
        # 3 x 10.00 incl. VAT nets to 26.09, which no single unit price hits: two invoice lines.
        self.assertEqual(receipt["items"], [{"name_en": "Burger", "name_ar": "برجر", "variant_en": "", "variant_ar": "",
                                             "quantity": 3, "unit_price": "10.00", "total": "30.00", "lines": [
            {"quantity": 2, "unit_net": "8.70", "net": "17.40", "rate": "15.00", "vat": "2.61", "total": "20.01"},
            {"quantity": 1, "unit_net": "8.69", "net": "8.69", "rate": "15.00", "vat": "1.30", "total": "9.99"},
        ]}])
        self.assertEqual(receipt["totals"], {"net": "26.09", "vat": "3.91", "rate": "15.00", "inclusive": "30.00",
                                             "rounding": "0.00", "payable": "30.00"})
        self.assertEqual(receipt["payments"], [{"method": "cash", "amount": "30.00", "tip": "5.00"}])
        self.assertEqual(receipt["tip"], "5.00")  # printed below, not part of the invoice
        self.assertEqual((receipt["order_type"], receipt["is_test"], receipt["footer_ar"]), ("takeaway", True, "شكراً لكم"))
        self.assertEqual(qr.decode(receipt["qr"])[4].decode(), "30.00")
        self.assertEqual(receipt["seller"]["vat_number"], "399999999900003")

    def test_split_payment_is_one_invoice_with_both_means(self):
        order = self.served_order(quantity=7, price=D("13.99"))
        self.pay(order, "50.00", method="card")
        self.assertFalse(ZatcaDocument.objects.exists())  # not settled yet
        self.pay(order, "47.93", method="cash")
        invoice = self.invoice_of(order)
        self.assertEqual(ZatcaDocument.objects.count(), 1)
        self.assertIn("<cbc:PaymentMeansCode>48</cbc:PaymentMeansCode>", invoice.signed_xml)
        self.assertIn("<cbc:PaymentMeansCode>10</cbc:PaymentMeansCode>", invoice.signed_xml)
        self.assertEqual([p["method"] for p in invoice.receipt["payments"]], ["card", "cash"])

    def test_zatca_off_changes_nothing(self):
        ZatcaSettings.objects.update(enabled=False)
        order = self.served_order()
        self.pay(order, "30.00")
        self.assertFalse(ZatcaDocument.objects.exists())
        self.assertEqual(self.receipts(order=order.pk), [])

    def test_zatca_down_never_blocks_the_payment_and_the_job_reports_it_later(self):
        self.fake.outage = True
        order = self.served_order()
        self.pay(order, "30.00")  # 201 regardless
        invoice = self.invoice_of(order)
        self.assertEqual((invoice.status, invoice.attempts), (DocumentStatus.PENDING, 1))
        self.assertEqual(len(self.receipts(order=order.pk)[0]["qr"]) > 0, True)  # B2C QR is ours, printable now

        self.fake.outage = False
        out = io.StringIO()
        with mock.patch.object(queue, "MIN_WAIT", queue.MIN_WAIT * 0):
            call_command("zatca_retry_queue", stdout=out)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, DocumentStatus.REPORTED)
        self.assertIn(f"{invoice.number} (ICV 1): reported", out.getvalue())

    def test_a_failing_hook_never_blocks_the_payment_and_the_job_issues_the_invoice(self):
        order = self.served_order()
        with mock.patch("zatca.pos.documents.issue", side_effect=RuntimeError("boom")):
            self.pay(order, "30.00")
        self.assertFalse(ZatcaDocument.objects.exists())
        self.assertEqual(Payment.objects.filter(order=order).count(), 1)
        result = queue.run_once()
        self.assertEqual([d.number for d in result.issued], [self.invoice_of(order).number])
        self.assertEqual(self.invoice_of(order).status, DocumentStatus.REPORTED)
        self.assertEqual(queue.run_once().issued, [])  # never twice

    def test_orders_paid_before_zatca_was_on_are_not_back_invoiced(self):
        ZatcaSettings.objects.update(enabled=False)
        order = self.served_order()
        self.pay(order, "30.00")
        ZatcaSettings.objects.update(enabled=True, enabled_at=timezone.now())
        self.assertEqual(queue.run_once().issued, [])
        self.assertFalse(ZatcaDocumentOrder.objects.filter(order=order).exists())


class BillSettlementTests(PosTestBase):
    def setUp(self):
        super().setUp()
        self.bill = Bill.objects.create(branch=self.branch, table=self.table)
        self.first = self.served_order(quantity=3, table=self.table, bill=self.bill)
        self.second = self.served_order(quantity=1, price=D("1.11"), table=self.table, bill=self.bill)

    def settle(self, method, moyasar=None):
        with self.captureOnCommitCallbacks(execute=True):
            return billing.mark_bill_paid(self.bill.pk, method, actor=self.cashier, moyasar=moyasar)

    def test_one_invoice_for_the_whole_settlement(self):
        self.settle(BillMethod.CARD)
        invoice = self.invoice_of(self.first)
        self.assertEqual(invoice, self.invoice_of(self.second))
        self.assertEqual(ZatcaDocument.objects.count(), 1)
        self.assertEqual(invoice.receipt["table"], "T7")
        self.assertEqual(invoice.receipt["order_type"], "dine_in")
        self.assertEqual(invoice.receipt["totals"]["payable"], "31.11")
        self.assertEqual(len(self.api.get("/api/zatca/receipts/", {"bill": self.bill.pk}).json()), 1)

    def test_moyasar_method_maps_to_payment_means(self):
        for company, label, code in (("mada", "mada", "48"), ("visa", "visa", "48")):
            with self.subTest(company=company):
                ZatcaDocumentOrder.objects.all().delete()
                ZatcaDocument.objects.all().delete()
                Payment.objects.all().delete()
                Bill.objects.filter(pk=self.bill.pk).update(status="pay_requested", pay_requested_at=timezone.now())
                Order.objects.filter(bill=self.bill).update(payment_status="unpaid", status=OrderStatus.SERVED)
                self.bill.refresh_from_db()
                payload = {"id": f"pay_{company}", "status": "paid", "amount": 3111, "currency": "SAR",
                           "metadata": {"bill_id": str(self.bill.pk),
                                        "payment_request_at": self.bill.pay_requested_at.isoformat()},
                           "source": {"type": "creditcard", "company": company}}
                self.settle(BillMethod.ONLINE, moyasar=payload)
                invoice = self.invoice_of(self.first)
                self.assertEqual({p["method"] for p in invoice.receipt["payments"]}, {label})
                self.assertIn(f"<cbc:PaymentMeansCode>{code}</cbc:PaymentMeansCode>", invoice.signed_xml)

    def test_wallet_sources(self):
        self.assertEqual(pos.moyasar_source({"source": {"type": "applepay", "company": "visa"}}), "applepay")
        self.assertEqual(pos.moyasar_source({"source": {"type": "stcpay"}}), "stcpay")
        self.assertEqual(pos._MOYASAR_MEANS["stcpay"][1], "30")
        self.assertEqual(pos._MOYASAR_MEANS["applepay"][1], "48")

    def test_public_bill_carries_the_customers_invoice(self):
        self.settle(BillMethod.CARD)
        from orders.billing_serializers import BillSerializer
        data = BillSerializer(Bill.objects.get(pk=self.bill.pk), context={"public": True}).data
        self.assertEqual(len(data["invoices"]), 1)
        self.assertNotIn("print_count", data["invoices"][0])
        public = APIClient().get(f"/api/zatca/public/receipts/{data['invoices'][0]['uuid']}/")
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.json()["number"], self.invoice_of(self.first).number)


class B2BTests(PosTestBase):
    company = {"legal_name": "شركة الاختبار | Test Co", "vat_number": "300000000000003", "crn": "1010101010",
               "street": "King Fahd Rd", "building_no": "1234", "additional_no": "5678", "district": "Olaya",
               "city": "Riyadh", "postal_code": "12211"}

    def make_company(self):
        response = self.api.post("/api/zatca/business-customers/", self.company, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.json()["id"]

    def test_company_invoice_is_cleared_at_payment(self):
        order = self.served_order()
        response = self.api.post(f"/api/zatca/orders/{order.pk}/buyer/", {"business_customer": self.make_company()},
                                 format="json")
        self.assertEqual(response.status_code, 200)
        self.pay(order, "30.00", method="card")
        invoice = self.invoice_of(order)
        self.assertEqual((invoice.simplified, invoice.status), (False, DocumentStatus.CLEARED))
        self.assertTrue(self.fake.calls[-1]["url"].endswith("/invoices/clearance/single"))
        self.assertIn("<cbc:CompanyID>300000000000003</cbc:CompanyID>", invoice.signed_xml)
        [receipt] = self.receipts(order=order.pk)
        self.assertEqual(receipt["buyer"]["legal_name"], self.company["legal_name"])
        self.assertEqual(receipt["qr"], "Q0xFQVJFRA==")  # ZATCA's QR from the cleared XML
        self.assertFalse(receipt["pending_clearance"])
        # A tax invoice prints each line net of VAT, with its rate and VAT, matching the XML.
        lines = receipt["items"][0]["lines"]
        self.assertEqual([(l["quantity"], l["unit_net"], l["rate"], l["vat"]) for l in lines],
                         [(2, "8.70", "15.00", "2.61"), (1, "8.69", "15.00", "1.30")])
        self.assertIn('<cbc:PriceAmount currencyID="SAR">8.69</cbc:PriceAmount>', invoice.signed_xml)
        # Once invoiced, the buyer can't be changed any more.
        again = self.api.post(f"/api/zatca/orders/{order.pk}/buyer/", {"business_customer": None}, format="json")
        self.assertEqual(again.json()["error"], "error.zatcaAlreadyInvoiced")

    def test_zatca_down_means_pending_clearance_without_qr_then_the_job_clears(self):
        order = self.served_order()
        self.api.post(f"/api/zatca/orders/{order.pk}/buyer/", {"business_customer": self.make_company()}, format="json")
        self.fake.outage = True
        self.pay(order, "30.00")
        [receipt] = self.receipts(order=order.pk)
        self.assertEqual((receipt["pending_clearance"], receipt["qr"]), (True, ""))
        self.fake.outage = False
        with mock.patch.object(queue, "MIN_WAIT", queue.MIN_WAIT * 0):
            queue.run_once()
        [receipt] = self.receipts(order=order.pk)
        self.assertEqual((receipt["status"], receipt["pending_clearance"], receipt["qr"]),
                         ("cleared", False, "Q0xFQVJFRA=="))

    def test_till_status_and_current_buyer(self):
        self.assertEqual(self.api.get("/api/zatca/pos-status/").json(), {"enabled": True, "is_test": True, "b2b": True})
        ZatcaSettings.objects.update(invoice_types="0100")
        self.assertFalse(self.api.get("/api/zatca/pos-status/").json()["b2b"])
        order = self.served_order()
        self.assertIsNone(self.api.get(f"/api/zatca/orders/{order.pk}/buyer/").json()["business_customer"])
        self.api.post(f"/api/zatca/orders/{order.pk}/buyer/", {"business_customer": self.make_company()}, format="json")
        current = self.api.get(f"/api/zatca/orders/{order.pk}/buyer/").json()["business_customer"]
        self.assertEqual(current["vat_number"], "300000000000003")

    def test_company_validation(self):
        response = self.api.post("/api/zatca/business-customers/", {**self.company, "vat_number": "123",
                                                                     "building_no": "12", "crn": "12"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["vat_number"], ["error.zatcaVatNumberInvalid"])
        self.assertEqual(response.json()["building_no"], ["error.zatcaBuildingNoInvalid"])
        self.assertEqual(response.json()["crn"], ["error.zatcaCrnInvalid"])
        self.make_company()
        found = self.api.get("/api/zatca/business-customers/", {"search": "3000"}).json()
        self.assertEqual([c["vat_number"] for c in found], ["300000000000003"])

    def test_bill_buyer_applies_to_its_unpaid_orders(self):
        bill = Bill.objects.create(branch=self.branch, table=self.table)
        order = self.served_order(table=self.table, bill=bill)
        self.api.post(f"/api/zatca/bills/{bill.pk}/buyer/", {"business_customer": self.make_company()}, format="json")
        with self.captureOnCommitCallbacks(execute=True):
            billing.mark_bill_paid(bill.pk, BillMethod.CASH, actor=self.cashier)
        self.assertFalse(self.invoice_of(order).simplified)


class CreditNoteTests(PosTestBase):
    def cancel(self, order, reason="Customer complaint"):
        api = APIClient()
        api.force_authenticate(user=self.manager)
        with self.captureOnCommitCallbacks(execute=True):
            response = api.post(f"/api/orders/{order.pk}/cancel/", {"reason": reason}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def test_cancelling_a_paid_order_issues_a_full_credit_note(self):
        order = self.served_order(quantity=7, price=D("13.99"))
        self.pay(order, "97.93")
        invoice = self.invoice_of(order)
        self.cancel(order)
        note = ZatcaDocument.objects.get(order_links__order=order, kind="credit_note")
        self.assertEqual((note.original, note.icv, note.status), (invoice, invoice.icv + 1, DocumentStatus.REPORTED))
        self.assertIn(f"<cbc:ID>{invoice.number}</cbc:ID>", note.signed_xml)
        self.assertIn("<cbc:InstructionNote>Customer complaint</cbc:InstructionNote>", note.signed_xml)
        self.assertEqual(note.receipt["totals"], invoice.receipt["totals"])  # the same split, to the halala
        receipts = self.receipts(order=order.pk)
        self.assertEqual([r["kind"] for r in receipts], ["invoice", "credit_note"])
        self.assertEqual(receipts[1]["original"]["number"], invoice.number)
        self.assertEqual(receipts[1]["items"][0]["total"], "97.93")  # items voided on the order, kept on the note

    def test_unpaid_cancellation_has_no_credit_note_and_the_job_catches_missed_ones(self):
        unpaid = self.served_order()
        self.cancel(unpaid)
        self.assertFalse(ZatcaDocument.objects.exists())

        order = self.served_order()
        self.pay(order, "30.00")
        with mock.patch("zatca.pos.documents.issue", side_effect=RuntimeError("boom")):
            self.cancel(order)
        self.assertFalse(ZatcaDocument.objects.filter(kind="credit_note").exists())
        self.assertEqual(len(queue.run_once().credited), 1)
        self.assertEqual(queue.run_once().credited, [])


class ReceiptTests(PosTestBase):
    def test_reprints_are_copies(self):
        order = self.served_order()
        self.pay(order, "30.00")
        [receipt] = self.receipts(order=order.pk)
        first = self.api.post(f"/api/zatca/receipts/{receipt['id']}/printed/").json()
        second = self.api.post(f"/api/zatca/receipts/{receipt['id']}/printed/").json()
        self.assertEqual((first["copy"], second["copy"], second["print_count"]), (False, True, 2))

    def test_public_receipt_never_shows_a_company_invoice(self):
        order = self.served_order()
        company = B2BTests.company
        buyer = self.api.post("/api/zatca/business-customers/", company, format="json").json()["id"]
        self.api.post(f"/api/zatca/orders/{order.pk}/buyer/", {"business_customer": buyer}, format="json")
        self.pay(order, "30.00")
        self.assertEqual(APIClient().get(f"/api/zatca/public/receipts/{self.invoice_of(order).uuid}/").status_code, 404)

    def test_managers_list_and_retry(self):
        self.fake.outage = True
        order = self.served_order()
        self.pay(order, "30.00")
        api = APIClient()
        api.force_authenticate(user=self.manager)
        listing = api.get("/api/zatca/documents/").json()
        self.assertEqual(listing["counts"]["pending"], 1)
        self.assertEqual(self.api.get("/api/zatca/documents/").status_code, 403)  # cashiers can't
        self.fake.outage = False
        retried = api.post(f"/api/zatca/documents/{listing['documents'][0]['id']}/retry/").json()
        self.assertEqual(retried["status"], "reported")
