from decimal import Decimal
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import close_old_connections, connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from inventory.models import InventoryItem, RecipeItem, StockMovement, StockMovementReason
from inventory.services import ProductStockUnavailable, adjust_stock, check_product_stock, deduct_stock_for_order_item, deduct_stock_for_order_items
from orders.models import Order, OrderItem, OrderStatus


class InventoryAvailabilityTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Test", name_ar="Test")
        self.category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        self.product = Product.objects.create(
            branch=self.branch, category=self.category, name_en="Pizza", name_ar="Pizza",
            price=10, cost_price=3,
        )
        self.cheese = InventoryItem.objects.create(
            branch=self.branch, name="Cheese", unit="g", current_stock=Decimal("11"),
        )
        self.recipe = RecipeItem.objects.create(
            product=self.product, inventory_item=self.cheese, quantity=Decimal("6"), unit="g",
        )

    def test_missing_and_empty_recipe_are_unavailable(self):
        self.recipe.delete()
        self.assertEqual(check_product_stock(self.product), (False, "missing_recipe"))

    def test_available_boundaries_and_requested_quantities(self):
        cases = [("-1", 1, False), ("0", 1, False), ("5", 1, False), ("6", 1, True),
                 ("11", 1, True), ("12", 2, True), ("11", 2, False)]
        for stock, quantity, expected in cases:
            with self.subTest(stock=stock, quantity=quantity):
                InventoryItem.objects.filter(pk=self.cheese.pk).update(current_stock=Decimal(stock))
                self.cheese.refresh_from_db()
                self.assertEqual(check_product_stock(self.product, quantity)[0], expected)

    def test_all_ingredients_and_unit_conversions_are_checked(self):
        flour = InventoryItem.objects.create(
            branch=self.branch, name="Flour", unit="kg", current_stock=Decimal("0.1"),
        )
        RecipeItem.objects.create(
            product=self.product, inventory_item=flour, quantity=Decimal("100"), unit="g",
        )
        self.assertTrue(check_product_stock(self.product)[0])
        InventoryItem.objects.filter(pk=flour.pk).update(current_stock=Decimal("0.099"))
        self.assertFalse(check_product_stock(self.product)[0])

    def test_incompatible_units_are_invalid(self):
        RecipeItem.objects.filter(pk=self.recipe.pk).update(unit="ml")
        self.assertEqual(check_product_stock(self.product), (False, "invalid_recipe"))

    def test_dispatch_deducts_exact_recipe_quantity_and_writes_ledger(self):
        order = Order.objects.create(branch=self.branch, status=OrderStatus.CONFIRMED)
        item = OrderItem.objects.create(
            order=order, product=self.product, name_en_snapshot="Pizza", name_ar_snapshot="Pizza",
            price_snapshot=10, quantity=2,
        )
        # Current stock 11 is one short for two products.
        with self.assertRaises(ProductStockUnavailable):
            with transaction.atomic():
                deduct_stock_for_order_item(item)
        self.assertFalse(StockMovement.objects.filter(order=order).exists())
        InventoryItem.objects.filter(pk=self.cheese.pk).update(current_stock=Decimal("12"))
        with transaction.atomic():
            deduct_stock_for_order_item(item)
        self.cheese.refresh_from_db()
        self.assertEqual(self.cheese.current_stock, Decimal("0.000"))
        movement = StockMovement.objects.get(order=order, reason=StockMovementReason.SALE)
        self.assertEqual(movement.previous_stock, Decimal("12.000"))
        self.assertEqual(movement.quantity_delta, Decimal("-12.000"))
        self.assertEqual(movement.resulting_stock, Decimal("0.000"))

    def test_stock_receipt_is_an_increment(self):
        InventoryItem.objects.filter(pk=self.cheese.pk).update(current_stock=Decimal("5"))
        with transaction.atomic():
            adjust_stock(self.cheese.pk, Decimal("5"), StockMovementReason.PURCHASE)
        self.cheese.refresh_from_db()
        self.assertEqual(self.cheese.current_stock, Decimal("10.000"))
        movement = StockMovement.objects.get(inventory_item=self.cheese)
        self.assertEqual(movement.previous_stock, Decimal("5.000"))
        self.assertEqual(movement.quantity_delta, Decimal("5.000"))
        self.assertEqual(movement.resulting_stock, Decimal("10.000"))

    def test_adjustment_cannot_create_a_negative_balance(self):
        from django.core.exceptions import ValidationError

        before = StockMovement.objects.count()
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                adjust_stock(self.cheese.pk, Decimal("-12"), StockMovementReason.WASTE)
        self.cheese.refresh_from_db()
        self.assertEqual(self.cheese.current_stock, Decimal("11.000"))
        self.assertEqual(StockMovement.objects.count(), before)

    def test_shared_ingredient_is_aggregated_across_products_in_one_order(self):
        second_product = Product.objects.create(
            branch=self.branch, category=self.category, name_en="Second", name_ar="Second", price=1, cost_price=1,
        )
        RecipeItem.objects.create(product=second_product, inventory_item=self.cheese, quantity=6, unit="g")
        order = Order.objects.create(branch=self.branch, status=OrderStatus.CONFIRMED)
        first = OrderItem.objects.create(
            order=order, product=self.product, name_en_snapshot="Pizza", name_ar_snapshot="Pizza",
            price_snapshot=1, quantity=1,
        )
        second = OrderItem.objects.create(
            order=order, product=second_product, name_en_snapshot="Second", name_ar_snapshot="Second",
            price_snapshot=1, quantity=1,
        )
        with self.assertRaises(ProductStockUnavailable):
            with transaction.atomic():
                deduct_stock_for_order_items([first, second])
        self.cheese.refresh_from_db()
        self.assertEqual(self.cheese.current_stock, Decimal("11.000"))
        self.assertFalse(StockMovement.objects.filter(order=order).exists())

    def test_public_menu_excludes_unorderable_product_and_restores_after_stock(self):
        from rest_framework.test import APIClient

        client = APIClient()
        InventoryItem.objects.filter(pk=self.cheese.pk).update(current_stock=Decimal("-11"))
        response = client.get(f"/api/menu/?branch={self.branch.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.product.pk, [
            product["id"] for category in response.data["categories"] for product in category["products"]
        ])
        InventoryItem.objects.filter(pk=self.cheese.pk).update(current_stock=Decimal("6"))
        response = client.get(f"/api/menu/?branch={self.branch.pk}")
        self.assertIn(self.product.pk, [
            product["id"] for category in response.data["categories"] for product in category["products"]
        ])

    def test_public_customer_order_rechecks_stock(self):
        from tables.models import Table

        table = Table.objects.create(branch=self.branch, label_en="T1", label_ar="T1")
        self.branch.latitude = Decimal("24.713600")
        self.branch.longitude = Decimal("46.675300")
        self.branch.save(update_fields=["latitude", "longitude", "updated_at"])
        InventoryItem.objects.filter(pk=self.cheese.pk).update(current_stock=Decimal("-11"))
        response = APIClient().post("/api/orders/qr/", {
            "session_token": table.session_token,
            "phone": "123456789",
            "request_id": "stale-cart-test",
            "latitude": 24.7136, "longitude": 46.6753, "accuracy_m": 0.2,
            "location_timestamp": time.time(),
            "items": [{"product": self.product.pk, "quantity": 1}],
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Order.objects.filter(table=table).exists())


class InventoryPermissionTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Test", name_ar="Test")
        self.client = APIClient()

    def test_cashier_waiter_and_kitchen_cannot_read_inventory_apis(self):
        paths = ["/api/inventory-items/", "/api/inventory-items/low-stock/",
                 "/api/stock-movements/", "/api/recipes/"]
        for role in (StaffRole.CASHIER, StaffRole.WAITER, StaffRole.KITCHEN):
            staff = Staff.objects.create(branch=self.branch, name=role, role=role, pin_hash="x")
            self.client.force_authenticate(user=staff)
            for path in paths:
                with self.subTest(role=role, path=path):
                    self.assertEqual(self.client.get(path).status_code, 403)

    def test_owner_can_read_inventory_and_product_orderability_has_no_stock_detail(self):
        owner = Staff.objects.create(branch=self.branch, name="Owner", role=StaffRole.OWNER, pin_hash="x")
        category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        product = Product.objects.create(
            branch=self.branch, category=category, name_en="No recipe", name_ar="No recipe", price=1, cost_price=1,
        )
        self.client.force_authenticate(user=owner)
        self.assertEqual(self.client.get("/api/inventory-items/").status_code, 200)
        response = self.client.get(f"/api/products/{product.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["is_orderable"])
        self.assertNotIn("current_stock", response.data)

    def test_initial_stock_and_positive_adjustments_increment_with_history(self):
        owner = Staff.objects.create(branch=self.branch, name="Owner", role=StaffRole.OWNER, pin_hash="x")
        self.client.force_authenticate(user=owner)
        response = self.client.post("/api/inventory-items/", {
            "branch": self.branch.pk, "name": "Flour", "unit": "g", "current_stock": "5",
        }, format="json")
        self.assertEqual(response.status_code, 201)
        item_id = response.data["id"]
        self.assertEqual(Decimal(response.data["current_stock"]), Decimal("5.000"))
        opening = StockMovement.objects.get(inventory_item_id=item_id)
        self.assertEqual(opening.previous_stock, Decimal("0.000"))
        self.assertEqual(opening.quantity_delta, Decimal("5.000"))
        self.assertEqual(opening.resulting_stock, Decimal("5.000"))

        response = self.client.post(f"/api/inventory-items/{item_id}/adjust/", {
            "quantity_delta": "5", "reason": "adjustment",
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(response.data["current_stock"]), Decimal("10.000"))
        movement = StockMovement.objects.filter(inventory_item_id=item_id).order_by("id").last()
        self.assertEqual(movement.previous_stock, Decimal("5.000"))
        self.assertEqual(movement.quantity_delta, Decimal("5.000"))
        self.assertEqual(movement.resulting_stock, Decimal("10.000"))

    def test_adjustment_api_rejects_a_negative_resulting_balance(self):
        owner = Staff.objects.create(branch=self.branch, name="Owner", role=StaffRole.OWNER, pin_hash="x")
        item = InventoryItem.objects.create(branch=self.branch, name="Flour", unit="g", current_stock=5)
        self.client.force_authenticate(user=owner)
        response = self.client.post(f"/api/inventory-items/{item.pk}/adjust/", {
            "quantity_delta": "-6", "reason": "waste",
        }, format="json")
        self.assertEqual(response.status_code, 409)
        item.refresh_from_db()
        self.assertEqual(item.current_stock, Decimal("5.000"))
        self.assertFalse(StockMovement.objects.filter(inventory_item=item).exists())


class RecipeIntegrityTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Test", name_ar="Test")
        self.other_branch = Branch.objects.create(name_en="Other", name_ar="Other")
        category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        self.product = Product.objects.create(
            branch=self.branch, category=category, name_en="Pizza", name_ar="Pizza", price=1, cost_price=1,
        )
        self.ingredient = InventoryItem.objects.create(branch=self.branch, name="Flour", unit="g")
        self.other_ingredient = InventoryItem.objects.create(branch=self.other_branch, name="Flour", unit="g")

    def test_invalid_quantities_units_and_cross_branch_are_rejected(self):
        from django.core.exceptions import ValidationError

        for quantity, unit, ingredient in ((0, "g", self.ingredient), (1, "", self.ingredient),
                                           (1, "g", self.other_ingredient), (1, "ml", self.ingredient)):
            with self.subTest(quantity=quantity, unit=unit, branch=ingredient.branch_id):
                recipe = RecipeItem(product=self.product, inventory_item=ingredient, quantity=quantity, unit=unit)
                with self.assertRaises(ValidationError):
                    recipe.full_clean()


class ConcurrentStockDispatchTests(TransactionTestCase):
    """SELECT FOR UPDATE must serialize two orders consuming the last stock."""

    @skipUnlessDBFeature("has_select_for_update")
    def test_only_one_concurrent_order_can_consume_last_ingredient(self):
        branch = Branch.objects.create(name_en="Concurrency", name_ar="Concurrency")
        category = Category.objects.create(branch=branch, name_en="Food", name_ar="Food")
        product = Product.objects.create(
            branch=branch, category=category, name_en="Item", name_ar="Item", price=1, cost_price=1,
        )
        ingredient = InventoryItem.objects.create(branch=branch, name="Last unit", unit="unit", current_stock=1)
        RecipeItem.objects.create(product=product, inventory_item=ingredient, quantity=1, unit="unit")
        item_ids = []
        for _ in range(2):
            order = Order.objects.create(branch=branch, status=OrderStatus.CONFIRMED)
            item = OrderItem.objects.create(
                order=order, product=product, name_en_snapshot="Item", name_ar_snapshot="Item",
                price_snapshot=1, quantity=1,
            )
            item_ids.append(item.pk)

        barrier = Barrier(2)

        def dispatch(item_id):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    item = OrderItem.objects.select_related("product", "order").get(pk=item_id)
                    deduct_stock_for_order_item(item)
                return "deducted"
            except ProductStockUnavailable:
                return "unavailable"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(dispatch, item_ids))
        self.assertCountEqual(results, ["deducted", "unavailable"])
        ingredient.refresh_from_db()
        self.assertEqual(ingredient.current_stock, Decimal("0.000"))
        self.assertEqual(StockMovement.objects.filter(reason=StockMovementReason.SALE).count(), 1)
