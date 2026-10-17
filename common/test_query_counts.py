"""
List endpoints must cost the same number of queries for 2 rows as for 6.

Each query is a round trip to the database; one per row (an "N+1") turned
the menu into 36 queries and the purchases list into 75, seconds apart on a
remote database. These tests fail the moment a serializer starts reading a
relation that the view does not load up front.
"""

from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product, ProductVariant
from customers.models import Customer
from inventory.models import InventoryItem, RecipeItem, StockMovement, StockMovementReason
from orders.models import Bill, Order, OrderItem, OrderStatus, Payment
from purchasing.models import Purchase, PurchaseItem, PurchaseStatus, Supplier


class QueryCountTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Main", name_ar="Main")
        self.owner = Staff(branch=self.branch, name="Owner", role=StaffRole.OWNER)
        self.owner.set_pin("1234")
        self.owner.save()
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        self.supplier = Supplier.objects.create(name_en="Supplier", name_ar="")
        self.made = 0

    def queries(self, path):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return len(captured.captured_queries)

    def assertFlat(self, path, add_rows):
        """Same query count after add_rows() as before it."""
        add_rows(2)
        before = self.queries(path)
        add_rows(4)
        self.assertEqual(self.queries(path), before, f"{path} runs more queries as rows are added")

    # ── fixtures ───────────────────────────────────────────────────────────

    def ingredient(self):
        self.made += 1
        return InventoryItem.objects.create(
            branch=self.branch, name=f"Item {self.made}", unit="g", current_stock=Decimal("1000"), cost_per_unit=1,
        )

    def products(self, count):
        """Orderable dishes, each with a two-line recipe and two sizes."""
        for _ in range(count):
            self.made += 1
            product = Product.objects.create(
                branch=self.branch, category=self.category, name_en=f"Dish {self.made}", name_ar="",
                price=100, cost_price=0,
            )
            for _ in range(2):
                RecipeItem.objects.create(product=product, inventory_item=self.ingredient(), quantity=10, unit="g")
            ProductVariant.objects.create(product=product, name_en="Half", name_ar="", price=100, recipe_multiplier=1)
            ProductVariant.objects.create(product=product, name_en="Full", name_ar="", price=180, recipe_multiplier=2)

    def purchases(self, count):
        """Received purchases, two lines each, with their stock movements."""
        for _ in range(count):
            purchase = Purchase.objects.create(branch=self.branch, supplier=self.supplier, status=PurchaseStatus.RECEIVED)
            for _ in range(2):
                item = self.ingredient()
                line = PurchaseItem.objects.create(purchase=purchase, inventory_item=item, quantity=5, unit_cost=2)
                StockMovement.objects.create(
                    inventory_item=item, previous_stock=0, quantity_delta=5, resulting_stock=5,
                    reason=StockMovementReason.PURCHASE, purchase=purchase, purchase_item=line,
                )

    def bills(self, count):
        """Open bills, each its own customer, with two paid orders."""
        if not hasattr(self, "dish"):
            self.dish = Product.objects.create(branch=self.branch, category=self.category, name_en="Burger", name_ar="", price=10, cost_price=0)
        for _ in range(count):
            self.made += 1
            customer = Customer.objects.create(branch=self.branch, phone=f"0500000{self.made:03d}")
            bill = Bill.objects.create(branch=self.branch, customer=customer)
            for _ in range(2):
                order = Order.objects.create(branch=self.branch, bill=bill, customer=customer, status=OrderStatus.SERVED)
                OrderItem.objects.create(
                    order=order, product=self.dish, name_en_snapshot="Burger", name_ar_snapshot="",
                    price_snapshot=10, quantity=1,
                )
                Payment.objects.create(order=order, amount=5, method="cash")

    # ── endpoints ──────────────────────────────────────────────────────────

    def test_public_menu(self):
        self.assertFlat(f"/api/menu/?branch={self.branch.pk}", self.products)

    def test_products(self):
        self.assertFlat("/api/products/", self.products)

    def test_purchases(self):
        self.assertFlat("/api/purchases/", self.purchases)

    def test_bills(self):
        self.assertFlat("/api/bills/", self.bills)

    def test_orders(self):
        self.assertFlat("/api/orders/", self.bills)
