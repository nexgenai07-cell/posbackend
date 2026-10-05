"""
Billing / Moyasar tests.

Moyasar is never actually called: orders.moyasar.fetch_payment is patched
everywhere, so the suite runs offline and deterministically. What is being
tested is our side of the contract — that we recompute the amount ourselves,
that we only believe the API response, and that a double settle cannot double
anything.
"""

from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from inventory.models import InventoryItem, RecipeItem
from orders.billing import bill_total, bill_total_halalas, to_halalas
from orders.models import (
    Bill,
    BillMethod,
    BillStatus,
    MoyasarPayment,
    Order,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    Payment,
    PaymentStatus,
)
from tables.models import Table, TableStatus

# A GPS fix sitting exactly on the test branch, for the geofenced QR endpoint.
AT_THE_RESTAURANT = {"latitude": 24.774265, "longitude": 46.738586, "accuracy_m": 5}

SECRET = "sk_test_dummy"
PUBLISHABLE = "pk_test_dummy"

billing_settings = override_settings(
    MOYASAR_SECRET_KEY=SECRET,
    MOYASAR_PUBLISHABLE_KEY=PUBLISHABLE,
    MOYASAR_WEBHOOK_SECRET="",
    CUSTOMER_WEB_BASE_URL="https://web.example.test",
    # Throttling is on by default for these scopes; it would make the suite
    # order-dependent, so it is switched off here rather than worked around.
    REST_FRAMEWORK={
        "DEFAULT_AUTHENTICATION_CLASSES": ("accounts.authentication.StaffJWTAuthentication",),
        "DEFAULT_PAGINATION_CLASS": "common.pagination.DefaultPagination",
        "PAGE_SIZE": 25,
        "EXCEPTION_HANDLER": "common.exceptions.error_code_exception_handler",
    },
)


def moyasar_payload(bill_id, amount, status="paid", payment_id="pay_1"):
    """A trimmed version of the real GET /v1/payments/{id} response."""
    return {
        "id": payment_id,
        "status": status,
        "amount": amount,
        "currency": "SAR",
        "metadata": {"bill_id": str(bill_id)},
        "source": {"type": "creditcard", "company": "mada"},
    }


@billing_settings
class BillingTestBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        # PublicOrderCreateView geofences every QR order, and a branch with no
        # coordinates is rejected outright (error.locationNotConfigured). Give
        # it a position and a wide radius so the location check passes and the
        # tests below exercise the billing logic rather than the geofence.
        self.branch = Branch.objects.create(
            name_en="Main", name_ar="Main",
            latitude=Decimal("24.774265"), longitude=Decimal("46.738586"),
            geofence_radius_m=Decimal("1000"),
        )
        category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        self.product = Product.objects.create(
            branch=self.branch, category=category, name_en="Burger", name_ar="Burger",
            price=Decimal("25.50"), cost_price=Decimal("8.00"),
        )
        # A product with no recipe is reported as "missing_recipe" by
        # inventory.services.check_products_stock, which makes
        # AddOrderItemSerializer reject it as unavailable. Give it one, with
        # far more stock than any test consumes, so these tests fail on
        # billing logic and never on inventory.
        self.ingredient = InventoryItem.objects.create(
            branch=self.branch, name="Beef", unit="g", current_stock=Decimal("100000"),
        )
        RecipeItem.objects.create(
            product=self.product, inventory_item=self.ingredient, quantity=Decimal("1"), unit="g",
        )

        self.table = Table.objects.create(branch=self.branch, label_en="T1", label_ar="T1")
        self.table.open()

        self.cashier = self._staff("Cashier", StaffRole.CASHIER)
        self.waiter = self._staff("Waiter", StaffRole.WAITER)
        self.kitchen = self._staff("Chef", StaffRole.KITCHEN)

        self.bill = Bill.objects.create(branch=self.branch, table=self.table)
        self.order = self._order(self.bill, OrderStatus.SERVED)
        self._item(self.order, quantity=2)  # 2 x 25.50 = 51.00

    def _staff(self, name, role):
        staff = Staff(branch=self.branch, name=name, role=role)
        staff.set_pin("1234")
        staff.save()
        return staff

    def _order(self, bill, status, table=None):
        return Order.objects.create(
            branch=self.branch, table=table or self.table, bill=bill, status=status,
        )

    def _item(self, order, quantity=1, status=OrderItemStatus.SERVED, price=None):
        return OrderItem.objects.create(
            order=order, product=self.product,
            name_en_snapshot=self.product.name_en, name_ar_snapshot=self.product.name_ar,
            price_snapshot=price if price is not None else self.product.price,
            quantity=quantity, status=status,
        )

    def as_staff(self, staff):
        self.client.force_authenticate(user=staff)
        return self.client

    def refresh(self):
        self.bill.refresh_from_db()
        self.table.refresh_from_db()
        self.order.refresh_from_db()


