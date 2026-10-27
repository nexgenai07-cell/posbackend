from django.core.cache import cache
from django.test import TestCase
from unittest.mock import patch
import time
from unittest import skip
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from inventory.models import InventoryItem, RecipeItem

from .models import Table
from .services import parse_qr_payload


def make_staff(branch, name, role, pin, is_active=True):
    staff = Staff(branch=branch, name=name, role=role, is_active=is_active)
    staff.set_pin(pin)
    staff.save()
    return staff


# A realistic token_urlsafe(24) value — 32 chars of the base64url alphabet,
# the exact shape Table.open() mints.
TOKEN = "8Kq2vB1nR7xT4mZ9pLcW0aHdYs6UeJr3"


class ParseQrPayloadTests(TestCase):
    """Pure string handling of "what the customer's phone actually has" —
    the /t/<token> payload a scan returns, or a bare token from manual entry."""

    def test_bare_token_passes_through(self):
        self.assertEqual(parse_qr_payload(TOKEN), TOKEN)

    def test_qr_payload_prefix_is_stripped(self):
        self.assertEqual(parse_qr_payload(f"/t/{TOKEN}"), TOKEN)

    def test_absolute_url_is_accepted(self):
        self.assertEqual(parse_qr_payload(f"https://pos.smokeandchar.test/t/{TOKEN}"), TOKEN)

    def test_customer_app_deep_link_is_accepted(self):
        self.assertEqual(parse_qr_payload(f"smokeandchar://t/{TOKEN}"), TOKEN)

    def test_query_string_and_fragment_are_dropped(self):
        self.assertEqual(parse_qr_payload(f"https://host/t/{TOKEN}?ref=qr#top"), TOKEN)

    def test_pasted_whitespace_is_trimmed(self):
        """Manual entry: a stray space or newline from a paste must not 404."""
        self.assertEqual(parse_qr_payload(f"  /t/{TOKEN}\n"), TOKEN)

    def test_blank_inputs_return_empty(self):
        for raw in (None, "", "   ", "/t/", "///"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_qr_payload(raw), "")

    def test_non_token_garbage_returns_empty(self):
        """A human typing the wrong thing — most likely the table *label*."""
        for raw in ("Table 3", "abc 123", "!!!", "not/a/token"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_qr_payload(raw), "")


class TablesApiTestCase(TestCase):
    """Shared fixtures. Two branches on purpose — branch scoping has to be
    provably not just "the only branch that exists"."""

    @classmethod
    def setUpTestData(cls):
        cls.riyadh = Branch.objects.create(
            name_en="Smoke & Char — Riyadh",
            name_ar="سموك آند تشار — الرياض",
            address="King Fahd Road, Riyadh",
            timezone="Asia/Riyadh",
            currency="SAR",
            latitude="24.713600", longitude="46.675300",
        )
        cls.jeddah = Branch.objects.create(
            name_en="Smoke & Char — Jeddah",
            name_ar="سموك آند تشار — جدة",
            address="Tahlia Street, Jeddah",
            timezone="Asia/Riyadh",
            currency="SAR",
            latitude="21.485800", longitude="39.192500",
        )
        cls.owner = make_staff(cls.riyadh, "Amir", StaffRole.OWNER, "1111")
        cls.cashier = make_staff(cls.riyadh, "Junaid", StaffRole.CASHIER, "3333")
        cls.waiter = make_staff(cls.riyadh, "Bilal", StaffRole.WAITER, "4444")
        cls.jeddah_owner = make_staff(cls.jeddah, "Faisal", StaffRole.OWNER, "5555")

        # open() mints the session token / QR payload — never hand-written.
        cls.open_table = Table.objects.create(branch=cls.riyadh, label_en="Table 3", label_ar="طاولة ٣")
        cls.open_table.open()
        cls.empty_table = Table.objects.create(branch=cls.riyadh, label_en="Table 4", label_ar="طاولة ٤")
        cls.other_branch_table = Table.objects.create(branch=cls.jeddah, label_en="Table 9", label_ar="طاولة ٩")
        cls.other_branch_table.open()

        cls.category = Category.objects.create(branch=cls.riyadh, name_en="Burgers", name_ar="برغر")
        cls.product = Product.objects.create(
            branch=cls.riyadh,
            category=cls.category,
            name_en="Smash Burger",
            name_ar="سماش برغر",
            price="25.00",
            cost_price="11.00",
        )
        ingredient = InventoryItem.objects.create(
            branch=cls.riyadh, name="Burger patty", unit="unit", current_stock=1000,
        )
        RecipeItem.objects.create(product=cls.product, inventory_item=ingredient, quantity=1, unit="unit")

    def setUp(self):
        self.client = APIClient()
        # Throttle history lives in the shared LocMemCache and is *not* reset
        # between tests (same reason accounts/tests.py clears it) — otherwise
        # table-lookup's 60/min would leak across this file's classes.
        cache.clear()
        self._idempotency_number = 0

    def post_qr(self, payload, key=None):
        self._idempotency_number += 1
        request_key = key or f"test-qr-{id(self)}-{self._idempotency_number}"
        payload = {"latitude": 24.7136, "longitude": 46.6753, "accuracy_m": 0.2,
                   "location_timestamp": time.time(), **payload}
        return self.client.post("/api/orders/qr/", payload, format="json", HTTP_IDEMPOTENCY_KEY=request_key)

    def auth_as(self, staff, pin):
        access = self.client.post(
            "/api/auth/pin-login/", {"staff_id": staff.id, "pin": pin}, format="json"
        ).json()["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

class ActiveSessionsTests(TablesApiTestCase):
    """
    `GET /api/tables/active-sessions/` — the "what code do I type?" endpoint.
    A table's code only exists in session_token while it's open, and grabbing
    it otherwise means opening Django admin or a shell.
    """

    url = "/api/tables/active-sessions/"

    def test_anonymous_is_unauthorized(self):
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cashier_can_read(self):
        """Read parity with list/retrieve, which already return session_token
        to any authenticated staff — this must not be a stricter gate."""
        self.auth_as(self.cashier, "3333")
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)

    def test_returns_the_open_tables_code_and_qr_payload(self):
        self.auth_as(self.owner, "1111")
        body = self.client.get(self.url).json()
        self.assertEqual(body["count"], 1)
        row = body["results"][0]
        self.assertEqual(row["id"], self.open_table.id)
        self.assertEqual(row["session_token"], self.open_table.session_token)
        self.assertEqual(row["qr_code"], f"/t/{self.open_table.session_token}")

    def test_a_table_that_was_never_opened_is_absent(self):
        self.auth_as(self.owner, "1111")
        ids = [r["id"] for r in self.client.get(self.url).json()["results"]]
        self.assertNotIn(self.empty_table.id, ids)

    def test_closing_hides_table_from_active_list_but_keeps_permanent_qr(self):
        self.auth_as(self.owner, "1111")
        stale_token = self.open_table.session_token
        self.open_table.close()
        body = self.client.get(self.url).json()
        self.assertEqual(body["count"], 0)
        self.assertNotIn(stale_token, [r["session_token"] for r in body["results"]])
        session = self.client.get(f"/api/tables/by-session/{stale_token}/")
        self.assertEqual(session.status_code, status.HTTP_200_OK)
        self.assertEqual(session.json()["table"]["session_token"], stale_token)

    def test_other_branchs_open_table_is_not_leaked(self):
        self.auth_as(self.jeddah_owner, "5555")
        ids = [r["id"] for r in self.client.get(self.url).json()["results"]]
        self.assertEqual(ids, [self.other_branch_table.id])


