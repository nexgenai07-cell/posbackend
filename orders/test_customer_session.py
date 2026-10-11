"""Customer session, order number, phone matching and waiter-flow regressions."""

import json
import time
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from customers.services import phones_match
from inventory.models import InventoryItem, RecipeItem, StockMovement
from tables.models import Table

from .models import Bill, CustomerOrderSubmission, Order, OrderHistory, OrderItem
from .serializers import OrderSerializer


def make_staff(branch, name, role, pin):
    staff = Staff(branch=branch, name=name, role=role)
    staff.set_pin(pin)
    staff.save()
    return staff


class CustomerSessionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(
            name_en="Main", name_ar="Main", timezone="Asia/Riyadh", currency="SAR",
            latitude="24.713600", longitude="46.675300",
        )
        cls.cashier = make_staff(cls.branch, "Cashier", StaffRole.CASHIER, "1001")
        cls.waiter = make_staff(cls.branch, "WaiterA", StaffRole.WAITER, "1002")
        cls.waiter_b = make_staff(cls.branch, "WaiterB", StaffRole.WAITER, "1003")
        cls.kitchen = make_staff(cls.branch, "Kitchen", StaffRole.KITCHEN, "1004")
        cls.manager = make_staff(cls.branch, "Manager", StaffRole.MANAGER, "1005")
        category = Category.objects.create(branch=cls.branch, name_en="Food", name_ar="Food")
        cls.fries = Product.objects.create(branch=cls.branch, category=category, name_en="Fries", name_ar="Fries",
                                           price="10.00", cost_price="3.00")
        cls.burger = Product.objects.create(branch=cls.branch, category=category, name_en="Burger", name_ar="Burger",
                                            price="25.00", cost_price="9.00")
        cls.potato = InventoryItem.objects.create(branch=cls.branch, name="Potato", unit="g", current_stock=100000)
        cls.patty = InventoryItem.objects.create(branch=cls.branch, name="Patty", unit="unit", current_stock=1000)
        RecipeItem.objects.create(product=cls.fries, inventory_item=cls.potato, quantity=100, unit="g")
        RecipeItem.objects.create(product=cls.burger, inventory_item=cls.patty, quantity=1, unit="unit")

    def setUp(self):
        cache.clear()
        self.public = APIClient()
        self.clients = {}
        self._key = 0

    # -- helpers -------------------------------------------------------------
    def staff_client(self, staff, pin):
        if staff.pk not in self.clients:
            cache.clear()
            client = APIClient()
            token = client.post("/api/auth/pin-login/", {"staff_id": staff.pk, "pin": pin}, format="json").json()["access"]
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            self.clients[staff.pk] = client
        return self.clients[staff.pk]

    @property
    def cashier_client(self):
        return self.staff_client(self.cashier, "1001")

    @property
    def waiter_client(self):
        return self.staff_client(self.waiter, "1002")

    @property
    def kitchen_client(self):
        return self.staff_client(self.kitchen, "1004")

    def new_table(self):
        return Table.objects.create(branch=self.branch, label_en="T", label_ar="T")

    def qr(self, table, phone="0501234567", items=None, key=None):
        self._key += 1
        return self.public.post("/api/orders/qr/", {
            "session_token": table.session_token, "phone": phone,
            "items": items or [{"product": self.fries.pk, "quantity": 1}],
            "latitude": 24.7136, "longitude": 46.6753, "accuracy_m": 0.2,
            "location_timestamp": time.time(), "request_id": key or f"key-{id(self)}-{self._key}",
        }, format="json")

    def item_status(self, client, order_id, item_id, value):
        return client.patch(f"/api/orders/{order_id}/items/{item_id}/status/", {"status": value}, format="json")

    def order_to_ready(self, table=None, phone="0501234567"):
        table = table or self.new_table()
        body = self.qr(table, phone).json()
        order_id, item_id = body["id"], body["items"][0]["id"]
        self.assertEqual(self.cashier_client.post(f"/api/orders/{order_id}/cashier-review/").status_code, 200)
        self.assertEqual(self.cashier_client.post(f"/api/orders/{order_id}/assign-waiter/",
                                                  {"waiter": self.waiter.pk}, format="json").status_code, 200)
        self.assertEqual(self.waiter_client.post(f"/api/orders/{order_id}/waiter-confirm/").status_code, 200)
        self.item_status(self.kitchen_client, order_id, item_id, "preparing")
        self.item_status(self.kitchen_client, order_id, item_id, "ready")
        return table, order_id, item_id, body

    def serve_everything(self, order_id):
        for item in OrderItem.objects.filter(order_id=order_id):
            if item.status == "fired":
                self.item_status(self.kitchen_client, order_id, item.pk, "preparing")
                item.refresh_from_db()
            if item.status == "preparing":
                self.item_status(self.kitchen_client, order_id, item.pk, "ready")
                item.refresh_from_db()
            if item.status == "ready":
                self.assertEqual(self.item_status(self.waiter_client, order_id, item.pk, "served").status_code, 200)

    def pay_and_release(self, bill_id):
        self.assertEqual(self.cashier_client.post(f"/api/bills/{bill_id}/mark-paid/", {"method": "cash"}, format="json").status_code, 200)
        self.assertEqual(self.cashier_client.post(f"/api/bills/{bill_id}/release-table/").status_code, 200)

    def track(self, code, phone):
        return self.public.get("/api/orders/track/", {"order_code": code, "phone": phone})