class BillTotalTests(BillingTestBase):
    def test_total_sums_item_snapshots(self):
        self.assertEqual(bill_total(self.bill), Decimal("51.00"))
        self.assertEqual(bill_total_halalas(self.bill), 5100)

    def test_total_excludes_cancelled_orders(self):
        cancelled = self._order(self.bill, OrderStatus.CANCELLED)
        self._item(cancelled, quantity=4)
        self.assertEqual(bill_total(self.bill), Decimal("51.00"))

    def test_total_excludes_voided_items(self):
        self._item(self.order, quantity=10, status=OrderItemStatus.VOIDED)
        self.assertEqual(bill_total(self.bill), Decimal("51.00"))

    def test_total_spans_multiple_orders(self):
        second = self._order(self.bill, OrderStatus.SERVED)
        self._item(second, quantity=1, price=Decimal("10.05"))
        self.assertEqual(bill_total(self.bill), Decimal("61.05"))
        self.assertEqual(bill_total_halalas(self.bill), 6105)

    def test_halalas_conversion_is_exact(self):
        for amount, expected in [("0.00", 0), ("0.01", 1), ("10.05", 1005),
                                 ("99.99", 9999), ("1234.56", 123456)]:
            self.assertEqual(to_halalas(Decimal(amount)), expected, amount)


