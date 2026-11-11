"""
Cash tendered & change (docs/cash-change-plan.md, Milestone 1).

The bill in BillingTestBase is one served order of 2 x 25.50 = 51.00.
`Payment.amount` must always stay what was charged; the tendered notes and the
change are recorded beside it and never alter takings.
"""

import uuid
from decimal import Decimal

from orders.models import BillMethod, BillStatus, OrderItemStatus, OrderStatus, Payment

from .test_billing import BillingTestBase

D = Decimal


class TillPaymentCashTests(BillingTestBase):
    """POST /api/orders/<id>/payments/ — the POS PaymentPage."""

    def pay(self, request_id=None, **body):
        payload = {"request_id": request_id or uuid.uuid4().hex, "amount": "51.00", "method": "cash", **body}
        return self.as_staff(self.cashier).post(f"/api/orders/{self.order.pk}/payments/", payload, format="json")

    def test_overpay_records_change(self):
        response = self.pay(cash_tendered="100.00")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["cash_tendered"], "100.00")
        self.assertEqual(response.data["change_given"], "49.00")
        payment = Payment.objects.get(order=self.order)
        self.assertEqual((payment.amount, payment.cash_tendered, payment.change_given), (D("51.00"), D("100.00"), D("49.00")))

    def test_exact_cash_has_zero_change(self):
        response = self.pay(cash_tendered="51.00")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["change_given"], "0.00")

    def test_tip_is_taken_out_of_the_change(self):
        response = self.pay(cash_tendered="60.00", tip="4.00")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["change_given"], "5.00")

    def test_tendered_below_amount_plus_tip_is_rejected(self):
        response = self.pay(cash_tendered="54.00", tip="4.00")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["cash_tendered"], ["error.cashTenderedTooLow"])
        self.assertFalse(Payment.objects.exists())

    def test_tendered_with_card_is_rejected(self):
        response = self.pay(method="card", cash_tendered="100.00")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["cash_tendered"], ["error.cashTenderedNotCash"])
        self.assertFalse(Payment.objects.exists())

    def test_cash_without_tendered_is_still_accepted(self):
        response = self.pay()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["cash_tendered"])
        self.assertIsNone(response.data["change_given"])

    def test_replay_must_match_the_tendered_amount(self):
        request_id = uuid.uuid4().hex
        self.assertEqual(self.pay(request_id, cash_tendered="100.00").status_code, 201)
        self.assertEqual(self.pay(request_id, cash_tendered="100.00").status_code, 200)
        self.assertEqual(self.pay(request_id, cash_tendered="200.00").status_code, 409)
        self.assertEqual(Payment.objects.count(), 1)


class BillSettleCashTests(BillingTestBase):
    """POST /api/bills/<id>/mark-paid/ — the BillsPage "Cash received"."""

    def settle(self, **body):
        return self.as_staff(self.cashier).post(
            f"/api/bills/{self.bill.pk}/mark-paid/", {"method": "cash", **body}, format="json"
        )

    def test_overpay_records_change_on_the_bill(self):
        response = self.settle(cash_tendered="100.00")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["result"], "settled")
        self.assertEqual(response.data["cash_tendered"], "100.00")
        self.assertEqual(response.data["change_given"], "49.00")
        self.refresh()
        self.assertEqual(self.bill.status, BillStatus.PAID)
        self.assertEqual((self.bill.cash_tendered, self.bill.change_given), (D("100.00"), D("49.00")))
        self.assertIsNone(self.bill.tip_at_payment)
        payment = Payment.objects.get(order=self.order)
        self.assertEqual((payment.amount, payment.tip), (D("51.00"), None))

    def test_keep_the_change_becomes_the_tip(self):
        response = self.settle(cash_tendered="100.00", tip="49.00")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["change_given"], "0.00")
        self.assertEqual(response.data["tip_at_payment"], "49.00")
        payment = Payment.objects.get(order=self.order)
        self.assertEqual((payment.amount, payment.tip), (D("51.00"), D("49.00")))

    def test_short_cash_is_rejected_and_nothing_is_settled(self):
        response = self.settle(cash_tendered="50.00")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["cash_tendered"], ["error.cashTenderedTooLow"])
        self.refresh()
        self.assertNotEqual(self.bill.status, BillStatus.PAID)
        self.assertEqual(self.order.status, OrderStatus.SERVED)
        self.assertFalse(Payment.objects.exists())

    def test_tip_counts_towards_what_must_be_tendered(self):
        response = self.settle(cash_tendered="55.00", tip="5.00")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Payment.objects.exists())

    def test_tendered_with_card_is_rejected(self):
        response = self.settle(method=BillMethod.CARD, cash_tendered="100.00")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["cash_tendered"], ["error.cashTenderedNotCash"])

    def test_cash_without_tendered_is_still_accepted(self):
        response = self.settle()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["cash_tendered"])
        self.assertIsNone(response.data["change_given"])

    def test_tip_is_written_once_across_several_orders(self):
        second = self._order(self.bill, OrderStatus.SERVED)
        self._item(second, quantity=1, status=OrderItemStatus.SERVED)  # 25.50; bill now 76.50
        response = self.settle(cash_tendered="100.00", tip="3.50")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["change_given"], "20.00")
        tips = list(Payment.objects.order_by("pk").values_list("tip", flat=True))
        self.assertEqual(tips, [None, D("3.50")])

    def test_only_the_still_owed_part_must_be_tendered(self):
        # 30.00 already taken at the till for this order: 21.00 left.
        self.as_staff(self.cashier).post(
            f"/api/orders/{self.order.pk}/payments/",
            {"request_id": uuid.uuid4().hex, "amount": "30.00", "method": "card"}, format="json",
        )
        response = self.settle(cash_tendered="50.00")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["change_given"], "29.00")