# =========================================================================== 1
class OrderNumberTests(CustomerSessionTestCase):
    def _walk(self, value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                yield from self._walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from self._walk(child, f"{path}[{index}]")
        else:
            yield path, value

    def assert_no_signed_value_displayed(self, payload, token):
        for path, value in self._walk(payload):
            if path.endswith("tracking_token"):
                continue
            self.assertNotIn(token, str(value), f"signed value leaked at {path}")

    def test_every_serializer_returns_short_order_number(self):
        table, order_id, item_id, qr_body = self.order_to_ready()
        expected = f"ORD-{order_id:08d}"
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.order_number, expected)
        self.assertEqual(order.order_code, expected)
        bill_id = qr_body["bill_id"]
        responses = {
            "qr_create": qr_body,
            "staff_detail": self.cashier_client.get(f"/api/orders/{order_id}/").json(),
            "staff_list": self.cashier_client.get("/api/orders/").json()["results"][0],
            "waiter_list": self.waiter_client.get("/api/orders/").json()["results"][0],
            "kds": self.kitchen_client.get("/api/kds/tickets/").json()["results"][0],
            "tracking": self.track(expected, "0501234567").json(),
            "session_open_order": self.public.get(f"/api/tables/by-session/{table.session_token}/", {"phone": "0501234567"}).json()["open_order"],
            "customer_bill_order": self.public.get(f"/api/bills/by-session/{table.session_token}/", {"phone": "0501234567"}).json()["orders"][0],
            "cashier_bill_order": self.cashier_client.get(f"/api/bills/{bill_id}/").json()["orders"][0],
            "cashier_bill_list_order": self.cashier_client.get("/api/bills/").json()["results"][0]["orders"][0],
            "history": self.cashier_client.get(f"/api/orders/{order_id}/history/").json()["results"][0],
            "report_recent": self.staff_client(self.manager, "1005").get("/api/reports/recent-orders/").json()[0],
        }
        for name, payload in responses.items():
            with self.subTest(name):
                self.assertEqual(payload["order_number"], expected)
                self.assert_no_signed_value_displayed(payload, order.tracking_token)
        # The signed value exists only as the hidden tracking token of public responses.
        self.assertEqual(qr_body["tracking_token"], order.tracking_token)
        self.assertNotEqual(qr_body["tracking_token"], expected)
        self.assertNotIn("tracking_token", responses["staff_detail"])

    def test_notification_payload_carries_short_order_number(self):
        from unittest.mock import patch

        table, order_id, item_id, _ = self.order_to_ready()
        sent = []
        with patch("orders.views.publish_staff_event", side_effect=lambda staff_id, name, payload: sent.append(payload)):
            self.cashier_client.post(f"/api/orders/{order_id}/items/", {"product": self.burger.pk}, format="json")
        self.assertTrue(sent)
        token = Order.objects.get(pk=order_id).tracking_token
        for payload in sent:
            self.assertEqual(payload["order_number"], f"ORD-{order_id:08d}")
            self.assertNotIn(token, json.dumps(payload))

    def test_staff_search_by_short_number(self):
        _, order_id, _, _ = self.order_to_ready()
        self.order_to_ready()
        for term in (f"ORD-{order_id:08d}", f"ord-{order_id}", f"{order_id:08d}", f"#{order_id}", str(order_id)):
            with self.subTest(term):
                rows = self.cashier_client.get("/api/orders/", {"search": term}).json()["results"]
                self.assertEqual([row["id"] for row in rows], [order_id])
        self.assertEqual(self.cashier_client.get("/api/orders/", {"search": "ORD-bogus"}).json()["results"], [])

    def test_tracking_token_is_stable_and_not_the_order_number(self):
        order = Order.objects.get(pk=self.qr(self.new_table()).json()["id"])
        self.assertEqual(order.tracking_token, Order.objects.get(pk=order.pk).tracking_token)
        self.assertNotIn(f"{order.pk:08d}", order.tracking_token)

    def test_lookup_accepts_short_number_legacy_and_tracking_token(self):
        from django.core import signing

        body = self.qr(self.new_table()).json()
        legacy_timestamped = "ORD-" + signing.dumps({"id": body["id"]}, salt="orders.public-order-code.v1", compress=True)
        for code in (body["order_number"], body["order_number"].lower(), body["tracking_token"], legacy_timestamped):
            with self.subTest(code):
                cache.clear()
                self.assertEqual(self.track(code, "0501234567").status_code, 200)