class RequestPayTests(BillingTestBase):
    def url(self):
        return "/api/bills/request-pay/"

    def test_cash_request_locks_bill_and_flags_table(self):
        response = self.client.post(
            self.url(), {"session_token": self.table.session_token, "method": "cash"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], BillStatus.PAY_REQUESTED)
        self.assertEqual(response.data["total"], "51.00")
        self.assertEqual(response.data["total_halalas"], 5100)
        self.refresh()
        self.assertEqual(self.table.status, TableStatus.NEEDS_BILL)

    def test_online_request_returns_publishable_key_and_callback(self):
        response = self.client.post(
            self.url(), {"session_token": self.table.session_token, "method": "online"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["publishable_key"], PUBLISHABLE)
        self.assertEqual(
            response.data["callback_url"],
            f"https://web.example.test/pay/callback?t={self.table.session_token}",
        )

    def test_secret_key_never_appears_in_a_response(self):
        response = self.client.post(
            self.url(), {"session_token": self.table.session_token, "method": "online"}, format="json"
        )
        self.assertNotIn(SECRET, str(response.data))

    def test_accepts_the_raw_qr_payload_not_just_a_bare_token(self):
        response = self.client.post(
            self.url(), {"session_token": f"/t/{self.table.session_token}", "method": "cash"}, format="json"
        )
        self.assertEqual(response.status_code, 200)

    def test_refused_while_an_order_is_not_yet_served(self):
        self.order.status = OrderStatus.PREPARING
        self.order.save(update_fields=["status"])
        response = self.client.post(
            self.url(), {"session_token": self.table.session_token, "method": "cash"}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "error.billNotServed")

    def test_refused_on_an_empty_bill(self):
        self.order.status = OrderStatus.CANCELLED
        self.order.save(update_fields=["status"])
        response = self.client.post(
            self.url(), {"session_token": self.table.session_token, "method": "cash"}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "error.billEmpty")

    def test_repeat_request_is_idempotent(self):
        payload = {"session_token": self.table.session_token, "method": "cash"}
        first = self.client.post(self.url(), payload, format="json")
        second = self.client.post(self.url(), payload, format="json")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(Bill.objects.count(), 1)

    def test_method_can_be_switched_before_paying(self):
        self.client.post(self.url(), {"session_token": self.table.session_token, "method": "cash"}, format="json")
        response = self.client.post(
            self.url(), {"session_token": self.table.session_token, "method": "online"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["requested_method"], BillMethod.ONLINE)

    def test_refused_once_paid(self):
        self.client.post(self.url(), {"session_token": self.table.session_token, "method": "cash"}, format="json")
        self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "cash"}, format="json")
        self.client.force_authenticate(user=None)
        response = self.client.post(
            self.url(), {"session_token": self.table.session_token, "method": "cash"}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "error.billAlreadyPaid")

    @override_settings(MOYASAR_SECRET_KEY="")
    def test_online_refused_when_gateway_not_configured(self):
        response = self.client.post(
            self.url(), {"session_token": self.table.session_token, "method": "online"}, format="json"
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"], "error.paymentsNotConfigured")

    def test_unknown_token_is_404(self):
        response = self.client.post(self.url(), {"session_token": "nope", "method": "cash"}, format="json")
        self.assertEqual(response.status_code, 404)


class BillLockTests(BillingTestBase):
    """Requirement 1: once pay_requested, nothing new goes on the bill."""

    def request_cash(self):
        self.client.post(
            "/api/bills/request-pay/",
            {"session_token": self.table.session_token, "method": "cash"}, format="json",
        )

    def test_customer_cannot_place_a_new_qr_order(self):
        self.request_cash()
        response = self.client.post("/api/orders/qr/", {
            "session_token": self.table.session_token,
            "phone": "0500000000",
            "request_id": "abc-123",
            "items": [{"product": self.product.pk, "quantity": 1}],
            **AT_THE_RESTAURANT,
        }, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "error.billLocked")

    def test_refusing_a_qr_order_leaves_the_table_on_needs_bill(self):
        """A `return` inside transaction.atomic() commits, so the refused
        request must not have flipped the table back to `occupied` first."""
        self.request_cash()
        self.client.post("/api/orders/qr/", {
            "session_token": self.table.session_token, "phone": "0500000000",
            "request_id": "abc-124", "items": [{"product": self.product.pk, "quantity": 1}],
            **AT_THE_RESTAURANT,
        }, format="json")
        self.refresh()
        self.assertEqual(self.table.status, TableStatus.NEEDS_BILL)

    def test_pos_cannot_open_a_new_order_on_the_table(self):
        self.request_cash()
        response = self.as_staff(self.cashier).post(
            "/api/orders/", {"table": self.table.pk}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "error.billLocked")

    def test_staff_cannot_add_an_item_to_an_existing_order(self):
        self.request_cash()
        response = self.as_staff(self.cashier).post(
            f"/api/orders/{self.order.pk}/items/", {"product": self.product.pk, "quantity": 1}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "error.billLocked")

    def test_ordering_works_again_after_the_bill_is_reopened(self):
        self.request_cash()
        self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/reopen/", format="json")
        response = self.as_staff(self.cashier).post(
            f"/api/orders/{self.order.pk}/items/", {"product": self.product.pk, "quantity": 1}, format="json"
        )
        self.assertEqual(response.status_code, 201)


@mock.patch("orders.moyasar.fetch_payment")
class VerifyOnlineTests(BillingTestBase):
    def setUp(self):
        super().setUp()
        self.client.post(
            "/api/bills/request-pay/",
            {"session_token": self.table.session_token, "method": "online"}, format="json",
        )

    def verify(self, payment_id="pay_1", token=None):
        return self.client.post("/api/bills/verify-online/", {
            "session_token": token or self.table.session_token,
            "payment_id": payment_id,
        }, format="json")

    def test_happy_path_settles_the_bill(self, fetch):
        fetch.return_value = moyasar_payload(self.bill.pk, 5100)
        response = self.verify()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["result"], "settled")
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.PAID)
        self.assertEqual(self.bill.paid_method, BillMethod.ONLINE)
        self.assertEqual(self.bill.total_at_payment, Decimal("51.00"))
        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)
        self.assertEqual(Payment.objects.filter(order=self.order).count(), 1)

    def test_query_status_is_never_trusted(self, fetch):
        """The redirect says paid; the API says failed. The API wins."""
        fetch.return_value = moyasar_payload(self.bill.pk, 5100, status="failed")
        response = self.verify()
        self.assertEqual(response.data["result"], "failed")
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.PAY_REQUESTED)
        self.assertFalse(Payment.objects.exists())

    def test_amount_mismatch_is_flagged_not_dropped(self, fetch):
        fetch.return_value = moyasar_payload(self.bill.pk, 100)  # 1.00 SAR, not 51.00
        response = self.verify()
        self.assertEqual(response.data["result"], "needs_review")
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.PAY_REQUESTED)
        record = MoyasarPayment.objects.get(payment_id="pay_1")
        self.assertTrue(record.needs_review)
        self.assertIn("amount", record.review_reason)
        self.assertFalse(Payment.objects.exists())

    def test_metadata_for_another_bill_is_rejected(self, fetch):
        other_table = Table.objects.create(branch=self.branch, label_en="T2", label_ar="T2")
        other = Bill.objects.create(branch=self.branch, table=other_table)
        fetch.return_value = moyasar_payload(other.pk, 5100)
        response = self.verify()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "error.paymentVerificationFailed")
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.PAY_REQUESTED)

    def test_missing_metadata_is_rejected(self, fetch):
        payload = moyasar_payload(self.bill.pk, 5100)
        payload["metadata"] = {}
        fetch.return_value = payload
        response = self.verify()
        self.assertEqual(response.status_code, 400)

    def test_unknown_payment_id_is_404(self, fetch):
        from orders.moyasar import MoyasarPaymentNotFound
        fetch.side_effect = MoyasarPaymentNotFound("pay_x")
        response = self.verify("pay_x")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"], "error.paymentVerificationFailed")

    def test_gateway_down_is_502_and_leaves_the_bill_alone(self, fetch):
        from orders.moyasar import MoyasarUnreachable
        fetch.side_effect = MoyasarUnreachable("timeout")
        response = self.verify()
        self.assertEqual(response.status_code, 502)
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.PAY_REQUESTED)

    def test_verifying_with_another_tables_token_is_rejected(self, fetch):
        """Addition 3: the bill named in the metadata must belong to the table
        whose QR token was presented."""
        other_table = Table.objects.create(branch=self.branch, label_en="T9", label_ar="T9")
        other_table.open()
        fetch.return_value = moyasar_payload(self.bill.pk, 5100)
        response = self.verify(token=other_table.session_token)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "error.paymentVerificationFailed")
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.PAY_REQUESTED)

    def test_replaying_the_same_payment_id_changes_nothing(self, fetch):
        fetch.return_value = moyasar_payload(self.bill.pk, 5100)
        self.verify()
        response = self.verify()
        self.assertEqual(response.data["result"], "already_processed")
        self.assertEqual(MoyasarPayment.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)


