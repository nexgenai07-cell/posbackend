"""Per-phone bills, same-order adds after payment, and payment accounting.

Regression tests for the production BILL-00000015 case: three phones shared
one table bill, a cancelled order's old payment discounted the next customer,
till payments were written a second time by the bill settlement, and the
settlement recorded order totals instead of the money collected.
"""

from decimal import Decimal
from unittest.mock import patch

from orders import moyasar

from .models import Bill, Order, OrderStatus, Payment
from .test_customer_session import CustomerSessionTestCase

PHONE_A = "0501111111"
PHONE_B = "0502222222"


class PerPhoneBillTestCase(CustomerSessionTestCase):
    def bill_for(self, table, phone, bill_id=None):
        params = {"phone": phone}
        if bill_id:
            params["bill_id"] = bill_id
        return self.public.get(f"/api/bills/by-session/{table.session_token}/", params)

    def review_assign_confirm(self, order_id):
        self.cashier_client.post(f"/api/orders/{order_id}/cashier-review/")
        self.cashier_client.post(f"/api/orders/{order_id}/assign-waiter/", {"waiter": self.waiter.pk}, format="json")
        self.waiter_client.post(f"/api/orders/{order_id}/waiter-confirm/")

    def dispatch_and_serve(self, order_id):
        """Whatever the order's state, get every line to served."""
        order = Order.objects.get(pk=order_id)
        if order.status in ("pending_cashier", "open"):
            self.review_assign_confirm(order_id)
        elif order.items.filter(status="pending").exists():
            self.cashier_client.post(f"/api/orders/{order_id}/cashier-review/")
        self.serve_everything(order_id)

    def till_pay(self, order_id, key):
        order = Order.objects.get(pk=order_id)
        from .billing import order_due
        amount = order_due(order)
        return self.cashier_client.post(f"/api/orders/{order_id}/payments/",
                                        {"amount": str(amount), "method": "cash", "request_id": key}, format="json")