# =========================================================================== 3
class RateLimitTests(CustomerSessionTestCase):
    def test_successful_session_polling_is_not_rate_limited(self):
        table = self.new_table()
        self.qr(table)
        for _ in range(80):
            self.assertEqual(self.public.get(f"/api/bills/by-session/{table.session_token}/", {"phone": "0501234567"}).status_code, 200)
            self.assertEqual(self.public.get(f"/api/tables/by-session/{table.session_token}/", {"phone": "0501234567"}).status_code, 200)

    def test_successful_fallback_lookups_are_not_counted(self):
        body = self.qr(self.new_table()).json()
        for _ in range(45):
            self.assertEqual(self.track(body["order_number"], "0501234567").status_code, 200)

    def test_failed_fallback_lookups_are_limited(self):
        body = self.qr(self.new_table()).json()
        statuses = [self.track(body["order_number"], "0599999999").status_code for _ in range(30)]
        self.assertEqual(set(statuses), {404})
        self.assertEqual(self.track(body["order_number"], "0599999999").status_code, 429)
        self.assertEqual(self.track(body["order_number"], "0501234567").json()["error"], "error.tooManyRequests")


# =========================================================================== 4
class PhoneMatchingTests(CustomerSessionTestCase):
    SAUDI = ["0501234567", "+966501234567", "966501234567", "00966501234567", "050 123 4567", "٠٥٠١٢٣٤٥٦٧", "۰۵۰۱۲۳۴۵۶۷"]
    PAKISTAN = ["03001234567", "+923001234567", "923001234567"]

    def test_saudi_variants_match_each_other(self):
        for first in self.SAUDI:
            for second in self.SAUDI:
                self.assertTrue(phones_match(first, second), (first, second))

    def test_pakistani_variants_match_each_other(self):
        for first in self.PAKISTAN:
            for second in self.PAKISTAN:
                self.assertTrue(phones_match(first, second), (first, second))

    def test_different_numbers_and_short_numbers_do_not_match(self):
        self.assertFalse(phones_match("0501234567", "0501234568"))
        self.assertFalse(phones_match("0501234567", "03001234567"))
        self.assertFalse(phones_match("1234567", "1234567"))
        self.assertFalse(phones_match("", ""))

    def test_tracking_matches_every_saudi_variant_and_rejects_wrong_number_or_code(self):
        table = self.new_table()
        body = self.qr(table, phone="0501234567").json()
        other = self.qr(self.new_table(), phone="0507654321").json()
        for phone in self.SAUDI:
            with self.subTest(phone):
                cache.clear()
                self.assertEqual(self.track(body["order_number"], phone).status_code, 200)
        cache.clear()
        self.assertEqual(self.track(body["order_number"], "0509999999").status_code, 404)
        self.assertEqual(self.track(other["order_number"], "0501234567").status_code, 404)

    def test_pakistani_order_tracks_with_international_forms(self):
        body = self.qr(self.new_table(), phone="03001234567").json()
        for phone in self.PAKISTAN:
            with self.subTest(phone):
                cache.clear()
                self.assertEqual(self.track(body["order_number"], phone).status_code, 200)

    def test_raw_phone_is_kept_and_variants_reuse_one_customer(self):
        first = self.qr(self.new_table(), phone="+966 50 123 4567").json()
        second = self.qr(self.new_table(), phone="0501234567").json()
        one = Order.objects.get(pk=first["id"]).customer
        self.assertEqual(one.phone, "+966 50 123 4567")
        self.assertEqual(Order.objects.get(pk=second["id"]).customer_id, one.pk)