@mock.patch("orders.moyasar.fetch_payment")
class WebhookTests(BillingTestBase):
    URL = "/api/payments/moyasar/webhook/"

    def setUp(self):
        super().setUp()
        self.client.post(
            "/api/bills/request-pay/",
            {"session_token": self.table.session_token, "method": "online"}, format="json",
        )

    def body(self, payment_id="pay_1", secret=None):
        payload = {
            "id": "evt_1",
            "type": "payment_paid",
            "created_at": "2026-10-05T10:00:00Z",
            "live": False,
            "data": {"id": payment_id, "status": "paid"},
        }
        if secret is not None:
            payload["secret_token"] = secret
        return payload

    def test_webhook_settles_without_the_customer_browser(self, fetch):
        fetch.return_value = moyasar_payload(self.bill.pk, 5100)
        response = self.client.post(self.URL, self.body(), format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["result"], "settled")
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.PAID)

    def test_webhook_body_is_not_trusted(self, fetch):
        """The body says paid; the API says failed. Nothing is settled."""
        fetch.return_value = moyasar_payload(self.bill.pk, 5100, status="failed")
        response = self.client.post(self.URL, self.body(), format="json")
        self.assertEqual(response.status_code, 200)
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.PAY_REQUESTED)

    def test_callback_then_webhook_settles_exactly_once(self, fetch):
        fetch.return_value = moyasar_payload(self.bill.pk, 5100)
        self.client.post("/api/bills/verify-online/", {
            "session_token": self.table.session_token, "payment_id": "pay_1",
        }, format="json")
        response = self.client.post(self.URL, self.body(), format="json")
        self.assertEqual(response.data["result"], "already_processed")
        self.assertEqual(MoyasarPayment.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Bill.objects.get(pk=self.bill.pk).total_at_payment, Decimal("51.00"))

    def test_webhook_then_callback_settles_exactly_once(self, fetch):
        fetch.return_value = moyasar_payload(self.bill.pk, 5100)
        self.client.post(self.URL, self.body(), format="json")
        response = self.client.post("/api/bills/verify-online/", {
            "session_token": self.table.session_token, "payment_id": "pay_1",
        }, format="json")
        self.assertEqual(response.data["result"], "already_processed")
        self.assertEqual(Payment.objects.count(), 1)

    def test_payload_without_a_payment_id_is_400(self, fetch):
        response = self.client.post(self.URL, {"type": "payment_paid", "data": {}}, format="json")
        self.assertEqual(response.status_code, 400)
        fetch.assert_not_called()

    def test_gateway_failure_returns_500_so_moyasar_retries(self, fetch):
        from orders.moyasar import MoyasarUnreachable
        fetch.side_effect = MoyasarUnreachable("boom")
        response = self.client.post(self.URL, self.body(), format="json")
        self.assertEqual(response.status_code, 500)

    @override_settings(MOYASAR_WEBHOOK_SECRET="shhh")
    def test_wrong_secret_token_is_rejected(self, fetch):
        response = self.client.post(self.URL, self.body(secret="wrong"), format="json")
        self.assertEqual(response.status_code, 403)
        fetch.assert_not_called()

    @override_settings(MOYASAR_WEBHOOK_SECRET="shhh")
    def test_missing_secret_token_is_rejected_when_one_is_configured(self, fetch):
        response = self.client.post(self.URL, self.body(), format="json")
        self.assertEqual(response.status_code, 403)

    @override_settings(MOYASAR_WEBHOOK_SECRET="shhh")
    def test_correct_secret_token_is_accepted(self, fetch):
        fetch.return_value = moyasar_payload(self.bill.pk, 5100)
        response = self.client.post(self.URL, self.body(secret="shhh"), format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["result"], "settled")