class SeparateBillPerPhoneTests(PerPhoneBillTestCase):
    def test_second_number_is_blocked_without_changing_existing_bill(self):
        table = self.new_table()
        first = self.qr(table, phone=PHONE_A).json()
        response = self.qr(table, phone=PHONE_B)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"error": "error.tableOccupied"})
        self.assertEqual(Bill.objects.filter(table=table).count(), 1)
        self.assertEqual(self.bill_for(table, PHONE_A).json()["total"], "10.00")
        again = self.qr(table, phone="+966501111111")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["bill_id"], first["bill_id"])

    def test_scan_does_not_claim_table_and_status_does_not_expose_phone(self):
        table = self.new_table()
        url = f"/api/tables/by-session/{table.session_token}/"
        self.assertFalse(self.public.get(url).json()["occupied"])
        first = self.qr(table, phone=PHONE_A).json()
        public = self.public.get(url).json()
        self.assertTrue(public["occupied"])
        self.assertIsNone(public["can_order"])
        self.assertNotIn(PHONE_A, str(public))
        other = self.public.get(url, {"phone": PHONE_B}).json()
        self.assertFalse(other["can_order"])
        self.assertIsNone(other["bill_id"])
        own = self.public.get(url, {"phone": "+966501111111"}).json()
        self.assertTrue(own["can_order"])
        self.assertEqual(own["bill_status"], "open")
        self.assertEqual(own["bill_id"], first["bill_id"])

    def test_paid_and_payment_requested_still_occupy_until_release(self):
        table = self.new_table()
        first = self.qr(table, phone=PHONE_A).json()
        self.dispatch_and_serve(first["id"])
        response = self.public.post("/api/bills/request-pay/", {
            "session_token": table.session_token, "method": "cash", "phone": PHONE_A,
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.qr(table, phone=PHONE_B).status_code, 409)
        self.assertEqual(self.cashier_client.post(f"/api/bills/{first['bill_id']}/mark-paid/",
                         {"method": "cash"}, format="json").status_code, 200)
        self.assertEqual(self.qr(table, phone=PHONE_B).status_code, 409)
        own = self.public.get(f"/api/tables/by-session/{table.session_token}/", {"phone": PHONE_A}).json()
        self.assertEqual(own["bill_status"], "paid")
        self.assertEqual(self.cashier_client.post(f"/api/bills/{first['bill_id']}/release-table/").status_code, 200)
        self.assertFalse(self.public.get(f"/api/tables/by-session/{table.session_token}/").json()["occupied"])
        self.assertEqual(self.qr(table, phone=PHONE_B).status_code, 201)

    def test_staff_links_pos_bill_for_qr_without_opening_a_second_bill(self):
        from customers.models import Customer
        table = self.new_table()
        staff = self.cashier_client.post("/api/orders/", {"table": table.pk}, format="json")
        self.assertEqual(staff.status_code, 201)
        order_id = staff.json()["id"]
        customer = Customer.objects.create(branch=self.branch, phone=PHONE_A)
        response = self.cashier_client.post(f"/api/orders/{order_id}/customer/", {"customer": customer.pk}, format="json")
        self.assertEqual(response.status_code, 200)
        qr = self.qr(table, phone=PHONE_A)
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr.json()["id"], order_id)
        self.assertEqual(Bill.objects.filter(table=table).count(), 1)
        self.assertEqual(self.qr(table, phone=PHONE_B).status_code, 409)

    def test_legacy_multiple_bills_are_preserved_and_new_numbers_blocked(self):
        from customers.models import Customer
        table = self.new_table()
        first = self.qr(table, phone=PHONE_A).json()
        other = Customer.objects.create(branch=self.branch, phone=PHONE_B)
        legacy = Bill.objects.create(branch=self.branch, table=table, customer=other)
        response = self.qr(table, phone=PHONE_B)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["bill_id"], legacy.pk)
        self.assertEqual(self.qr(table, phone="0503333333").status_code, 409)
        self.assertEqual(self.qr(table, phone=PHONE_A).json()["bill_id"], first["bill_id"])
        self.assertEqual(Bill.objects.filter(table=table).count(), 2)

    def test_unclaimed_pos_bill_blocks_qr(self):
        table = self.new_table()
        Bill.objects.create(branch=self.branch, table=table)
        self.assertEqual(self.qr(table, phone=PHONE_A).status_code, 409)
        self.assertEqual(Bill.objects.filter(table=table).count(), 1)

    def test_phone_is_required_and_other_phones_bill_is_never_returned(self):
        table = self.new_table()
        a = self.qr(table, phone=PHONE_A).json()
        self.assertEqual(self.bill_for(table, "").status_code, 400)
        self.assertEqual(self.bill_for(table, PHONE_B).status_code, 404)
        # B pinning A's bill id gets "session ended", not A's data.
        response = self.bill_for(table, PHONE_B, a["bill_id"])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"error": "error.billSessionEnded"})
        session = self.public.get(f"/api/tables/by-session/{table.session_token}/").json()
        self.assertIsNone(session["open_order"])
        self.assertIsNone(session["bill_id"])
        mine = self.public.get(f"/api/tables/by-session/{table.session_token}/", {"phone": PHONE_A}).json()
        self.assertEqual(mine["open_order"]["id"], a["id"])

    def test_pay_requests_are_scoped_to_the_callers_bill(self):
        table = self.new_table()
        a = self.qr(table, phone=PHONE_A).json()
        self.dispatch_and_serve(a["id"])
        response = self.public.post("/api/bills/request-pay/", {
            "session_token": table.session_token, "method": "cash", "phone": PHONE_B,
        }, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Bill.objects.get(pk=a["bill_id"]).status, "open")
        response = self.public.post("/api/bills/request-pay/", {
            "session_token": table.session_token, "method": "cash", "phone": PHONE_A, "bill_id": a["bill_id"],
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pay_requested")

    def test_previous_orders_are_shown_for_the_same_phone_only(self):
        table = self.new_table()
        first = self.qr(table, phone=PHONE_A).json()
        self.dispatch_and_serve(first["id"])
        self.pay_and_release(first["bill_id"])
        # Same phone, after release: no open bill, previous order listed with its date.
        response = self.bill_for(table, PHONE_A)
        self.assertEqual(response.status_code, 404)
        previous = response.json()["previous_orders"]
        self.assertEqual([row["order_number"] for row in previous], [first["order_number"]])
        self.assertIn("opened_at", previous[0])
        # A new visit shows the new bill, with the old order only as history.
        second = self.qr(table, phone=PHONE_A, items=[{"product": self.burger.pk}]).json()
        current = self.bill_for(table, PHONE_A).json()
        self.assertEqual([order["id"] for order in current["orders"]], [second["id"]])
        self.assertEqual([row["id"] for row in current["previous_orders"]], [first["id"]])
        # Another phone sees none of it.
        self.assertEqual(self.bill_for(table, PHONE_B).json()["previous_orders"], [])

    def test_old_unfinished_order_from_a_released_bill_is_never_adopted(self):
        table = self.new_table()
        old = self.qr(table, phone=PHONE_A).json()
        self.dispatch_and_serve(old["id"])
        self.pay_and_release(old["bill_id"])
        # Simulate the production leftover: an order still "confirmed" on a closed bill.
        Order.objects.filter(pk=old["id"]).update(status=OrderStatus.CONFIRMED)
        new = self.qr(table, phone=PHONE_A, items=[{"product": self.burger.pk}]).json()
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(Order.objects.get(pk=old["id"]).bill_id, old["bill_id"])
        staff = self.cashier_client.post("/api/orders/", {"table": table.pk}, format="json").json()
        self.assertNotEqual(staff["id"], old["id"])


class SameOrderAfterPaymentTests(PerPhoneBillTestCase):
    def test_same_phone_adds_after_paying_into_the_same_order_and_pays_only_new_items(self):
        table = self.new_table()
        first = self.qr(table, phone=PHONE_A).json()
        self.dispatch_and_serve(first["id"])
        self.assertEqual(self.cashier_client.post(f"/api/bills/{first['bill_id']}/mark-paid/", {"method": "cash"},
                                                  format="json").status_code, 200)
        self.assertEqual(Order.objects.get(pk=first["id"]).status, "paid")

        more = self.qr(table, phone=PHONE_A, items=[{"product": self.burger.pk}])
        self.assertEqual(more.status_code, 200)
        self.assertEqual(more.json()["id"], first["id"])
        order = Order.objects.get(pk=first["id"])
        self.assertEqual((order.status, order.payment_status), ("served", "partial"))
        bill = self.bill_for(table, PHONE_A).json()
        self.assertEqual((bill["status"], bill["total"], bill["amount_paid"], bill["amount_outstanding"]),
                         ("open", "35.00", "10.00", "25.00"))
        self.assertFalse(bill["ready_to_pay"])

        self.dispatch_and_serve(first["id"])
        self.assertEqual(self.cashier_client.post(f"/api/bills/{first['bill_id']}/mark-paid/", {"method": "cash"},
                                                  format="json").status_code, 200)
        amounts = sorted(Payment.objects.filter(order_id=first["id"]).values_list("amount", flat=True))
        self.assertEqual(amounts, [Decimal("10.00"), Decimal("25.00")])
        bill = self.bill_for(table, PHONE_A).json()
        self.assertEqual((bill["status"], bill["amount_paid"], bill["amount_outstanding"]), ("paid", "35.00", "0.00"))
        self.assertEqual(Order.objects.get(pk=first["id"]).status, "paid")

    def test_staff_add_to_paid_order_reopens_it_instead_of_rejecting(self):
        table, order_id, fries_id, body = self.order_to_ready()
        self.serve_everything(order_id)
        self.cashier_client.post(f"/api/bills/{body['bill_id']}/mark-paid/", {"method": "cash"}, format="json")
        response = self.cashier_client.post(f"/api/orders/{order_id}/items/", {"product": self.burger.pk}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Order.objects.get(pk=order_id).payment_status, "partial")
        self.assertEqual(Bill.objects.get(pk=body["bill_id"]).status, "open")


class PaymentAccountingTests(PerPhoneBillTestCase):
    def test_bill15_sequence_till_payments_then_online_settle_never_double_counts(self):
        """Replay of production BILL-00000015 on one phone's bill."""
        table = self.new_table()
        first = self.qr(table, phone=PHONE_A).json()
        order_id, bill_id = first["id"], first["bill_id"]
        self.dispatch_and_serve(order_id)
        # Cashier takes the money at the till for the order (POS payment page).
        self.assertEqual(self.till_pay(order_id, "till-1").status_code, 201)
        bill = Bill.objects.get(pk=bill_id)
        self.assertEqual(bill.status, "paid")  # covered by the till payment, not left "open"

        # Same customer adds more and pays online.
        self.qr(table, phone=PHONE_A, items=[{"product": self.burger.pk, "quantity": 2}])
        self.dispatch_and_serve(order_id)
        state = self.bill_for(table, PHONE_A).json()
        self.assertEqual((state["total"], state["amount_paid"], state["amount_outstanding"], state["total_halalas"]),
                         ("60.00", "10.00", "50.00", 5000))
        with patch.object(moyasar, "is_configured", return_value=True):
            requested = self.public.post("/api/bills/request-pay/", {
                "session_token": table.session_token, "method": "online", "phone": PHONE_A, "bill_id": bill_id,
            }, format="json")
        self.assertEqual(requested.status_code, 200)
        bill = Bill.objects.get(pk=bill_id)
        payload = {"id": "pay_1", "status": "paid", "amount": 5000, "currency": "SAR", "source": {"type": "creditcard"},
                   "metadata": {"bill_id": str(bill_id), "payment_request_at": bill.pay_requested_at.isoformat()}}
        with patch.object(moyasar, "is_configured", return_value=True), patch.object(moyasar, "fetch_payment", return_value=payload):
            verified = self.public.post("/api/bills/verify-online/", {"session_token": table.session_token, "payment_id": "pay_1"},
                                        format="json")
        self.assertEqual(verified.json()["result"], "settled")
        amounts = sorted(Payment.objects.filter(order_id=order_id).values_list("amount", flat=True))
        self.assertEqual(amounts, [Decimal("10.00"), Decimal("50.00")])  # exactly the money collected
        final = self.bill_for(table, PHONE_A).json()
        self.assertEqual((final["status"], final["total"], final["amount_paid"], final["amount_outstanding"]),
                         ("paid", "60.00", "60.00", "0.00"))

    def test_cancelled_orders_payment_does_not_discount_the_bill(self):
        table = self.new_table()
        first = self.qr(table, phone=PHONE_A).json()
        bill = Bill.objects.get(pk=first["bill_id"])
        cancelled = Order.objects.create(branch=self.branch, table=table, bill=bill, customer=bill.customer,
                                         status=OrderStatus.CANCELLED)
        Payment.objects.create(order=cancelled, amount=Decimal("40.00"), method="cash", idempotency_key="old-40")
        state = self.bill_for(table, PHONE_A).json()
        self.assertEqual((state["total"], state["amount_paid"], state["amount_outstanding"]), ("10.00", "0.00", "10.00"))

    def test_fully_paid_bill_is_never_offered_as_pay_zero(self):
        table = self.new_table()
        first = self.qr(table, phone=PHONE_A).json()
        self.dispatch_and_serve(first["id"])
        self.till_pay(first["id"], "till-zero")
        state = self.bill_for(table, PHONE_A).json()
        self.assertEqual((state["status"], state["amount_outstanding"], state["ready_to_pay"]), ("paid", "0.00", False))
        response = self.public.post("/api/bills/request-pay/", {
            "session_token": table.session_token, "method": "cash", "phone": PHONE_A,
        }, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "error.billAlreadyPaid")