# =========================================================================== 5
class IdempotencyAfterReleaseTests(CustomerSessionTestCase):
    def test_same_key_after_release_creates_new_order(self):
        table = self.new_table()
        first = self.qr(table, key="seat-key").json()
        self.cashier_client.post(f"/api/orders/{first['id']}/cashier-review/")
        self.cashier_client.post(f"/api/orders/{first['id']}/assign-waiter/", {"waiter": self.waiter.pk}, format="json")
        self.waiter_client.post(f"/api/orders/{first['id']}/waiter-confirm/")
        self.serve_everything(first["id"])
        self.pay_and_release(first["bill_id"])
        again = self.qr(table, key="seat-key")
        self.assertEqual(again.status_code, 201)
        self.assertNotEqual(again.json()["id"], first["id"])
        self.assertNotEqual(again.json()["bill_id"], first["bill_id"])
        self.assertEqual(CustomerOrderSubmission.objects.get(idempotency_key="seat-key").order_id, again.json()["id"])
        # A plain retry of the new submission is idempotent again.
        self.assertEqual(self.qr(table, key="seat-key").json()["id"], again.json()["id"])

    def test_same_key_after_cancel_creates_new_order(self):
        table = self.new_table()
        first = self.qr(table, key="cancel-key").json()
        self.assertEqual(self.cashier_client.post(f"/api/orders/{first['id']}/cancel/").status_code, 200)
        again = self.qr(table, key="cancel-key")
        self.assertEqual(again.status_code, 201)
        self.assertNotEqual(again.json()["id"], first["id"])

    def test_same_key_different_cart_on_active_order_conflicts(self):
        table = self.new_table()
        self.qr(table, key="active-key")
        response = self.qr(table, key="active-key", items=[{"product": self.burger.pk, "quantity": 2}])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "error.idempotencyKeyConflict")


# =========================================================================== 6
class PinnedBillTests(CustomerSessionTestCase):
    def _served_and_paid(self, table):
        _, order_id, _, body = self.order_to_ready(table)
        self.serve_everything(order_id)
        return body["bill_id"]

    def test_pinned_current_bill_is_returned(self):
        table = self.new_table()
        bill_id = self.qr(table).json()["bill_id"]
        response = self.public.get(f"/api/bills/by-session/{table.session_token}/", {"bill_id": bill_id, "phone": "0501234567"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], bill_id)

    def test_pinned_bill_after_release_is_session_ended(self):
        table = self.new_table()
        bill_id = self._served_and_paid(table)
        self.pay_and_release(bill_id)
        response = self.public.get(f"/api/bills/by-session/{table.session_token}/", {"bill_id": bill_id, "phone": "0501234567"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"error": "error.billSessionEnded"})

    def test_pinned_bill_after_new_party_never_returns_new_bill(self):
        table = self.new_table()
        old_bill = self._served_and_paid(table)
        self.pay_and_release(old_bill)
        new_bill = self.qr(table, phone="0507777777", items=[{"product": self.burger.pk}]).json()["bill_id"]
        self.assertNotEqual(new_bill, old_bill)
        response = self.public.get(f"/api/bills/by-session/{table.session_token}/", {"bill_id": old_bill, "phone": "0501234567"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"error": "error.billSessionEnded"})
        self.assertNotIn("Burger", response.content.decode())