class CashierActionTests(BillingTestBase):
    def request_cash(self):
        self.client.force_authenticate(user=None)
        self.client.post(
            "/api/bills/request-pay/",
            {"session_token": self.table.session_token, "method": "cash"}, format="json",
        )

    def test_cashier_marks_cash_received(self):
        self.request_cash()
        response = self.as_staff(self.cashier).post(
            f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "cash"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.PAID)
        self.assertEqual(self.bill.paid_method, BillMethod.CASH)
        self.assertEqual(self.order.status, OrderStatus.PAID)
        self.assertEqual(Payment.objects.get(order=self.order).method, "cash")

    def test_cashier_can_take_a_card_at_the_till(self):
        self.request_cash()
        self.as_staff(self.cashier).post(
            f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "card"}, format="json"
        )
        self.refresh()
        self.assertEqual(self.bill.paid_method, BillMethod.CARD)
        self.assertEqual(Payment.objects.get(order=self.order).method, "card")

    def test_method_defaults_to_cash(self):
        self.request_cash()
        self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/mark-paid/", {}, format="json")
        self.refresh()
        self.assertEqual(self.bill.paid_method, BillMethod.CASH)

    def test_online_cannot_be_claimed_at_the_till(self):
        self.request_cash()
        response = self.as_staff(self.cashier).post(
            f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "online"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_double_tap_does_not_double_the_takings(self):
        self.request_cash()
        url = f"/api/bills/{self.bill.pk}/mark-paid/"
        self.as_staff(self.cashier).post(url, {"method": "cash"}, format="json")
        response = self.as_staff(self.cashier).post(url, {"method": "cash"}, format="json")
        self.assertEqual(response.data["result"], "already_processed")
        self.assertEqual(Payment.objects.count(), 1)

    def test_waiter_cannot_settle(self):
        self.request_cash()
        response = self.as_staff(self.waiter).post(
            f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "cash"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_kitchen_cannot_settle(self):
        self.request_cash()
        response = self.as_staff(self.kitchen).post(
            f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "cash"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_release_requires_payment_first(self):
        self.request_cash()
        response = self.as_staff(self.cashier).post(
            f"/api/bills/{self.bill.pk}/release-table/", format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "error.billNotPaid")

    def test_release_frees_the_table_and_closes_the_orders(self):
        self.request_cash()
        self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "cash"}, format="json")
        response = self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/release-table/", format="json")
        self.assertEqual(response.status_code, 200)
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.CLOSED)
        self.assertEqual(self.bill.released_by_id, self.cashier.pk)
        self.assertEqual(self.table.status, TableStatus.EMPTY)
        self.assertEqual(self.order.status, OrderStatus.CLOSED)

    def test_waiter_cannot_release(self):
        self.request_cash()
        self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "cash"}, format="json")
        response = self.as_staff(self.waiter).post(f"/api/bills/{self.bill.pk}/release-table/", format="json")
        self.assertEqual(response.status_code, 403)

    def test_reopen_unlocks_a_stuck_bill(self):
        self.request_cash()
        response = self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/reopen/", format="json")
        self.assertEqual(response.status_code, 200)
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.OPEN)
        self.assertEqual(self.bill.requested_method, "")
        self.assertEqual(self.table.status, TableStatus.OCCUPIED)

    def test_reopen_refused_once_paid(self):
        self.request_cash()
        self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "cash"}, format="json")
        response = self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/reopen/", format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "error.billAlreadyPaid")

    def test_customer_can_cancel_their_own_pay_request(self):
        self.request_cash()
        response = self.client.post("/api/bills/cancel-pay-request/", {
            "session_token": self.table.session_token,
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.OPEN)

    def test_customer_cannot_cancel_once_paid(self):
        self.request_cash()
        self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "cash"}, format="json")
        self.client.force_authenticate(user=None)
        response = self.client.post("/api/bills/cancel-pay-request/", {
            "session_token": self.table.session_token,
        }, format="json")
        self.assertEqual(response.status_code, 409)


