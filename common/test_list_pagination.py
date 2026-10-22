from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from customers.models import Customer
from inventory.models import InventoryItem, RecipeItem
from orders.models import Order, Payment
from tables.models import Table


class AdminListPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(name_en="Main", name_ar="Main", timezone="Asia/Riyadh", currency="SAR")
        cls.staff = Staff.objects.create(branch=cls.branch, name="Manager", role=StaffRole.MANAGER)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.staff)

    def get(self, path, **params):
        response = self.client.get(path, {"page": 1, "page_size": 2, **params})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_table_pages_are_sorted_globally_and_metadata_is_not_page_scoped(self):
        for label in ["C", "A", "B"]:
            Table.objects.create(branch=self.branch, label_en=label, label_ar=label, status="occupied")
        other = Branch.objects.create(name_en="Other", name_ar="Other")
        Table.objects.create(branch=other, label_en="Hidden", label_ar="Hidden")
        first = self.get("/api/tables/", ordering="label")
        second = self.get("/api/tables/", page=2, ordering="label")
        self.assertEqual(first["count"], 3)
        self.assertEqual([row["label_en"] for row in first["results"]], ["A", "B"])
        self.assertEqual([row["label_en"] for row in second["results"]], ["C"])
        self.assertEqual(first["metadata"]["in_use"], 3)
        self.assertEqual(self.get("/api/tables/", search="C")["count"], 1)

    def test_categories_and_products_filter_before_pagination(self):
        category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        empty = Category.objects.create(branch=self.branch, name_en="Empty", name_ar="Empty")
        ingredient = InventoryItem.objects.create(branch=self.branch, name="Flour", unit="kg", current_stock=10, cost_per_unit=2)
        for index in range(3):
            product = Product.objects.create(branch=self.branch, category=category, name_en=f"Dish {index}", name_ar=f"Dish {index}", price=10, cost_price=1)
            RecipeItem.objects.create(product=product, inventory_item=ingredient, quantity=1, unit="kg")
        Product.objects.create(branch=self.branch, category=empty, name_en="Deleted", name_ar="Deleted", price=10, cost_price=1, is_deleted=True)
        data = self.get("/api/categories/", has_products="true", ordering="-products")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["product_count"], 3)
        data = self.get("/api/products/", has_recipe="true", ordering="-name")
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["metadata"]["with_recipe"], 3)
        self.assertEqual([row["name_en"] for row in data["results"]], ["Dish 2", "Dish 1"])
        product_id = data["results"][0]["id"]
        recipes = self.get("/api/recipes/", products=str(product_id))
        self.assertEqual(recipes["count"], 1)
        self.assertEqual(recipes["results"][0]["product"], product_id)

    def test_inventory_totals_and_unit_choices_cover_whole_selection(self):
        for index in range(3):
            InventoryItem.objects.create(branch=self.branch, name=f"Item {index}", unit="kg", current_stock=2, par_level=3, cost_per_unit=5)
        InventoryItem.objects.create(branch=self.branch, name="Separate", unit="litre", current_stock=10, par_level=1, cost_per_unit=1)
        data = self.get("/api/inventory-items/", stock_status="low", ordering="-value")
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["metadata"]["low_stock"], 3)
        self.assertEqual(Decimal(str(data["metadata"]["stock_value"])), Decimal("30"))
        self.assertEqual(set(data["metadata"]["units"]), {"kg", "litre"})

    def test_customer_history_totals_are_not_limited_to_current_page(self):
        customer = Customer.objects.create(branch=self.branch, phone="0501111111")
        for index in range(3):
            order = Order.objects.create(branch=self.branch, customer=customer, status="closed", payment_status="paid")
            Payment.objects.create(order=order, amount=10, method="cash", idempotency_key=f"history-{index}")
        data = self.get("/api/orders/", customer=customer.pk, ordering="total")
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["metadata"]["paid_count"], 3)
        self.assertEqual(Decimal(str(data["metadata"]["total_spend"])), Decimal("30"))

    def test_orders_report_can_reach_records_beyond_legacy_limit(self):
        Order.objects.bulk_create([Order(branch=self.branch, source="pos") for _ in range(105)])
        first = self.get("/api/reports/recent-orders/", page_size=100)
        second = self.get("/api/reports/recent-orders/", page=2, page_size=100)
        self.assertEqual(first["count"], 105)
        self.assertEqual(len(first["results"]), 100)
        self.assertEqual(len(second["results"]), 5)
        self.assertFalse({row["id"] for row in first["results"]} & {row["id"] for row in second["results"]})
        # Existing dashboard callers still receive their bounded array.
        legacy = self.client.get("/api/reports/recent-orders/", {"limit": 5})
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(len(legacy.data), 5)

    def test_staff_and_supplier_ordering_apply_before_page_slicing(self):
        from purchasing.models import Supplier
        for name in ["Zed", "Amy", "Bob"]:
            Staff.objects.create(branch=self.branch, name=name, role=StaffRole.WAITER)
            Supplier.objects.create(name_en=name, name_ar=name, contact_info=name)
        staff = self.get("/api/staff/", role="waiter", ordering="name")
        self.assertEqual(staff["count"], 3)
        self.assertEqual([row["name"] for row in staff["results"]], ["Amy", "Bob"])
        suppliers = self.get("/api/suppliers/", search="", ordering="-contact")
        self.assertEqual(suppliers["count"], 3)
        self.assertEqual([row["name_en"] for row in suppliers["results"]], ["Zed", "Bob"])

    def test_margin_ordering_uses_live_recipe_cost_before_slicing(self):
        category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        ingredient = InventoryItem.objects.create(branch=self.branch, name="Flour", unit="kg", current_stock=10, cost_per_unit=2)
        for name, quantity in [("Low", 4), ("High", 1), ("Middle", 2)]:
            product = Product.objects.create(branch=self.branch, category=category, name_en=name, name_ar=name, price=10, cost_price=0)
            RecipeItem.objects.create(product=product, inventory_item=ingredient, quantity=quantity, unit="kg")
        data = self.get("/api/products/", ordering="-margin")
        self.assertEqual(data["count"], 3)
        self.assertEqual([row["name_en"] for row in data["results"]], ["High", "Middle"])
        self.assertEqual(data["metadata"]["with_recipe"], 3)