# =========================================================================== 7
class VoidedLinesTests(CustomerSessionTestCase):
    def test_voided_line_hidden_from_customer_kept_for_staff_totals_unchanged(self):
        table, order_id, fries_id, body = self.order_to_ready()
        burger_id = self.cashier_client.post(f"/api/orders/{order_id}/items/", {"product": self.burger.pk}, format="json").json()["id"]
        self.assertEqual(self.item_status(self.kitchen_client, order_id, burger_id, "voided").status_code, 200)

        tracking = self.track(body["order_number"], "0501234567").json()
        self.assertEqual([item["id"] for item in tracking["items"]], [fries_id])
        self.assertEqual(Decimal(str(tracking["total"])), Decimal("10.00"))

        customer_bill = self.public.get(f"/api/bills/by-session/{table.session_token}/", {"phone": "0501234567"}).json()
        self.assertEqual([item["id"] for item in customer_bill["orders"][0]["items"]], [fries_id])
        self.assertEqual(customer_bill["total"], "10.00")

        staff_order = self.cashier_client.get(f"/api/orders/{order_id}/").json()
        self.assertIn((burger_id, "voided"), [(item["id"], item["status"]) for item in staff_order["items"]])
        staff_bill = self.cashier_client.get(f"/api/bills/{body['bill_id']}/").json()
        self.assertIn((burger_id, "voided"), [(item["id"], item["status"]) for item in staff_bill["orders"][0]["items"]])
        self.assertEqual(staff_bill["total"], "10.00")


# =========================================================================== 8
class QrResponseBillStateTests(CustomerSessionTestCase):
    def test_qr_response_carries_bill_state_after_add_cancels_request(self):
        table, order_id, _, body = self.order_to_ready()
        self.assertEqual(body["bill_status"], "open")
        self.assertFalse(body["payment_request_cancelled_by_add"])
        self.serve_everything(order_id)
        self.assertEqual(self.public.post("/api/bills/request-pay/", {"session_token": table.session_token, "method": "cash", "phone": "0501234567"},
                                          format="json").status_code, 200)
        response = self.qr(table, items=[{"product": self.burger.pk}])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["bill_status"], "open")
        self.assertTrue(response.json()["payment_request_cancelled_by_add"])