class BillScopingTests(BillingTestBase):
    def test_cashier_cannot_touch_another_branchs_bill(self):
        other_branch = Branch.objects.create(name_en="Other", name_ar="Other")
        other_table = Table.objects.create(branch=other_branch, label_en="X1", label_ar="X1")
        other_bill = Bill.objects.create(branch=other_branch, table=other_table, status=BillStatus.PAID)
        client = self.as_staff(self.cashier)
        self.assertEqual(client.get(f"/api/bills/{other_bill.pk}/").status_code, 404)
        self.assertEqual(
            client.post(f"/api/bills/{other_bill.pk}/release-table/", format="json").status_code, 404
        )
        self.assertEqual(
            client.post(f"/api/bills/{other_bill.pk}/mark-paid/", {"method": "cash"}, format="json").status_code,
            404,
        )

    def test_queue_lists_only_this_branch(self):
        other_branch = Branch.objects.create(name_en="Other", name_ar="Other")
        other_table = Table.objects.create(branch=other_branch, label_en="X1", label_ar="X1")
        Bill.objects.create(branch=other_branch, table=other_table)
        response = self.as_staff(self.cashier).get("/api/bills/")
        ids = [row["id"] for row in response.data["results"]]
        self.assertEqual(ids, [self.bill.pk])

    def test_queue_filters_by_status(self):
        response = self.as_staff(self.cashier).get("/api/bills/?status=pay_requested")
        self.assertEqual(response.data["results"], [])

    def test_waiter_cannot_read_the_queue(self):
        self.assertEqual(self.as_staff(self.waiter).get("/api/bills/").status_code, 403)


