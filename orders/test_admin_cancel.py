from decimal import Decimal

from django.db import transaction
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from inventory.models import InventoryItem, RecipeItem, StockMovement, StockMovementReason
from inventory.services import deduct_stock_for_order_items
from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus, Payment, PaymentStatus


class AdminCancelTests(TestCase):
    """Owners and managers can cancel at any stage; cashiers only before the kitchen."""

    def setUp(self):
        self.branch = Branch.objects.create(name_en="Test", name_ar="Test")
        category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        self.product = Product.objects.create(branch=self.branch, category=category, name_en="Pizza", name_ar="Pizza", price=10, cost_price=0)
        self.cheese = InventoryItem.objects.create(branch=self.branch, name="Cheese", unit="g", current_stock=Decimal("100"))
        RecipeItem.objects.create(product=self.product, inventory_item=self.cheese, quantity=6, unit="g")
        self.owner = Staff.objects.create(branch=self.branch, name="Owner", role=StaffRole.OWNER, pin_hash="x")
        self.cashier = Staff.objects.create(branch=self.branch, name="Cashier", role=StaffRole.CASHIER, pin_hash="x")
        self.client = APIClient()

    def order(self, status, *, fired=False, paid=None):
        order = Order.objects.create(branch=self.branch, status=status)
        item = OrderItem.objects.create(
            order=order, product=self.product, name_en_snapshot="Pizza", name_ar_snapshot="Pizza", price_snapshot=10, quantity=2,
        )
        if fired:
            with transaction.atomic():
                deduct_stock_for_order_items([item])
            OrderItem.objects.filter(pk=item.pk).update(status=OrderItemStatus.SERVED)
        if paid is not None:
            Payment.objects.create(order=order, amount=paid, method="cash")
            Order.objects.filter(pk=order.pk).update(payment_status=PaymentStatus.PAID)
        return order

    def cancel(self, order, user, reason=""):
        self.client.force_authenticate(user=user)
        return self.client.post(f"/api/orders/{order.pk}/cancel/", {"reason": reason}, format="json")

    def test_cashier_still_cannot_cancel_once_in_the_kitchen(self):
        order = self.order(OrderStatus.PREPARING, fired=True)
        self.assertEqual(self.cancel(order, self.cashier, "x").json()["error"], "error.orderAlreadyInKitchen")

    def test_owner_cancels_served_order_and_cooked_stock_becomes_waste(self):
        order = self.order(OrderStatus.SERVED, fired=True)
        # After food is made a reason is required.
        self.assertEqual(self.cancel(order, self.owner).json()["error"], "error.cancelReasonRequired")
        response = self.cancel(order, self.owner, "Customer left")
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(
            (order.status, order.cancelled_from_status, order.cancelled_total, order.refunded_amount, order.cancelled_by),
            (OrderStatus.CANCELLED, OrderStatus.SERVED, Decimal("20.00"), None, self.owner),
        )
        reasons = set(StockMovement.objects.filter(order=order).values_list("reason", flat=True))
        self.assertEqual(reasons, {StockMovementReason.WASTE})
        # Stock is not given back: the cheese was used.
        self.cheese.refresh_from_db()
        self.assertEqual(self.cheese.current_stock, Decimal("88.000"))

    def test_owner_cancels_a_paid_order_and_the_payment_is_owed_back(self):
        order = self.order(OrderStatus.CLOSED, fired=True, paid=Decimal("20"))
        self.assertEqual(self.cancel(order, self.owner, "Wrong dish, refunded").status_code, 200)
        order.refresh_from_db()
        self.assertEqual((order.status, order.refunded_amount), (OrderStatus.CANCELLED, Decimal("20.00")))
        self.assertEqual(Decimal(str(self.client.get("/api/reports/financial-summary/?preset=today").json()["revenue"])), 0)

    def test_reason_is_optional_before_the_kitchen_and_cancelling_twice_conflicts(self):
        order = self.order(OrderStatus.OPEN)
        self.assertEqual(self.cancel(order, self.owner).status_code, 200)
        self.assertEqual(self.cancel(order, self.owner, "again").status_code, 409)

    def test_preview_says_what_cancelling_would_cost(self):
        order = self.order(OrderStatus.SERVED, fired=True, paid=Decimal("20"))
        self.client.force_authenticate(user=self.owner)
        data = self.client.get(f"/api/orders/{order.pk}/cancel/").json()
        self.assertEqual(
            (data["allowed"], Decimal(data["total"]), Decimal(data["refund"]), data["cooked_plates"], data["reason_required"]),
            (True, Decimal("20"), Decimal("20"), 2, True),
        )
        self.client.force_authenticate(user=self.cashier)
        self.assertFalse(self.client.get(f"/api/orders/{order.pk}/cancel/").json()["allowed"])