class TableBySessionTests(TablesApiTestCase):
    """The public resolve-a-scan call (also the mobile app's status refresh)."""

    def url(self, token):
        return f"/api/tables/by-session/{token}/"

    def test_is_public_and_resolves_the_bare_token(self):
        """No token: the customer's phone has no staff JWT."""
        response = self.client.get(self.url(self.open_table.session_token))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["table"]["id"], self.open_table.id)
        self.assertIsNone(response.json()["open_order"])

    def test_an_unknown_token_is_not_found(self):
        response = self.client.get(self.url("thisTokenDoesNotExist0000000000"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json()["error"], "error.notFound")

    def test_a_closed_table_keeps_its_permanent_qr_token(self):
        token = self.open_table.session_token
        self.open_table.close()
        response = self.client.get(self.url(token))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["table"]["status"], "empty")
    def test_never_opened_table_has_a_permanent_qr_token(self):
        self.assertTrue(self.empty_table.session_token)
        self.assertEqual(self.empty_table.qr_code, f"/t/{self.empty_table.session_token}")
        response = self.client.get(self.url(self.empty_table.session_token))
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class PublicQrOrderTests(TablesApiTestCase):
    """
    `POST /api/orders/qr/` reads session_token from the *body* — so this is
    where the raw "/t/<token>" a scan returns really can be posted straight
    through, with no client-side string handling.
    """

    url = "/api/orders/qr/"

    def payload(self, token, phone="0770000001"):
        return {
            "session_token": token,
            "phone": phone,
            "latitude": 24.7136,
            "longitude": 46.6753,
            "accuracy_m": 0.2,
            "location_timestamp": time.time(),
            "items": [{"product": self.product.id, "quantity": 1}],
        }

    def error_code(self, response):
        # Field-level errors come back list-wrapped (see common/exceptions.py
        # and accounts/tests.py's helper); view-level ones are a plain string.
        value = response.json()["error"]
        return value[0] if isinstance(value, list) else value

    def test_full_qr_payload_is_accepted(self):
        payload = self.payload(f"/t/{self.open_table.session_token}")
        payload["items"][0]["notes"] = "No onions"
        response = self.post_qr(payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body["table"], self.open_table.id)
        self.assertEqual(body["source"], "qr")
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["name_en_snapshot"], "Smash Burger")
        self.assertEqual(body["items"][0]["notes"], "No onions")

    def test_geofence_accepts_exact_boundary_and_rejects_outside_without_creating_order(self):
        from math import degrees
        import time

        at_boundary = self.payload(self.open_table.session_token)
        at_boundary.update(latitude=24.7136 + degrees(1.9 / 6_371_000), accuracy_m=0.1, location_timestamp=time.time())
        self.assertEqual(self.post_qr(at_boundary).status_code, status.HTTP_201_CREATED)

        self.open_table.close()
        outside = self.payload(self.open_table.session_token, phone="0770000088")
        outside.update(latitude=24.7136 + degrees(2.2 / 6_371_000), accuracy_m=0.1, location_timestamp=time.time())
        response = self.post_qr(outside)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        from orders.models import Order
        self.assertFalse(Order.objects.filter(customer__phone="0770000088").exists())

    def test_geofence_rejects_missing_or_stale_customer_gps(self):
        import time

        for key, value in (("latitude", None), ("location_timestamp", time.time() - 180)):
            payload = self.payload(self.open_table.session_token, phone=f"07700000{90 + len(key)}")
            payload["request_id"] = f"missing-gps-{key}"
            payload[key] = value
            response = self.client.post(self.url, payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            if key == "location_timestamp":
                # Raised against the field (common/geofence.py), so it is keyed by it.
                self.assertEqual(response.json()["location_timestamp"], "error.locationStale")
        from orders.models import Order
        self.assertFalse(Order.objects.filter(table=self.open_table).exists())

    def test_bare_token_works_and_appends_to_the_same_order(self):
        """A guest ordering a starter then a dessert must land on one bill."""
        first = self.post_qr(self.payload(f"/t/{self.open_table.session_token}"))
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        second = self.post_qr(self.payload(self.open_table.session_token))
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertEqual(len(second.json()["items"]), 2)

    def test_existing_customer_tracking_sees_lines_appended_to_the_same_order(self):
        from orders.models import Order, OrderStatus

        phone = "0770000011"
        first = self.post_qr(self.payload(self.open_table.session_token, phone=phone))
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        Order.objects.filter(pk=first.json()["id"]).update(status=OrderStatus.SENT)

        second = self.post_qr(self.payload(self.open_table.session_token, phone=phone))
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(second.json()["id"], first.json()["id"])
        tracked = self.client.get("/api/orders/track/", {
            "order_code": first.json()["order_code"], "phone": phone,
        })
        self.assertEqual(len(tracked.json()["items"]), 2)
        self.assertTrue(any(item["is_new"] for item in tracked.json()["items"]))
        self.assertEqual(tracked.json()["bill_id"], first.json()["bill_id"])
        session = self.client.get(f"/api/tables/by-session/{self.open_table.session_token}/", {"phone": phone})
        self.assertEqual(session.json()["bill_id"], first.json()["bill_id"])
        # Without the phone the table token reveals no order or bill.
        anonymous = self.client.get(f"/api/tables/by-session/{self.open_table.session_token}/").json()
        self.assertIsNone(anonymous["bill_id"])
        self.assertIsNone(anonymous["open_order"])

    def test_tracking_phone_normalization_accepts_local_and_international_formats(self):
        submitted = self.post_qr(self.payload(self.open_table.session_token, phone="03001234567"))
        self.assertEqual(submitted.status_code, status.HTTP_201_CREATED)
        for phone in ("03001234567", "+923001234567", "923001234567"):
            with self.subTest(phone=phone):
                response = self.client.get("/api/orders/track/", {
                    "order_code": submitted.json()["order_code"], "phone": phone,
                })
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_tracking_order_code_is_signed_and_tampering_is_rejected(self):
        # The displayed number is short; only the hidden tracking_token is signed.
        submitted = self.post_qr(self.payload(self.open_table.session_token))
        self.assertEqual(submitted.json()["order_number"], f"ORD-{submitted.json()['id']:08d}")
        code = submitted.json()["tracking_token"]
        self.assertNotEqual(code, submitted.json()["order_number"])
        tampered = code[:-1] + ("A" if code[-1] != "A" else "B")
        response = self.client.get("/api/orders/track/", {
            "order_code": tampered, "phone": "0770000001",
        })
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_public_order_tracking_is_rate_limited(self):
        from django.core.cache import cache

        # Only FAILED lookups count toward the limit; successful ones never do.
        submitted = self.post_qr(self.payload(self.open_table.session_token))
        cache.clear()
        url = "/api/orders/track/"
        good = {"order_code": submitted.json()["order_number"], "phone": "0770000001"}
        bad = {"order_code": submitted.json()["order_number"], "phone": "0779999999"}
        self.assertTrue(all(self.client.get(url, good).status_code == 200 for _ in range(31)))
        failures = [self.client.get(url, bad) for _ in range(31)]
        self.assertEqual(failures[-1].status_code, 429)
        cache.clear()

    def test_customer_tracking_returns_per_line_status_and_batch_after_each_poll(self):
        from customers.models import Customer
        from orders.models import Order, OrderHistory, OrderItem, OrderItemStatus, OrderStatus

        customer = Customer.objects.create(branch=self.riyadh, phone="923000000001")
        order = Order.objects.create(branch=self.riyadh, table=self.open_table, customer=customer, status=OrderStatus.PREPARING)
        served = OrderItem.objects.create(order=order, product=self.product,
            name_en_snapshot="Original", name_ar_snapshot="Original", price_snapshot=10,
            status=OrderItemStatus.SERVED)
        later = OrderItem.objects.create(order=order, product=self.product,
            name_en_snapshot="Later", name_ar_snapshot="Later", price_snapshot=10,
            status=OrderItemStatus.PREPARING)
        OrderHistory.objects.create(order=order, event="cashier_item_added_and_sent",
            details={"item_ids": [later.pk]})
        params = {"order_code": order.order_code, "phone": "+923000000001"}
        poll = self.client.get("/api/orders/track/", params)
        lines = {row["id"]: row for row in poll.json()["items"]}
        self.assertEqual(lines[served.pk]["status"], OrderItemStatus.SERVED)
        self.assertTrue(lines[later.pk]["is_additional_batch"])
        self.assertEqual(lines[later.pk]["status"], OrderItemStatus.PREPARING)
        for next_status in (OrderItemStatus.READY, OrderItemStatus.SERVED):
            OrderItem.objects.filter(pk=later.pk).update(status=next_status)
            poll = self.client.get("/api/orders/track/", params)
            lines = {row["id"]: row for row in poll.json()["items"]}
            self.assertEqual(lines[later.pk]["status"], next_status)

    def test_ready_cashier_add_is_sent_once_and_redundant_waiter_send_returns_no_pending(self):
        from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus

        order = Order.objects.create(branch=self.riyadh, table=self.open_table,
            assigned_waiter=self.waiter, status=OrderStatus.READY)
        OrderItem.objects.create(order=order, product=self.product, name_en_snapshot="Ready",
            name_ar_snapshot="Ready", price_snapshot=25, status=OrderItemStatus.READY)
        self.client.force_authenticate(user=self.cashier)
        added = self.client.post(f"/api/orders/{order.pk}/items/", {"product": self.product.pk}, format="json")
        self.assertEqual(added.status_code, status.HTTP_201_CREATED)
        self.assertEqual(added.json()["status"], OrderItemStatus.FIRED)
        self.client.force_authenticate(user=self.waiter)
        resent = self.client.post(f"/api/orders/{order.pk}/send-to-kitchen/", {}, format="json")
        self.assertEqual(resent.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resent.json()["error"], "error.noPendingItems")

    def test_garbage_token_is_rejected_as_invalid(self):
        response = self.post_qr(self.payload("Table 3"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.error_code(response), "error.invalidSessionToken")

    def test_unknown_well_formed_qr_token_is_rejected_with_clear_code(self):
        response = self.post_qr(self.payload("A" * 32))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.error_code(response), "error.invalidSessionToken")

    def test_qr_still_works_after_close(self):
        token = self.open_table.session_token
        self.open_table.close()
        response = self.post_qr(self.payload(token))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_retry_with_same_request_key_does_not_duplicate_lines(self):
        payload = self.payload(self.open_table.session_token)
        first = self.post_qr(payload, "same-cart-request")
        retry = self.post_qr(payload, "same-cart-request")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(retry.status_code, status.HTTP_200_OK)
        self.assertEqual(first.json()["id"], retry.json()["id"])
        self.assertEqual(len(retry.json()["items"]), 1)

    def test_mobile_request_id_body_is_supported_for_retries(self):
        payload = self.payload(self.open_table.session_token)
        payload["request_id"] = "mobile-body-retry"
        first = self.client.post(self.url, payload, format="json")
        retry = self.client.post(self.url, payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(retry.status_code, status.HTTP_200_OK)
        self.assertEqual(len(retry.json()["items"]), 1)

    def test_request_key_cannot_be_reused_for_different_cart(self):
        key = "request-key-payload-conflict"
        self.post_qr(self.payload(self.open_table.session_token), key)
        changed = self.payload(self.open_table.session_token)
        changed["items"][0]["quantity"] = 2
        response = self.post_qr(changed, key)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.error_code(response), "error.idempotencyKeyConflict")

    def test_customer_submission_requires_idempotency_key(self):
        response = self.client.post(self.url, self.payload(self.open_table.session_token), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.error_code(response), "error.idempotencyKeyRequired")

    def test_item_must_be_available_and_belong_to_table_branch(self):
        from catalog.models import Category, Product
        other_category = Category.objects.create(branch=self.jeddah, name_en="Other", name_ar="Other")
        other_product = Product.objects.create(branch=self.jeddah, category=other_category,
            name_en="Other branch", name_ar="Other", price="5.00", cost_price="2.00")
        for product_id in (other_product.id,):
            payload = self.payload(self.open_table.session_token)
            payload["items"][0]["product"] = product_id
            self.assertEqual(self.post_qr(payload).status_code, status.HTTP_409_CONFLICT)
        unavailable = Product.objects.create(branch=self.riyadh, category=self.category,
            name_en="Unavailable", name_ar="Unavailable", price="5.00", cost_price="2.00", is_available=False)
        payload = self.payload(self.open_table.session_token)
        payload["items"][0]["product"] = unavailable.id
        response = self.post_qr(payload)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json(), {"error": "error.productUnavailable"})

    def test_table_session_returns_new_active_order_not_old_paid_order(self):
        from customers.services import find_or_create_customer
        from orders.models import Bill, BillStatus, Order, OrderStatus
        customer, _ = find_or_create_customer(self.riyadh, "0770000012")
        old_bill = Bill.objects.create(branch=self.riyadh, table=self.open_table, customer=customer, status=BillStatus.CLOSED)
        bill = Bill.objects.create(branch=self.riyadh, table=self.open_table, customer=customer)
        old = Order.objects.create(branch=self.riyadh, table=self.open_table, customer=customer, bill=old_bill, status=OrderStatus.CLOSED)
        current = Order.objects.create(branch=self.riyadh, table=self.open_table, customer=customer, bill=bill, status=OrderStatus.PENDING_CASHIER)
        response = self.client.get(f"/api/tables/by-session/{self.open_table.session_token}/", {"phone": "+966770000012"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["open_order"]["id"], current.id)
        self.assertNotEqual(response.json()["open_order"]["id"], old.id)


class RestaurantOrderWorkflowTests(TablesApiTestCase):
    def setUp(self):
        super().setUp()
        self.waiter = make_staff(self.riyadh, "Sara", StaffRole.WAITER, "4444")
        self.kitchen = make_staff(self.riyadh, "Ali", StaffRole.KITCHEN, "6666")

    def test_order_date_filters_use_the_branch_timezone(self):
        from datetime import datetime, timezone as datetime_timezone
        from orders.models import Order

        order = Order.objects.create(branch=self.riyadh, table=self.open_table)
        Order.objects.filter(pk=order.pk).update(opened_at=datetime(2026, 10, 2, 22, 30, tzinfo=datetime_timezone.utc))
        self.auth_as(self.cashier, "3333")
        today_at_branch = self.client.get("/api/orders/?from=2026-10-03&to=2026-10-03")
        previous_day = self.client.get("/api/orders/?from=2026-10-02&to=2026-10-02")
        self.assertIn(order.pk, [row["id"] for row in today_at_branch.json()["results"]])
        self.assertNotIn(order.pk, [row["id"] for row in previous_day.json()["results"]])

    def test_cashiers_can_see_branch_orders_but_not_other_branch_orders(self):
        from orders.models import Order, OrderStatus, Payment

        cashier_b = make_staff(self.riyadh, "Mona", StaffRole.CASHIER, "7777")
        same_branch = Order.objects.create(branch=self.riyadh, table=self.open_table, assigned_cashier=cashier_b, status=OrderStatus.PAID)
        second_same_branch = Order.objects.create(branch=self.riyadh, table=self.empty_table, assigned_cashier=cashier_b, status=OrderStatus.PAID)
        payment = Payment.objects.create(order=same_branch, amount="25.00", method="cash")
        other_branch = Order.objects.create(branch=self.jeddah, table=self.other_branch_table)
        self.auth_as(self.cashier, "3333")
        listed_ids = [row["id"] for row in self.client.get("/api/orders/").json()["results"]]
        self.assertIn(same_branch.pk, listed_ids)
        self.assertIn(second_same_branch.pk, listed_ids)
        self.assertNotIn(other_branch.pk, listed_ids)
        first_page = self.client.get(f"/api/orders/?cashier={cashier_b.pk}&status=paid&page_size=1").json()
        second_page = self.client.get(f"/api/orders/?cashier={cashier_b.pk}&status=paid&page_size=1&page=2").json()
        self.assertEqual(first_page["count"], 2)
        self.assertEqual(len(first_page["results"]), 1)
        self.assertEqual(len(second_page["results"]), 1)
        self.assertNotEqual(first_page["results"][0]["id"], second_page["results"][0]["id"])
        detail = self.client.get(f"/api/orders/{same_branch.pk}/")
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.json()["payments"][0]["id"], payment.pk)
        self.assertEqual(self.client.get(f"/api/orders/{other_branch.pk}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.post(
            f"/api/orders/{other_branch.pk}/payments/",
            {"amount": "1.00", "method": "cash", "request_id": "foreign-branch-payment"},
            format="json",
        ).status_code, status.HTTP_404_NOT_FOUND)
        self.auth_as(self.owner, "1111")
        self.assertIn(same_branch.pk, [row["id"] for row in self.client.get("/api/orders/").json()["results"]])

    def test_waiter_list_detail_and_history_are_limited_to_assigned_orders(self):
        from orders.models import Order

        other_waiter = make_staff(self.riyadh, "Layla", StaffRole.WAITER, "8888")
        assigned = Order.objects.create(branch=self.riyadh, table=self.open_table, assigned_waiter=self.waiter)
        restricted = Order.objects.create(branch=self.riyadh, table=self.empty_table, assigned_waiter=other_waiter)
        self.auth_as(self.waiter, "4444")
        self.assertEqual([row["id"] for row in self.client.get("/api/orders/").json()["results"]], [assigned.pk])
        self.assertEqual(self.client.get(f"/api/orders/{assigned.pk}/").status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(f"/api/orders/{restricted.pk}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(f"/api/orders/{restricted.pk}/history/").status_code, status.HTTP_404_NOT_FOUND)

    def test_unassigned_ready_order_can_be_recovered_by_assigning_a_waiter(self):
        """A ready ticket without an assignee is visible to KDS but to no waiter's queue."""
        from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus

        order = Order.objects.create(
            branch=self.riyadh, table=self.open_table, status=OrderStatus.READY,
        )
        item = OrderItem.objects.create(
            order=order, product=self.product, name_en_snapshot="Burger", name_ar_snapshot="Burger",
            price_snapshot="25.00", quantity=1, status=OrderItemStatus.READY,
        )

        self.auth_as(self.kitchen, "6666")
        tickets = self.client.get("/api/kds/tickets/").json()["results"]
        self.assertTrue(any(row["id"] == order.pk for row in tickets))

        self.auth_as(self.waiter, "4444")
        self.assertNotIn(order.pk, [row["id"] for row in self.client.get("/api/orders/").json()["results"]])

        self.auth_as(self.cashier, "3333")
        assigned = self.client.post(
            f"/api/orders/{order.pk}/assign-waiter/", {"waiter": self.waiter.pk}, format="json",
        )
        self.assertEqual(assigned.status_code, status.HTTP_200_OK)
        self.assertEqual(assigned.json()["status"], OrderStatus.READY)
        self.assertEqual(assigned.json()["items"][0]["id"], item.pk)

    def test_cashier_unassigned_filter_includes_ready_order_and_kitchen_ticket(self):
        from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus

        order = Order.objects.create(branch=self.riyadh, table=self.open_table, status=OrderStatus.READY)
        OrderItem.objects.create(order=order, product=self.product, name_en_snapshot="Burger",
            name_ar_snapshot="Burger", price_snapshot=25, status=OrderItemStatus.READY)
        self.auth_as(self.cashier, "3333")
        listing = self.client.get("/api/orders/?waiter=unassigned").json()["results"]
        self.assertIn(order.pk, [row["id"] for row in listing])
        self.auth_as(self.waiter, "4444")
        self.assertNotIn(order.pk, [row["id"] for row in self.client.get("/api/orders/").json()["results"]])
        self.auth_as(self.kitchen, "6666")
        tickets = self.client.get("/api/kds/tickets/").json()["results"]
        self.assertIn(order.pk, [row["id"] for row in tickets])

    @skip("Needs PostgreSQL: SQLite does not enforce select_for_update row locks.")
    def test_concurrent_cashier_waiter_customer_adds_serialize_under_order_lock(self):
        """Run this concurrency integration case against PostgreSQL, not SQLite."""
        self.fail("PostgreSQL concurrency test fixture is required to assert serialized row locks.")

    def test_released_table_can_start_a_new_qr_order(self):
        from decimal import Decimal
        from orders.models import Bill, BillStatus, Order, OrderItem, OrderItemStatus, OrderStatus

        bill = Bill.objects.create(branch=self.riyadh, table=self.open_table, status=BillStatus.PAID)
        order = Order.objects.create(
            branch=self.riyadh, table=self.open_table, bill=bill, status=OrderStatus.PAID,
        )
        OrderItem.objects.create(
            order=order, product=self.product, name_en_snapshot="Burger", name_ar_snapshot="Burger",
            price_snapshot=Decimal("25.00"), quantity=1, status=OrderItemStatus.SERVED,
        )
        self.auth_as(self.cashier, "3333")
        released = self.client.post(f"/api/bills/{bill.pk}/release-table/", format="json")
        self.assertEqual(released.status_code, status.HTTP_200_OK)

        self.client.credentials()
        placed = self.post_qr({
            "session_token": self.open_table.session_token,
            "phone": "0770000099",
            "items": [{"product": self.product.pk, "quantity": 1}],
        })
        self.assertEqual(placed.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(placed.json()["id"], order.pk)

    def test_kitchen_ticket_response_excludes_cashier_payment_and_customer_data(self):
        from orders.models import Order, OrderItem, OrderItemStatus

        order = Order.objects.create(branch=self.riyadh, table=self.open_table, customer_id=None, assigned_cashier=self.cashier, assigned_waiter=self.waiter)
        item = OrderItem.objects.create(order=order, product=self.product, name_en_snapshot="Burger", name_ar_snapshot="Burger", price_snapshot="99.00", quantity=1, status=OrderItemStatus.FIRED)
        OrderItem.objects.create(order=order, product=self.product, name_en_snapshot="Pending", name_ar_snapshot="Pending", price_snapshot="50.00", quantity=2, status=OrderItemStatus.PENDING)
        self.auth_as(self.kitchen, "6666")
        response = self.client.get("/api/kds/tickets/")
        row = next(row for row in response.json()["results"] if row["id"] == order.pk)
        self.assertEqual([line["id"] for line in row["items"]], [item.pk])
        self.assertNotIn("payments", row)
        self.assertNotIn("customer", row)
        self.assertNotIn("assigned_cashier", row)
        self.assertNotIn("price_snapshot", row["items"][0])
        self.assertEqual(self.client.get(f"/api/orders/{order.pk}/").status_code, status.HTTP_403_FORBIDDEN)

    def test_cashier_adds_to_ready_order_dispatches_only_new_batch(self):
        from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus

        order = Order.objects.create(
            branch=self.riyadh, table=self.open_table, assigned_waiter=self.waiter, status=OrderStatus.READY,
        )
        old_item = OrderItem.objects.create(
            order=order, product=self.product, name_en_snapshot="Ready burger", name_ar_snapshot="Ready burger",
            price_snapshot="25.00", quantity=1, status=OrderItemStatus.READY,
        )
        self.client.force_authenticate(user=self.cashier)
        layer = get_channel_layer()
        waiter_channel = async_to_sync(layer.new_channel)()
        kitchen_channel = async_to_sync(layer.new_channel)()
        async_to_sync(layer.group_add)(f"staff_{self.waiter.pk}_events", waiter_channel)
        async_to_sync(layer.group_add)(f"staff_{self.kitchen.pk}_events", kitchen_channel)
        with self.captureOnCommitCallbacks(execute=True):
            added = self.client.post(f"/api/orders/{order.pk}/items/", {"product": self.product.pk}, format="json")
        self.assertEqual(added.status_code, status.HTTP_201_CREATED)
        new_item_id = added.json()["id"]
        order.refresh_from_db()
        old_item.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PREPARING)
        self.assertEqual(order.assigned_waiter_id, self.waiter.pk)
        self.assertEqual(old_item.status, OrderItemStatus.READY)
        self.assertEqual(OrderItem.objects.get(pk=new_item_id).status, OrderItemStatus.FIRED)

        self.client.force_authenticate(user=self.waiter)
        queue = self.client.get("/api/orders/").json()["results"]
        self.assertIn(order.pk, [row["id"] for row in queue])
        order.refresh_from_db()
        old_item.refresh_from_db()
        new_item = OrderItem.objects.get(pk=new_item_id)
        self.assertEqual(order.status, OrderStatus.PREPARING)
        self.assertEqual(old_item.status, OrderItemStatus.READY)
        self.assertEqual(new_item.status, OrderItemStatus.FIRED)
        cashier_event = order.history.get(actor=self.cashier, event="cashier_item_added_and_sent")
        self.assertEqual(cashier_event.details["actor_role"], StaffRole.CASHIER)
        self.assertTrue(next(row for row in self.client.get(f"/api/orders/{order.pk}/").json()["items"] if row["id"] == new_item_id)["is_new"])
        notice = async_to_sync(layer.receive)(waiter_channel)
        self.assertEqual(notice["name"], "order:new_items")
        self.assertEqual(notice["payload"]["items"][0]["name"], self.product.name_en)
        kitchen_notice = async_to_sync(layer.receive)(kitchen_channel)
        self.assertEqual(kitchen_notice["name"], "order:kitchen_new")
        self.assertEqual(kitchen_notice["payload"]["items"][0]["quantity"], 1)

        self.client.force_authenticate(user=self.kitchen)
        ticket = next(row for row in self.client.get("/api/kds/tickets/").json()["results"] if row["id"] == order.pk)
        self.assertEqual([row["id"] for row in ticket["items"]], [new_item_id])
        self.client.patch(f"/api/orders/{order.pk}/items/{new_item_id}/status/", {"status": "preparing"}, format="json")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(f"/api/orders/{order.pk}/items/{new_item_id}/status/", {"status": "ready"}, format="json")
        ready_notice = async_to_sync(layer.receive)(waiter_channel)
        self.assertEqual(ready_notice["name"], "order:ready")
        self.assertEqual(ready_notice["payload"]["items"][0]["id"], new_item_id)
        self.client.force_authenticate(user=self.kitchen)
        ready_ticket = next(row for row in self.client.get("/api/kds/tickets/").json()["results"] if row["id"] == order.pk)
        self.assertEqual([line["id"] for line in ready_ticket["items"]], [new_item_id])
        self.client.force_authenticate(user=self.waiter)
        served = self.client.patch(f"/api/orders/{order.pk}/items/{new_item_id}/status/", {"status": "served"}, format="json")
        self.assertEqual(served.status_code, status.HTTP_200_OK)
        refreshed = self.client.get(f"/api/orders/{order.pk}/").json()
        self.assertFalse(any(line["is_new"] for line in refreshed["items"]))

    def test_cashier_adds_to_served_order_without_reopening_served_lines(self):
        from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus

        order = Order.objects.create(
            branch=self.riyadh, table=self.open_table, assigned_waiter=self.waiter, status=OrderStatus.SERVED,
        )
        old_item = OrderItem.objects.create(
            order=order, product=self.product, name_en_snapshot="Served burger", name_ar_snapshot="Served burger",
            price_snapshot="25.00", quantity=1, status=OrderItemStatus.SERVED,
        )
        self.client.force_authenticate(user=self.cashier)
        added = self.client.post(f"/api/orders/{order.pk}/items/", {"product": self.product.pk}, format="json")
        self.assertEqual(added.status_code, status.HTTP_201_CREATED)
        new_item_id = added.json()["id"]
        order.refresh_from_db()
        old_item.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PREPARING)
        self.assertEqual(old_item.status, OrderItemStatus.SERVED)
        self.assertEqual(OrderItem.objects.get(pk=new_item_id).status, OrderItemStatus.FIRED)
        self.client.force_authenticate(user=self.waiter)
        waiter_ids = [row["id"] for row in self.client.get("/api/orders/").json()["results"]]
        self.assertIn(order.pk, waiter_ids)
        old_item.refresh_from_db()
        self.assertEqual(old_item.status, OrderItemStatus.SERVED)
        self.assertEqual(OrderItem.objects.get(pk=new_item_id).status, OrderItemStatus.FIRED)

    def test_waiter_adds_to_ready_and_served_order_dispatch_immediately(self):
        from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus, OrderHistory

        layer = get_channel_layer()
        cashier_channel = async_to_sync(layer.new_channel)()
        async_to_sync(layer.group_add)(f"staff_{self.cashier.pk}_events", cashier_channel)
        for parent_status, line_status in ((OrderStatus.READY, OrderItemStatus.READY), (OrderStatus.SERVED, OrderItemStatus.SERVED)):
            order = Order.objects.create(branch=self.riyadh, table=self.open_table,
                assigned_waiter=self.waiter, status=parent_status)
            old_item = OrderItem.objects.create(order=order, product=self.product,
                name_en_snapshot="Existing", name_ar_snapshot="Existing", price_snapshot="25.00",
                status=line_status)
            self.client.force_authenticate(user=self.waiter)
            with self.captureOnCommitCallbacks(execute=True):
                added = self.client.post(f"/api/orders/{order.pk}/items/", {"product": self.product.pk}, format="json")
            self.assertEqual(added.status_code, status.HTTP_201_CREATED)
            old_item.refresh_from_db()
            self.assertEqual(old_item.status, line_status)
            new_item = OrderItem.objects.get(pk=added.json()["id"])
            self.assertEqual(new_item.status, OrderItemStatus.FIRED)
            cashier_notice = async_to_sync(layer.receive)(cashier_channel)
            self.assertEqual(cashier_notice["name"], "order:new_items")
            self.assertEqual(cashier_notice["payload"]["items"][0]["quantity"], 1)
        added_event = order.history.get(actor=self.waiter, event="waiter_item_added_and_sent")
        self.assertEqual(added_event.details["actor_role"], StaffRole.WAITER)
        self.assertEqual(added_event.details["item_ids"], [new_item.pk])

    def test_waiter_cannot_add_to_another_waiters_order_or_another_branch(self):
        from orders.models import Order, OrderStatus

        other_waiter = make_staff(self.riyadh, "Layla", StaffRole.WAITER, "8888")
        other_order = Order.objects.create(branch=self.riyadh, table=self.empty_table,
            assigned_waiter=other_waiter, status=OrderStatus.READY)
        foreign_order = Order.objects.create(branch=self.jeddah, table=self.other_branch_table,
            assigned_waiter=self.waiter, status=OrderStatus.READY)
        self.client.force_authenticate(user=self.waiter)
        self.assertEqual(self.client.post(f"/api/orders/{other_order.pk}/items/", {"product": self.product.pk}, format="json").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.post(f"/api/orders/{foreign_order.pk}/items/", {"product": self.product.pk}, format="json").status_code, status.HTTP_404_NOT_FOUND)

    def test_customer_addition_waits_for_cashier_review_then_dispatches_to_same_order(self):
        from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus

        from customers.services import find_or_create_customer
        from orders.models import Bill
        # The customer's own order (each phone has its own bill at a table).
        customer, _ = find_or_create_customer(self.riyadh, "0770000099")
        bill = Bill.objects.create(branch=self.riyadh, table=self.open_table, customer=customer)
        order = Order.objects.create(branch=self.riyadh, table=self.open_table, customer=customer, bill=bill,
            assigned_waiter=self.waiter, status=OrderStatus.READY)
        old_item = OrderItem.objects.create(order=order, product=self.product,
            name_en_snapshot="Already ready", name_ar_snapshot="Already ready",
            price_snapshot="25.00", status=OrderItemStatus.READY)
        layer = get_channel_layer()
        cashier_channel = async_to_sync(layer.new_channel)()
        waiter_channel = async_to_sync(layer.new_channel)()
        async_to_sync(layer.group_add)(f"staff_{self.cashier.pk}_events", cashier_channel)
        async_to_sync(layer.group_add)(f"staff_{self.waiter.pk}_events", waiter_channel)

        with self.captureOnCommitCallbacks(execute=True):
            added = self.post_qr({
                "session_token": self.open_table.session_token,
                "phone": "0770000099",
                "items": [{"product": self.product.pk, "quantity": 2}],
            })
        self.assertEqual(added.status_code, status.HTTP_200_OK)
        self.assertEqual(added.json()["id"], order.pk)
        order.refresh_from_db()
        old_item.refresh_from_db()
        new_item = OrderItem.objects.filter(order=order).exclude(pk=old_item.pk).get()
        self.assertEqual(order.status, OrderStatus.READY)
        self.assertEqual(order.assigned_waiter_id, self.waiter.pk)
        self.assertEqual(old_item.status, OrderItemStatus.READY)
        self.assertEqual(new_item.status, OrderItemStatus.PENDING)
        self.assertTrue(next(line for line in added.json()["items"] if line["id"] == new_item.pk)["is_new"])
        notice = async_to_sync(layer.receive)(cashier_channel)
        self.assertEqual(notice["name"], "order:cashier_review")
        self.assertEqual(notice["payload"]["items"], [{"id": new_item.pk, "name": self.product.name_en, "quantity": 2}])

        self.client.force_authenticate(user=self.kitchen)
        before_review = self.client.get("/api/kds/tickets/").json()["results"]
        self.assertEqual([line["id"] for row in before_review if row["id"] == order.pk for line in row["items"]], [old_item.pk])

        self.client.force_authenticate(user=self.cashier)
        with self.captureOnCommitCallbacks(execute=True):
            reviewed = self.client.post(f"/api/orders/{order.pk}/cashier-review/")
        self.assertEqual(reviewed.status_code, status.HTTP_200_OK)
        self.assertEqual(reviewed.json()["status"], OrderStatus.PREPARING)
        self.assertEqual(next(line for line in reviewed.json()["items"] if line["id"] == new_item.pk)["status"], OrderItemStatus.FIRED)
        waiter_notice = async_to_sync(layer.receive)(waiter_channel)
        self.assertEqual(waiter_notice["name"], "order:new_items")
        self.assertEqual(waiter_notice["payload"]["items"][0]["quantity"], 2)

        self.client.force_authenticate(user=self.kitchen)
        ticket = next(row for row in self.client.get("/api/kds/tickets/").json()["results"] if row["id"] == order.pk)
        self.assertEqual([line["id"] for line in ticket["items"]], [new_item.pk])

    def test_public_tracking_resolves_exact_order_only_with_customer_phone(self):
        from customers.models import Customer
        from orders.models import Order, OrderStatus

        customer = Customer.objects.create(branch=self.riyadh, phone="0770000001")
        order = Order.objects.create(
            branch=self.riyadh, table=self.open_table, customer=customer, status=OrderStatus.CANCELLED,
        )
        url = f"/api/orders/qr/{self.open_table.session_token}/{order.pk}/"
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(url, {"phone": "0770000099"}).status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.get(url, {"phone": customer.phone})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], OrderStatus.CANCELLED)
        self.assertIn("bill_id", response.json())
        other_order = Order.objects.create(branch=self.riyadh, table=self.empty_table, status=OrderStatus.CANCELLED)
        self.assertEqual(self.client.get(
            f"/api/orders/qr/{self.open_table.session_token}/{other_order.pk}/", {"phone": customer.phone},
        ).status_code, status.HTTP_404_NOT_FOUND)

    def test_assign_waiter_recovery_is_only_for_unassigned_ready_or_served_orders(self):
        from orders.models import Order, OrderHistory, OrderStatus

        other_waiter = make_staff(self.riyadh, "Layla", StaffRole.WAITER, "8888")
        ready = Order.objects.create(branch=self.riyadh, table=self.open_table, status=OrderStatus.READY)
        self.client.force_authenticate(user=self.cashier)
        assigned = self.client.post(
            f"/api/orders/{ready.pk}/assign-waiter/", {"waiter": self.waiter.pk}, format="json",
        )
        self.assertEqual(assigned.status_code, status.HTTP_200_OK)
        ready.refresh_from_db()
        self.assertEqual(ready.status, OrderStatus.READY)
        self.assertEqual(ready.assigned_waiter_id, self.waiter.pk)
        self.assertTrue(OrderHistory.objects.filter(order=ready, event="waiter_assigned").exists())

        rejected = self.client.post(
            f"/api/orders/{ready.pk}/assign-waiter/", {"waiter": other_waiter.pk}, format="json",
        )
        self.assertEqual(rejected.status_code, status.HTTP_409_CONFLICT)
        ready.refresh_from_db()
        self.assertEqual(ready.assigned_waiter_id, self.waiter.pk)

    def test_customer_order_requires_waiter_confirmation_and_payment_does_not_release_table(self):
        table_layer = get_channel_layer()
        table_channel = async_to_sync(table_layer.new_channel)()
        async_to_sync(table_layer.group_add)(f"table_{self.open_table.id}_events", table_channel)

        with self.captureOnCommitCallbacks(execute=True):
            placed = self.client.post(
                "/api/orders/qr/",
                {
                    "session_token": self.open_table.session_token,
                    "phone": "0770000033",
                    "latitude": 24.7136,
                    "longitude": 46.6753,
                    "accuracy_m": 0.2,
                    "location_timestamp": time.time(),
                    "items": [{"product": self.product.id, "quantity": 1}],
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY="workflow-customer-order",
            )
        self.assertEqual(placed.status_code, status.HTTP_201_CREATED)
        order_id = placed.json()["id"]
        order_code = placed.json()["order_code"]
        self.assertEqual(placed.json()["status"], "pending_cashier")
        self.assertTrue(order_code.startswith("ORD-"))
        table_event = async_to_sync(table_layer.receive)(table_channel)
        self.assertEqual(table_event["name"], "order:updated")

        self.auth_as(self.kitchen, "6666")
        tickets = self.client.get("/api/kds/tickets/")
        self.assertEqual(tickets.status_code, status.HTTP_200_OK)
        self.assertFalse(any(row["id"] == order_id for row in tickets.json()["results"]))
        self.assertEqual(
            self.client.post(f"/api/orders/{order_id}/send-to-kitchen/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        self.auth_as(self.cashier, "3333")
        reviewed = self.client.post(f"/api/orders/{order_id}/cashier-review/")
        self.assertEqual(reviewed.status_code, status.HTTP_200_OK)
        self.assertEqual(reviewed.json()["status"], "awaiting_waiter")
        assigned = self.client.post(
            f"/api/orders/{order_id}/assign-waiter/", {"waiter": self.waiter.id}, format="json"
        )
        self.assertEqual(assigned.status_code, status.HTTP_200_OK)
        self.assertEqual(assigned.json()["assigned_cashier"], self.cashier.id)
        self.assertEqual(assigned.json()["assigned_waiter"], self.waiter.id)
        filtered = self.client.get(
            f"/api/orders/?status=awaiting_waiter&table={self.open_table.id}&waiter={self.waiter.id}"
            f"&cashier={self.cashier.id}&search={order_code}&from=2020-01-01&to=2099-12-31"
        )
        self.assertEqual(filtered.status_code, status.HTTP_200_OK)
        self.assertEqual([row["id"] for row in filtered.json()["results"]], [order_id])

        self.auth_as(self.waiter, "4444")
        waiter_queue = self.client.get("/api/orders/")
        self.assertEqual(waiter_queue.status_code, status.HTTP_200_OK)
        self.assertEqual([row["id"] for row in waiter_queue.json()["results"]], [order_id])
        confirmed = self.client.post(f"/api/orders/{order_id}/waiter-confirm/")
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK)
        self.assertEqual(confirmed.json()["status"], "sent")
        self.assertEqual(confirmed.json()["items"][0]["status"], "fired")

        self.auth_as(self.kitchen, "6666")
        tickets = self.client.get("/api/kds/tickets/")
        self.assertEqual(tickets.status_code, status.HTTP_200_OK)
        self.assertTrue(any(row["id"] == order_id for row in tickets.json()["results"]))

        channel_layer = get_channel_layer()
        waiter_channel = async_to_sync(channel_layer.new_channel)()
        cashier_channel = async_to_sync(channel_layer.new_channel)()
        async_to_sync(channel_layer.group_add)(f"staff_{self.waiter.id}_events", waiter_channel)
        async_to_sync(channel_layer.group_add)(f"staff_{self.cashier.id}_events", cashier_channel)

        item_id = confirmed.json()["items"][0]["id"]
        for item_status, order_status in (("preparing", "preparing"), ("ready", "ready")):
            kitchen_update_channel = async_to_sync(table_layer.new_channel)()
            async_to_sync(table_layer.group_add)(f"table_{self.open_table.id}_events", kitchen_update_channel)
            with self.captureOnCommitCallbacks(execute=True):
                updated = self.client.patch(
                    f"/api/orders/{order_id}/items/{item_id}/status/", {"status": item_status}, format="json"
                )
            self.assertEqual(updated.status_code, status.HTTP_200_OK)
            self.assertEqual(updated.json()["status"], item_status)
            customer_update = async_to_sync(table_layer.receive)(kitchen_update_channel)
            self.assertEqual(customer_update["name"], "order:updated")
            self.assertEqual(
                __import__("orders.models", fromlist=["Order"]).Order.objects.get(pk=order_id).status,
                order_status,
            )
            if item_status == "preparing":
                waiter_event = async_to_sync(channel_layer.receive)(waiter_channel)
                cashier_event = async_to_sync(channel_layer.receive)(cashier_channel)
                self.assertEqual(waiter_event["name"], "order:preparing")
                self.assertEqual(waiter_event["payload"]["order_id"], order_id)
                self.assertEqual(cashier_event["name"], "order:preparing")
                self.assertEqual(cashier_event["payload"]["order_id"], order_id)
            else:
                waiter_event = async_to_sync(channel_layer.receive)(waiter_channel)
                self.assertEqual(waiter_event["name"], "order:ready")
                self.assertEqual(waiter_event["payload"]["order_id"], order_id)

        self.assertEqual(
            self.client.patch(
                f"/api/orders/{order_id}/items/{item_id}/status/", {"status": "served"}, format="json"
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.auth_as(self.waiter, "4444")
        served = self.client.patch(
            f"/api/orders/{order_id}/items/{item_id}/status/", {"status": "served"}, format="json"
        )
        self.assertEqual(served.status_code, status.HTTP_200_OK)
        self.assertEqual(served.json()["status"], "served")

        self.auth_as(self.cashier, "3333")
        payment = self.client.post(
            f"/api/orders/{order_id}/payments/",
            {"amount": "25.00", "method": "cash", "tip": "2.50", "request_id": "workflow-payment"}, format="json",
        )
        self.assertEqual(payment.status_code, status.HTTP_201_CREATED)
        self.assertEqual(payment.json()["tip"], "2.50")
        payment_retry = self.client.post(
            f"/api/orders/{order_id}/payments/",
            {"amount": "25.00", "method": "cash", "tip": "2.50", "request_id": "workflow-payment"}, format="json",
        )
        self.assertEqual(payment_retry.status_code, status.HTTP_200_OK)
        mismatched_retry = self.client.post(
            f"/api/orders/{order_id}/payments/",
            {"amount": "24.00", "method": "cash", "request_id": "workflow-payment"}, format="json",
        )
        self.assertEqual(mismatched_retry.status_code, status.HTTP_409_CONFLICT)
        from orders.models import Payment
        self.assertEqual(Payment.objects.filter(order_id=order_id).count(), 1)
        self.assertEqual(str(Payment.objects.get(order_id=order_id).tip), "2.50")
        self.open_table.refresh_from_db()
        # Paid in full at the till: the bill is settled, but the table is not
        # released (needs-bill, never empty) until a cashier releases it.
        self.assertEqual(self.open_table.status, "needs-bill")
        from orders.models import Order
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.bill.status, "paid")
        self.assertEqual(order.status, "paid")
        self.assertEqual(order.payment_status, "paid")
        from orders.models import OrderHistory
        payment_event = OrderHistory.objects.get(order_id=order_id, event="payment_completed")
        self.assertEqual(payment_event.details["tip"], "2.50")

        released = self.client.post(f"/api/tables/{self.open_table.id}/close/")
        self.assertEqual(released.status_code, status.HTTP_200_OK)
        self.open_table.refresh_from_db()
        self.assertEqual(self.open_table.status, "empty")
        history = self.client.get(f"/api/orders/{order_id}/history/")
        self.assertEqual(history.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(history.json()["results"]), 7)

    def test_multi_item_ready_event_serving_gate_and_kds_item_filter(self):
        from orders.models import Order
        placed = self.post_qr({
            "session_token": self.open_table.session_token,
            "phone": "0770000044",
            "items": [
                {"product": self.product.id, "quantity": 1},
                {"product": self.product.id, "quantity": 2},
            ],
        })
        order_id = placed.json()["id"]
        self.auth_as(self.cashier, "3333")
        self.assertEqual(self.client.post(f"/api/orders/{order_id}/cashier-review/").status_code, status.HTTP_200_OK)
        self.client.post(f"/api/orders/{order_id}/assign-waiter/", {"waiter": self.waiter.id}, format="json")
        self.auth_as(self.waiter, "4444")
        confirmed = self.client.post(f"/api/orders/{order_id}/waiter-confirm/")
        item_ids = [item["id"] for item in confirmed.json()["items"]]

        channel_layer = get_channel_layer()
        waiter_channel = async_to_sync(channel_layer.new_channel)()
        async_to_sync(channel_layer.group_add)(f"staff_{self.waiter.id}_events", waiter_channel)
        cashier_channel = async_to_sync(channel_layer.new_channel)()
        async_to_sync(channel_layer.group_add)(f"staff_{self.cashier.id}_events", cashier_channel)

        self.auth_as(self.kitchen, "6666")
        with self.captureOnCommitCallbacks(execute=True):
            for item_id in item_ids:
                self.client.patch(f"/api/orders/{order_id}/items/{item_id}/status/", {"status": "preparing"}, format="json")
        # Status notifications are order-level and fire only when the overall
        # order transitions to preparing (the second item doesn't transition it).
        self.assertEqual(async_to_sync(channel_layer.receive)(waiter_channel)["name"], "order:preparing")
        self.assertEqual(async_to_sync(channel_layer.receive)(cashier_channel)["name"], "order:preparing")

        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(f"/api/orders/{order_id}/items/{item_ids[0]}/status/", {"status": "ready"}, format="json")
        self.assertEqual(Order.objects.get(pk=order_id).status, "preparing")
        self.auth_as(self.waiter, "4444")
        # Serving is per line now: a line that is still cooking cannot be served.
        early_serve = self.client.patch(
            f"/api/orders/{order_id}/items/{item_ids[1]}/status/", {"status": "served"}, format="json"
        )
        self.assertEqual(early_serve.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(early_serve.json()["error"], "error.itemNotReady")

        self.auth_as(self.kitchen, "6666")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(f"/api/orders/{order_id}/items/{item_ids[1]}/status/", {"status": "ready"}, format="json")
        self.assertEqual(Order.objects.get(pk=order_id).status, "ready")
        ready_event = async_to_sync(channel_layer.receive)(waiter_channel)
        self.assertEqual(ready_event["name"], "order:ready")
        self.assertEqual(ready_event["payload"]["status"], "ready")

        # A new QR addition goes through cashier review and must not appear as a kitchen line.
        self.post_qr({
            "session_token": self.open_table.session_token,
            "phone": "0770000044",
            "items": [{"product": self.product.id, "quantity": 1}],
        })
        tickets = self.client.get("/api/kds/tickets/").json()["results"]
        ticket = next(row for row in tickets if row["id"] == order_id)
        self.assertEqual(len(ticket["items"]), 2)
        self.assertTrue(all(item["status"] == "ready" for item in ticket["items"]))

    def test_table_release_rejects_active_order_and_preserves_occupied_state(self):
        self.post_qr({
            "session_token": self.open_table.session_token,
            "phone": "0770000055",
            "items": [{"product": self.product.id, "quantity": 1}],
        })
        self.auth_as(self.cashier, "3333")
        response = self.client.post(f"/api/tables/{self.open_table.id}/close/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.open_table.refresh_from_db()
        self.assertEqual(self.open_table.status, "occupied")

    def test_stale_cashier_review_and_waiter_reassignment_are_rejected_after_transition(self):
        placed = self.post_qr({
            "session_token": self.open_table.session_token,
            "phone": "0770000066",
            "items": [{"product": self.product.id, "quantity": 1}],
        })
        order_id = placed.json()["id"]
        self.auth_as(self.cashier, "3333")
        self.assertEqual(self.client.post(f"/api/orders/{order_id}/cashier-review/").status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.post(f"/api/orders/{order_id}/cashier-review/").status_code, status.HTTP_409_CONFLICT)
        self.client.post(f"/api/orders/{order_id}/assign-waiter/", {"waiter": self.waiter.id}, format="json")
        self.auth_as(self.waiter, "4444")
        self.assertEqual(self.client.post(f"/api/orders/{order_id}/waiter-confirm/").status_code, status.HTTP_200_OK)
        self.auth_as(self.cashier, "3333")
        reassignment = self.client.post(
            f"/api/orders/{order_id}/assign-waiter/", {"waiter": self.waiter.id}, format="json"
        )
        self.assertEqual(reassignment.status_code, status.HTTP_409_CONFLICT)

    def test_waiter_edit_keeps_assignment_and_records_actor_and_changes(self):
        from orders.models import Order, OrderHistory
        placed = self.post_qr({
            "session_token": self.open_table.session_token,
            "phone": "0770000077",
            "items": [{"product": self.product.id, "quantity": 1, "notes": "no onion"}],
        })
        order_id = placed.json()["id"]
        item_id = placed.json()["items"][0]["id"]
        self.auth_as(self.cashier, "3333")
        self.client.post(f"/api/orders/{order_id}/cashier-review/")
        self.client.post(f"/api/orders/{order_id}/assign-waiter/", {"waiter": self.waiter.id}, format="json")

        self.auth_as(self.waiter, "4444")
        edited = self.client.patch(
            f"/api/orders/{order_id}/items/{item_id}/",
            {"quantity": 3, "notes": "extra sauce"}, format="json",
        )
        self.assertEqual(edited.status_code, status.HTTP_200_OK)
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.status, "awaiting_waiter")
        self.assertEqual(order.assigned_waiter_id, self.waiter.id)
        history = OrderHistory.objects.get(order=order, event="waiter_item_updated")
        self.assertEqual(history.actor_id, self.waiter.id)
        self.assertEqual(history.details["before"], {"quantity": 1, "notes": "no onion"})
        self.assertEqual(history.details["after"], {"quantity": 3, "notes": "extra sauce"})

    @patch("orders.views.publish_staff_event")
    def test_workflow_notifications_target_the_relevant_roles(self, publish):
        placed = self.post_qr({
            "session_token": self.open_table.session_token,
            "phone": "0770000088",
            "items": [{"product": self.product.id, "quantity": 1}],
        })
        order_id = placed.json()["id"]
        event_names = [call.args[1] for call in publish.call_args_list]
        self.assertIn("order:cashier_review", event_names)
        self.assertIn("order:admin_update", event_names)
        self.assertNotIn("order:kitchen_new", event_names)

        publish.reset_mock()
        self.auth_as(self.cashier, "3333")
        self.client.post(f"/api/orders/{order_id}/cashier-review/")
        self.client.post(f"/api/orders/{order_id}/assign-waiter/", {"waiter": self.waiter.id}, format="json")
        assigned = [call for call in publish.call_args_list if call.args[1] == "order:assigned"]
        self.assertEqual([call.args[0] for call in assigned], [self.waiter.id])
        self.assertEqual(assigned[0].args[2]["actor_id"], self.cashier.id)

        publish.reset_mock()
        self.auth_as(self.waiter, "4444")
        self.client.post(f"/api/orders/{order_id}/waiter-confirm/")
        kitchen_events = [call for call in publish.call_args_list if call.args[1] == "order:kitchen_new"]
        self.assertEqual(len(kitchen_events), 1)
        from accounts.models import StaffRole
        kitchen_ids = set(Staff.objects.filter(branch=self.riyadh, role=StaffRole.KITCHEN).values_list("id", flat=True))
        self.assertIn(kitchen_events[0].args[0], kitchen_ids)



class EmptyPosOrderTableTests(TablesApiTestCase):
    """A POS ticket whose every line is taken off again must not leave an
    empty order number behind, keep the table occupied, or block Release/delete."""

    def setUp(self):
        super().setUp()
        self.table = Table.objects.create(branch=self.riyadh, label_en="Table 20", label_ar="طاولة ٢٠")
        self.auth_as(self.owner, "1111")

    def add_then_remove(self):
        order_id = self.client.post("/api/orders/", {"table": self.table.id}, format="json").json()["id"]
        item = self.client.post(f"/api/orders/{order_id}/items/", {"product": self.product.id, "quantity": 1}, format="json")
        self.assertEqual(item.status_code, status.HTTP_201_CREATED, item.content)
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, "occupied")
        removed = self.client.patch(f"/api/orders/{order_id}/items/{item.json()['id']}/", {"quantity": 0}, format="json")
        self.assertEqual(removed.status_code, status.HTTP_204_NO_CONTENT)
        return order_id

    def test_removing_the_last_line_deletes_the_draft_order_and_frees_the_table(self):
        from orders.models import Bill, Order

        order_id = self.add_then_remove()
        self.assertFalse(Order.objects.filter(pk=order_id).exists())
        self.assertFalse(Bill.objects.filter(table=self.table).exists())
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, "empty")
        self.assertEqual(self.client.get(f"/api/tables/{self.table.id}/open-order/").status_code, status.HTTP_404_NOT_FOUND)
        # The next tap starts a fresh order and occupies the table again.
        fresh = self.client.post("/api/orders/", {"table": self.table.id}, format="json")
        self.assertEqual(fresh.status_code, status.HTTP_201_CREATED)
        self.client.post(f"/api/orders/{fresh.json()['id']}/items/", {"product": self.product.id, "quantity": 1}, format="json")
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, "occupied")

    def test_removing_one_of_two_lines_keeps_the_order(self):
        from orders.models import Order

        order_id = self.client.post("/api/orders/", {"table": self.table.id}, format="json").json()["id"]
        first = self.client.post(f"/api/orders/{order_id}/items/", {"product": self.product.id, "quantity": 1}, format="json").json()
        self.client.post(f"/api/orders/{order_id}/items/", {"product": self.product.id, "quantity": 1, "notes": "no onion"}, format="json")
        self.client.patch(f"/api/orders/{order_id}/items/{first['id']}/", {"quantity": 0}, format="json")
        self.assertTrue(Order.objects.filter(pk=order_id).exists())
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, "occupied")

    def test_cashier_emptying_a_customer_qr_order_keeps_its_record(self):
        from orders.models import Order

        self.table.open()
        body = self.post_qr({
            "session_token": self.table.session_token,
            "phone": "0770000077",
            "items": [{"product": self.product.id, "quantity": 1}],
        }).json()
        self.auth_as(self.cashier, "3333")
        removed = self.client.patch(f"/api/orders/{body['id']}/items/{body['items'][0]['id']}/", {"quantity": 0}, format="json")
        self.assertEqual(removed.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(Order.objects.filter(pk=body["id"]).exists())
        # Release then cancels it (nothing owed) instead of refusing.
        released = self.client.post(f"/api/tables/{self.table.id}/close/")
        self.assertEqual(released.status_code, status.HTTP_200_OK, released.content)
        order = Order.objects.get(pk=body["id"])
        self.assertEqual(order.status, "cancelled")
        # Nothing was ordered, so it is not a cancellation for the report.
        self.assertIsNone(order.cancelled_at)

    def test_release_drops_an_order_that_never_got_a_line(self):
        from orders.models import Order

        order_id = self.client.post("/api/orders/", {"table": self.table.id}, format="json").json()["id"]
        self.table.open()
        response = self.client.post(f"/api/tables/{self.table.id}/close/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()["status"], "empty")
        self.assertFalse(Order.objects.filter(pk=order_id).exists())

    def test_release_still_refuses_an_order_with_items(self):
        order_id = self.client.post("/api/orders/", {"table": self.table.id}, format="json").json()["id"]
        self.client.post(f"/api/orders/{order_id}/items/", {"product": self.product.id, "quantity": 1}, format="json")
        response = self.client.post(f"/api/tables/{self.table.id}/close/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, "occupied")

    def test_a_table_with_only_an_empty_order_can_be_deleted(self):
        self.client.post("/api/orders/", {"table": self.table.id}, format="json")
        response = self.client.delete(f"/api/tables/{self.table.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT, response.content)
        self.assertFalse(Table.objects.filter(pk=self.table.id).exists())


class KitchenTicketDetailTests(TablesApiTestCase):
    """What a KDS ticket carries: where it came from, the portion ordered,
    the recipe scaled for that portion, and a date-filtered kitchen log."""

    def setUp(self):
        super().setUp()
        self.waiter = make_staff(self.riyadh, "Sara", StaffRole.WAITER, "4444")
        self.kitchen = make_staff(self.riyadh, "Ali", StaffRole.KITCHEN, "6666")

    def _fired_order(self, item_status="fired", **order_fields):
        from django.utils import timezone
        from catalog.models import ProductVariant
        from orders.models import Order, OrderItem

        variant = ProductVariant.objects.create(
            product=self.product, name_en="Double", name_ar="دبل", price="40.00", recipe_multiplier="2.000",
        )
        order = Order.objects.create(
            branch=self.riyadh, table=self.open_table, assigned_waiter=self.waiter, **order_fields,
        )
        item = OrderItem.objects.create(
            order=order, product=self.product, variant=variant,
            name_en_snapshot="Smash Burger", name_ar_snapshot="سماش برغر",
            variant_name_en_snapshot="Double", variant_name_ar_snapshot="دبل",
            price_snapshot="40.00", quantity=3, status=item_status, notes="No onion",
            fired_at=timezone.now(),
        )
        return order, item

    def test_live_ticket_carries_table_waiter_variant_and_scaled_recipe(self):
        order, item = self._fired_order()
        self.auth_as(self.kitchen, "6666")
        ticket = next(row for row in self.client.get("/api/kds/tickets/").json()["results"] if row["id"] == order.pk)

        self.assertEqual(ticket["table_label_en"], "Table 3")
        self.assertEqual(ticket["assigned_waiter_name"], "Sara")
        self.assertEqual(ticket["source"], "pos")
        line = ticket["items"][0]
        self.assertEqual(line["variant_name_en_snapshot"], "Double")
        self.assertEqual(line["notes"], "No onion")
        self.assertEqual(line["recipe_multiplier"], "2.000")
        # 1 patty per recipe x 2 (Double) x 3 ordered = 6, with the inputs shown.
        self.assertEqual(line["recipe"], [{
            "inventory_item": line["recipe"][0]["inventory_item"], "name": "Burger patty", "unit": "unit",
            "per_portion": "1.000", "total": "6.000",
        }])

    def test_history_scope_lists_finished_orders_filtered_by_date(self):
        from datetime import datetime, timezone as datetime_timezone
        from orders.models import Order

        order, _ = self._fired_order(item_status="served")
        Order.objects.filter(pk=order.pk).update(opened_at=datetime(2026, 10, 2, 22, 30, tzinfo=datetime_timezone.utc))
        self.auth_as(self.kitchen, "6666")

        live = self.client.get("/api/kds/tickets/").json()["results"]
        self.assertNotIn(order.pk, [row["id"] for row in live])
        # 22:30 UTC on the 2nd is the 3rd in Riyadh.
        in_range = self.client.get("/api/kds/tickets/?scope=history&from=2026-10-03&to=2026-10-03").json()["results"]
        out_of_range = self.client.get("/api/kds/tickets/?scope=history&from=2026-10-02&to=2026-10-02").json()["results"]
        ticket = next(row for row in in_range if row["id"] == order.pk)
        self.assertEqual(ticket["items"][0]["status"], "served")
        self.assertNotIn(order.pk, [row["id"] for row in out_of_range])

    def test_filters_narrow_by_table_type_and_order_number(self):
        order, _ = self._fired_order()
        self.auth_as(self.kitchen, "6666")

        def ids(query):
            return [row["id"] for row in self.client.get(f"/api/kds/tickets/?{query}").json()["results"]]

        self.assertIn(order.pk, ids(f"table={self.open_table.pk}"))
        self.assertNotIn(order.pk, ids(f"table={self.empty_table.pk}"))
        self.assertIn(order.pk, ids("type=dine_in"))
        self.assertNotIn(order.pk, ids("type=takeaway"))
        self.assertNotIn(order.pk, ids("type=qr"))
        self.assertIn(order.pk, ids(f"search={order.pk}"))
        self.assertEqual(ids("type=bogus"), [])


class TableLabelValidationTests(TablesApiTestCase):
    """Table labels are normalised on save and duplicates are caught ignoring case and spaces."""

    def create(self, label_en="", label_ar=""):
        return self.client.post(
            "/api/tables/",
            {"branch": self.riyadh.id, "label_en": label_en, "label_ar": label_ar, "seats": 4},
            format="json",
        )

    def setUp(self):
        super().setUp()
        self.auth_as(self.owner, "1111")

    def test_english_label_is_stored_upper_case_with_collapsed_spaces(self):
        response = self.create("  garden   7 ")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["label_en"], "GARDEN 7")

    def test_duplicate_label_rejected_regardless_of_case_or_spacing(self):
        for label in ("Table 4", "table 4", "TABLE4", "  table   4 "):
            response = self.create(label)
            self.assertEqual(response.status_code, 400, label)
            self.assertEqual(response.json()["label_en"], ["error.tableLabelExists"])

    def test_duplicate_arabic_label_rejected_ignoring_spaces(self):
        response = self.create(label_ar="طاولة  ٤")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["label_ar"], ["error.tableLabelExists"])

    def test_same_label_allowed_in_another_branch(self):
        response = self.create("Table 9")
        self.assertEqual(response.status_code, 201, response.content)

    def test_editing_a_table_keeps_its_own_label(self):
        response = self.client.patch(f"/api/tables/{self.empty_table.id}/", {"label_en": "table 4", "seats": 6}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["label_en"], "TABLE 4")

    def test_renaming_into_an_existing_label_rejected(self):
        response = self.client.patch(f"/api/tables/{self.empty_table.id}/", {"label_en": "table3"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["label_en"], ["error.tableLabelExists"])