# =========================================================================== 9
class WaiterFlowWhileExtraLineCooksTests(CustomerSessionTestCase):
    def _ready_fries_preparing_burger(self):
        table, order_id, fries_id, _ = self.order_to_ready()
        burger_id = self.cashier_client.post(f"/api/orders/{order_id}/items/", {"product": self.burger.pk}, format="json").json()["id"]
        self.item_status(self.kitchen_client, order_id, burger_id, "preparing")
        return order_id, fries_id, burger_id

    def test_waiter_serves_ready_line_while_other_line_cooks(self):
        order_id, fries_id, burger_id = self._ready_fries_preparing_burger()
        response = self.item_status(self.waiter_client, order_id, fries_id, "served")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(OrderItem.objects.get(pk=fries_id).status, "served")
        self.assertEqual(OrderItem.objects.get(pk=burger_id).status, "preparing")
        self.assertEqual(Order.objects.get(pk=order_id).status, "preparing")

    def test_parent_served_after_last_line_served(self):
        order_id, fries_id, burger_id = self._ready_fries_preparing_burger()
        self.item_status(self.waiter_client, order_id, fries_id, "served")
        self.item_status(self.kitchen_client, order_id, burger_id, "ready")
        self.assertEqual(Order.objects.get(pk=order_id).status, "ready")
        self.assertEqual(self.item_status(self.waiter_client, order_id, burger_id, "served").status_code, 200)
        self.assertEqual(Order.objects.get(pk=order_id).status, "served")

    def test_line_that_is_not_ready_cannot_be_served(self):
        order_id, fries_id, burger_id = self._ready_fries_preparing_burger()
        response = self.item_status(self.waiter_client, order_id, burger_id, "served")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "error.itemNotReady")
        self.assertEqual(OrderItem.objects.get(pk=burger_id).status, "preparing")

    def test_cashier_assigns_waiter_to_null_waiter_order_in_any_live_status(self):
        for target in ("preparing", "ready", "served"):
            with self.subTest(target):
                order_id, fries_id, burger_id = self._ready_fries_preparing_burger()
                if target in ("ready", "served"):
                    self.item_status(self.kitchen_client, order_id, burger_id, "ready")
                if target == "served":
                    self.serve_everything(order_id)
                Order.objects.filter(pk=order_id).update(assigned_waiter=None)
                before = (Order.objects.get(pk=order_id).status, sorted(OrderItem.objects.filter(order_id=order_id).values_list("pk", "status")))
                self.assertEqual(before[0], target)
                response = self.cashier_client.post(f"/api/orders/{order_id}/assign-waiter/", {"waiter": self.waiter_b.pk}, format="json")
                self.assertEqual(response.status_code, 200)
                order = Order.objects.get(pk=order_id)
                self.assertEqual(order.assigned_waiter_id, self.waiter_b.pk)
                self.assertEqual((order.status, sorted(OrderItem.objects.filter(order_id=order_id).values_list("pk", "status"))), before)

    def test_assigned_live_order_cannot_be_silently_reassigned(self):
        order_id, _, _ = self._ready_fries_preparing_burger()
        response = self.cashier_client.post(f"/api/orders/{order_id}/assign-waiter/", {"waiter": self.waiter_b.pk}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "error.orderAlreadyAssigned")


# ========================================================================== 10
class ConsistencyTests(CustomerSessionTestCase):
    def test_out_of_stock_is_409_for_add_and_dispatch(self):
        table, order_id, _, _ = self.order_to_ready()
        InventoryItem.objects.filter(pk=self.patty.pk).update(current_stock=0)
        staff_add = self.cashier_client.post(f"/api/orders/{order_id}/items/", {"product": self.burger.pk}, format="json")
        customer_add = self.qr(self.new_table(), items=[{"product": self.burger.pk}])
        for response in (staff_add, customer_add):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json(), {"error": "error.productUnavailable"})
        # Dispatch of a pending customer line that ran out of stock in between.
        InventoryItem.objects.filter(pk=self.patty.pk).update(current_stock=1)
        self.assertEqual(self.qr(table, items=[{"product": self.burger.pk}]).status_code, 200)
        InventoryItem.objects.filter(pk=self.patty.pk).update(current_stock=0)
        dispatch = self.cashier_client.post(f"/api/orders/{order_id}/cashier-review/")
        self.assertEqual(dispatch.status_code, 409)
        self.assertEqual(dispatch.json(), {"error": "error.productUnavailable"})

    def test_void_after_dispatch_writes_plain_text_history(self):
        _, order_id, fries_id, _ = self.order_to_ready()
        self.item_status(self.kitchen_client, order_id, fries_id, "voided")
        entry = OrderHistory.objects.get(order_id=order_id, event="item_voided_after_dispatch")
        self.assertEqual(entry.actor_id, self.kitchen.pk)
        self.assertIsNotNone(entry.created_at)
        self.assertEqual(entry.details["message"], "Line voided after dispatch, stock moved to waste")
        self.assertEqual(entry.details["waste_movements"], 1)
        self.assertEqual(StockMovement.objects.get(order_item_id=fries_id).reason, "waste")