class BillBySessionTests(BillingTestBase):
    def test_customer_sees_the_server_computed_total(self):
        response = self.client.get(f"/api/bills/by-session/{self.table.session_token}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total"], "51.00")
        self.assertEqual(response.data["total_halalas"], 5100)
        self.assertEqual(response.data["currency"], "SAR")
        self.assertTrue(response.data["ready_to_pay"])

    def test_unserved_orders_block_the_pay_button(self):
        second = self._order(self.bill, OrderStatus.PREPARING)
        self._item(second)
        response = self.client.get(f"/api/bills/by-session/{self.table.session_token}/")
        self.assertFalse(response.data["ready_to_pay"])
        self.assertEqual(response.data["unserved_order_ids"], [second.pk])

    def test_unknown_token_is_404(self):
        response = self.client.get("/api/bills/by-session/not-a-token/")
        self.assertEqual(response.status_code, 404)


class OpenBillLifecycleTests(BillingTestBase):
    def test_qr_order_creates_and_reuses_one_bill(self):
        table = Table.objects.create(branch=self.branch, label_en="T5", label_ar="T5")
        payload = {
            "session_token": table.session_token,
            "phone": "0500000001",
            "items": [{"product": self.product.pk, "quantity": 1}],
            **AT_THE_RESTAURANT,
        }
        first = self.client.post("/api/orders/qr/", {**payload, "request_id": "r1"}, format="json")
        self.assertEqual(first.status_code, 201)
        self.client.post("/api/orders/qr/", {**payload, "request_id": "r2"}, format="json")
        self.assertEqual(Bill.objects.filter(table=table).count(), 1)

    def test_a_released_table_opens_a_fresh_bill_for_the_next_party(self):
        self.client.post(
            "/api/bills/request-pay/",
            {"session_token": self.table.session_token, "method": "cash"}, format="json",
        )
        self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "cash"}, format="json")
        self.as_staff(self.cashier).post(f"/api/bills/{self.bill.pk}/release-table/", format="json")
        self.client.force_authenticate(user=None)

        response = self.client.post("/api/orders/qr/", {
            "session_token": self.table.session_token, "phone": "0500000002",
            "request_id": "r9", "items": [{"product": self.product.pk, "quantity": 1}],
            **AT_THE_RESTAURANT,
        }, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Bill.objects.filter(table=self.table).count(), 2)
        self.assertEqual(Bill.objects.filter(table=self.table, status=BillStatus.OPEN).count(), 1)
