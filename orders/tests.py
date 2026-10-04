from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from inventory.models import InventoryItem, RecipeItem, StockMovement
from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus


class OrderStockWorkflowTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Test", name_ar="Test")
        category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        self.product = Product.objects.create(
            branch=self.branch, category=category, name_en="Pizza", name_ar="Pizza", price=10, cost_price=3,
        )
        self.cheese = InventoryItem.objects.create(
            branch=self.branch, name="Cheese", unit="g", current_stock=Decimal("6"),
        )
        RecipeItem.objects.create(product=self.product, inventory_item=self.cheese, quantity=6, unit="g")
        self.second_product = Product.objects.create(
            branch=self.branch, category=category, name_en="Soup", name_ar="Soup", price=5, cost_price=2,
        )
        self.salt = InventoryItem.objects.create(
            branch=self.branch, name="Salt", unit="g", current_stock=Decimal("0"),
        )
        RecipeItem.objects.create(product=self.second_product, inventory_item=self.salt, quantity=1, unit="g")
        self.waiter = Staff.objects.create(
            branch=self.branch, name="Waiter", role=StaffRole.WAITER, pin_hash="x",
        )
        self.cashier = Staff.objects.create(
            branch=self.branch, name="Cashier", role=StaffRole.CASHIER, pin_hash="x",
        )
        self.order = Order.objects.create(
            branch=self.branch, status=OrderStatus.AWAITING_WAITER, assigned_waiter=self.waiter,
        )
        self.item = OrderItem.objects.create(
            order=self.order, product=self.product, name_en_snapshot="Pizza", name_ar_snapshot="Pizza",
            price_snapshot=10, quantity=1,
        )
        self.second_item = OrderItem.objects.create(
            order=self.order, product=self.second_product, name_en_snapshot="Soup", name_ar_snapshot="Soup",
            price_snapshot=5, quantity=1,
        )
        self.client = APIClient()

    def test_waiter_confirmation_rechecks_and_rolls_back_all_deductions(self):
        self.client.force_authenticate(user=self.waiter)
        response = self.client.post(f"/api/orders/{self.order.pk}/waiter-confirm/", {}, format="json")
        self.assertEqual(response.status_code, 409)
        self.order.refresh_from_db()
        self.cheese.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.AWAITING_WAITER)
        self.assertEqual(self.cheese.current_stock, Decimal("6.000"))
        self.assertFalse(StockMovement.objects.filter(order=self.order).exists())

        InventoryItem.objects.filter(pk=self.salt.pk).update(current_stock=Decimal("1"))
        response = self.client.post(f"/api/orders/{self.order.pk}/waiter-confirm/", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.cheese.refresh_from_db()
        self.salt.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.SENT)
        self.assertEqual(self.cheese.current_stock, Decimal("0.000"))
        self.assertEqual(self.salt.current_stock, Decimal("0.000"))
        self.assertEqual(StockMovement.objects.filter(order=self.order).count(), 2)

    def test_cashier_quantity_edit_cannot_bypass_stock_availability(self):
        self.order.status = OrderStatus.PENDING_CASHIER
        self.order.save(update_fields=["status", "updated_at"])
        self.client.force_authenticate(user=self.cashier)
        response = self.client.patch(
            f"/api/orders/{self.order.pk}/items/{self.item.pk}/", {"quantity": 2}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)

    def test_cashier_and_waiter_cannot_add_unavailable_products(self):
        self.order.status = OrderStatus.PENDING_CASHIER
        self.order.save(update_fields=["status", "updated_at"])
        existing_count = self.order.items.filter(product=self.second_product).count()
        self.client.force_authenticate(user=self.cashier)
        response = self.client.post(
            f"/api/orders/{self.order.pk}/items/", {"product": self.second_product.pk, "quantity": 1}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Salt", str(response.data))
        self.assertEqual(self.order.items.filter(product=self.second_product).count(), existing_count)

        self.order.status = OrderStatus.AWAITING_WAITER
        self.order.save(update_fields=["status", "updated_at"])
        self.client.force_authenticate(user=self.waiter)
        response = self.client.post(
            f"/api/orders/{self.order.pk}/items/", {"product": self.second_product.pk, "quantity": 1}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Salt", str(response.data))
        self.assertEqual(self.order.items.filter(product=self.second_product).count(), existing_count)

    def test_waiter_quantity_edit_cannot_bypass_stock_availability(self):
        self.client.force_authenticate(user=self.waiter)
        response = self.client.patch(
            f"/api/orders/{self.order.pk}/items/{self.item.pk}/", {"quantity": 2}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)

    def test_waiter_cannot_dispatch_product_disabled_after_order_placement(self):
        Product.objects.filter(pk=self.product.pk).update(is_available=False)
        self.client.force_authenticate(user=self.waiter)
        response = self.client.post(f"/api/orders/{self.order.pk}/waiter-confirm/", {}, format="json")
        self.assertEqual(response.status_code, 409)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.AWAITING_WAITER)

    def test_cancel_before_dispatch_voids_items_without_touching_stock(self):
        self.client.force_authenticate(user=self.cashier)
        response = self.client.post(f"/api/orders/{self.order.pk}/cancel/", {"reason": "Guest changed mind"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.cheese.refresh_from_db()
        self.assertEqual(self.item.status, OrderItemStatus.VOIDED)
        self.assertEqual(self.cheese.current_stock, Decimal("6.000"))
        self.assertFalse(StockMovement.objects.filter(order=self.order).exists())

    def test_void_after_dispatch_keeps_deducted_stock_consumed(self):
        # Make the second item orderable so the normal waiter-confirm flow
        # fires both items and records their ingredient deductions.
        InventoryItem.objects.filter(pk=self.salt.pk).update(current_stock=1)
        self.client.force_authenticate(user=self.waiter)
        response = self.client.post(f"/api/orders/{self.order.pk}/waiter-confirm/", {}, format="json")
        self.assertEqual(response.status_code, 200)
        sale_movements_before_void = StockMovement.objects.filter(order=self.order).count()
        self.assertEqual(sale_movements_before_void, 2)

        kitchen = Staff.objects.create(branch=self.branch, name="Kitchen", role=StaffRole.KITCHEN, pin_hash="x")
        self.client.force_authenticate(user=kitchen)
        response = self.client.patch(
            f"/api/orders/{self.order.pk}/items/{self.item.pk}/status/", {"status": OrderItemStatus.VOIDED}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.cheese.refresh_from_db()
        self.assertEqual(self.item.status, OrderItemStatus.VOIDED)
        self.assertEqual(self.cheese.current_stock, Decimal("0.000"))
        self.assertEqual(StockMovement.objects.filter(order=self.order).count(), sale_movements_before_void)
        self.assertFalse(StockMovement.objects.filter(order=self.order, quantity_delta__gt=0).exists())

    def test_repeating_item_void_does_not_change_stock_or_history(self):
        InventoryItem.objects.filter(pk=self.salt.pk).update(current_stock=1)
        self.client.force_authenticate(user=self.waiter)
        response = self.client.post(f"/api/orders/{self.order.pk}/waiter-confirm/", {}, format="json")
        self.assertEqual(response.status_code, 200)

        kitchen = Staff.objects.create(branch=self.branch, name="Kitchen", role=StaffRole.KITCHEN, pin_hash="x")
        self.client.force_authenticate(user=kitchen)
        url = f"/api/orders/{self.order.pk}/items/{self.item.pk}/status/"
        response = self.client.patch(url, {"status": OrderItemStatus.VOIDED}, format="json")
        self.assertEqual(response.status_code, 200)
        self.cheese.refresh_from_db()
        stock_after_first_void = self.cheese.current_stock
        movements_after_first_void = StockMovement.objects.filter(order=self.order).count()

        response = self.client.patch(url, {"status": OrderItemStatus.VOIDED}, format="json")
        self.assertEqual(response.status_code, 409)
        self.cheese.refresh_from_db()
        self.assertEqual(self.cheese.current_stock, stock_after_first_void)
        self.assertEqual(StockMovement.objects.filter(order=self.order).count(), movements_after_first_void)
