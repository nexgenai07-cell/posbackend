from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from inventory.models import InventoryItem, RecipeItem, StockMovementReason
from inventory.services import adjust_stock
from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus, Payment, PaymentStatus
from purchasing.models import Purchase, PurchaseItem, PurchaseStatus, Supplier


class FinancialAnalyticsTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Branch A", name_ar="A", timezone="Asia/Riyadh", currency="SAR")
        self.other_branch = Branch.objects.create(name_en="Branch B", name_ar="B", timezone="UTC")
        self.owner = Staff.objects.create(branch=self.branch, name="Owner", role=StaffRole.OWNER, pin_hash="x")
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        self.product = Product.objects.create(
            branch=self.branch, category=category, name_en="Meal", name_ar="Meal", price=65, cost_price=999,
        )
        self.ingredient = InventoryItem.objects.create(
            branch=self.branch, name="Ingredient A", unit="unit", current_stock=100, cost_per_unit=999,
        )
        RecipeItem.objects.create(product=self.product, inventory_item=self.ingredient, quantity=5, unit="unit")
        self.supplier = Supplier.objects.create(name_en="Supplier", name_ar="Supplier")
        self.purchase = Purchase.objects.create(branch=self.branch, supplier=self.supplier, status=PurchaseStatus.RECEIVED)
        self.purchase_item = PurchaseItem.objects.create(
            purchase=self.purchase, inventory_item=self.ingredient, quantity=5, unit_cost=5,
        )
        Purchase.objects.filter(pk=self.purchase.pk).update(received_at=timezone.now())

    def completed_order(self, *, days_ago=0, other_branch=False):
        branch = self.other_branch if other_branch else self.branch
        product = self.product
        order = Order.objects.create(
            branch=branch, status=OrderStatus.CLOSED, payment_status=PaymentStatus.PAID,
        )
        closed_at = timezone.now() - timedelta(days=days_ago)
        Order.objects.filter(pk=order.pk).update(opened_at=closed_at, closed_at=closed_at)
        item = OrderItem.objects.create(
            order=order, product=product, name_en_snapshot="Meal", name_ar_snapshot="Meal",
            price_snapshot=65, quantity=1, status=OrderItemStatus.SERVED,
        )
        Payment.objects.create(order=order, amount=65, method="cash", tip=5)
        if not other_branch:
            with transaction.atomic():
                adjust_stock(
                    self.ingredient.pk, -5, StockMovementReason.SALE,
                    order=order, order_item=item,
                )
        return order

    def test_purchase_summary_uses_received_po_actual_prices_not_inventory_display_cost(self):
        response = self.client.get("/api/purchases/summary/?preset=today")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(Decimal(data["total_purchase_cost"]), Decimal("25"))
        self.assertEqual(data["received_order_count"], 1)
        self.assertEqual(data["top_purchased_ingredients"][0]["name"], "Ingredient A")

    def test_financial_summary_uses_payment_and_historical_movement_cost(self):
        self.completed_order()
        response = self.client.get("/api/reports/financial-summary/?preset=today")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(Decimal(data["revenue"]), Decimal("65"))
        self.assertEqual(Decimal(data["ingredient_cost_consumed"]), Decimal("25"))
        self.assertEqual(Decimal(data["gross_profit"]), Decimal("40"))
        self.assertEqual(Decimal(data["gross_margin_pct"]).quantize(Decimal("0.01")), Decimal("61.54"))
        self.assertEqual(Decimal(data["tips"]), Decimal("5"))
        self.assertTrue(data["historical_cost_complete"])

    def test_product_profitability_does_not_use_catalog_estimate_as_historical_cost(self):
        self.completed_order()
        response = self.client.get("/api/reports/product-profitability/?preset=today")
        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(Decimal(row["ingredient_cost"]), Decimal("25"))
        self.assertEqual(Decimal(row["gross_profit"]), Decimal("40"))
        self.assertEqual(Decimal(row["current_recipe_cost_per_unit"]), Decimal("25"))

    def test_reports_are_branch_scoped_and_empty_ranges_return_zeroes(self):
        self.completed_order(other_branch=True)
        response = self.client.get("/api/reports/financial-summary/?preset=today")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(response.json()["revenue"]), Decimal("0"))
        empty = self.client.get("/api/reports/ingredient-costs/?preset=yesterday")
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(Decimal(str(empty.json()["results"][0]["purchase_spending"])), Decimal("0"))

    def test_old_sales_without_movement_cost_are_marked_incomplete(self):
        order = Order.objects.create(branch=self.branch, status=OrderStatus.CLOSED, payment_status=PaymentStatus.PAID)
        Order.objects.filter(pk=order.pk).update(closed_at=timezone.now())
        OrderItem.objects.create(
            order=order, product=self.product, name_en_snapshot="Meal", name_ar_snapshot="Meal",
            price_snapshot=65, quantity=1, status=OrderItemStatus.SERVED,
        )
        Payment.objects.create(order=order, amount=65, method="card")
        response = self.client.get("/api/reports/financial-summary/?preset=today")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["ingredient_cost_consumed"])
        self.assertIsNone(response.json()["gross_profit"])
        self.assertFalse(response.json()["historical_cost_complete"])
