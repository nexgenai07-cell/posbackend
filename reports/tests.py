from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from orders.models import Order, OrderItem, OrderStatus, Payment, PaymentStatus


class PaymentMethodReportTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Test", name_ar="Test")
        self.owner = Staff.objects.create(branch=self.branch, name="Owner", role=StaffRole.OWNER, pin_hash="x")
        category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        self.product = Product.objects.create(branch=self.branch, category=category,
            name_en="Food", name_ar="Food", price=10, cost_price=4)
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def add_order(self, *, status=OrderStatus.CLOSED, payment_status=PaymentStatus.PAID, days_ago=0):
        order = Order.objects.create(branch=self.branch, status=status, payment_status=payment_status)
        Order.objects.filter(pk=order.pk).update(opened_at=timezone.now() - timedelta(days=days_ago))
        OrderItem.objects.create(order=order, product=self.product, name_en_snapshot="Food", name_ar_snapshot="Food",
            price_snapshot=10, quantity=1)
        return order

    def test_paid_cash_card_split_unpaid_and_cancelled_orders_are_counted_from_payments(self):
        cash = self.add_order()
        Payment.objects.create(order=cash, amount=Decimal("10.00"), method="cash")
        split = self.add_order()
        Payment.objects.create(order=split, amount=Decimal("4.00"), method="cash")
        Payment.objects.create(order=split, amount=Decimal("6.00"), method="card")
        unpaid = self.add_order(payment_status=PaymentStatus.PARTIAL)
        Payment.objects.create(order=unpaid, amount=Decimal("3.00"), method="cash")
        cancelled = self.add_order(status=OrderStatus.CANCELLED)
        Payment.objects.create(order=cancelled, amount=Decimal("10.00"), method="card")

        today = timezone.localdate().isoformat()
        response = self.client.get(f"/api/reports/sales-overview/?from={today}&to={today}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        methods = {row["method"]: row for row in data["by_payment_method"]}
        self.assertEqual(data["paid_order_count"], 2)
        self.assertEqual(Decimal(data["total_paid_amount"]), Decimal("20.00"))
        self.assertEqual(methods["cash"]["order_count"], 1)
        self.assertEqual(Decimal(methods["cash"]["total"]), Decimal("14.00"))
        self.assertEqual(methods["card"]["order_count"], 1)
        self.assertEqual(Decimal(methods["card"]["total"]), Decimal("6.00"))
        self.assertEqual(sum(row["order_count"] for row in methods.values()), data["paid_order_count"])

    def test_payment_summary_obeys_the_existing_date_range(self):
        today_order = self.add_order()
        Payment.objects.create(order=today_order, amount=Decimal("10.00"), method="cash")
        old_order = self.add_order(days_ago=5)
        Payment.objects.create(order=old_order, amount=Decimal("10.00"), method="card")
        today = timezone.localdate().isoformat()
        response = self.client.get(f"/api/reports/sales-overview/?from={today}&to={today}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["paid_order_count"], 1)
        self.assertEqual([row["method"] for row in response.json()["by_payment_method"]], ["cash"])

# Create your tests here.
