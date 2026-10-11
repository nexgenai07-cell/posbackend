from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from inventory.models import InventoryItem, RecipeItem
from inventory.services import orderable_units


class OrderableUnitsTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="B", name_ar="B", timezone="Asia/Riyadh")
        category = Category.objects.create(branch=self.branch, name_en="C", name_ar="C")
        self.burger = Product.objects.create(branch=self.branch, category=category, name_en="Burger", name_ar="Burger",
                                             price=10, cost_price=3)
        self.no_recipe = Product.objects.create(branch=self.branch, category=category, name_en="Tea", name_ar="Tea",
                                                price=2, cost_price=1)
        self.bun = InventoryItem.objects.create(branch=self.branch, name="Bun", unit="unit", current_stock=7)
        self.beef = InventoryItem.objects.create(branch=self.branch, name="Beef", unit="kg", current_stock=Decimal("1.000"))
        RecipeItem.objects.create(product=self.burger, inventory_item=self.bun, quantity=1, unit="unit")
        RecipeItem.objects.create(product=self.burger, inventory_item=self.beef, quantity=150, unit="g")

    def test_units_are_limited_by_the_scarcest_ingredient(self):
        # 7 buns, 1000 g / 150 g = 6.67 beef portions -> 6 burgers.
        self.assertEqual(orderable_units([self.burger, self.no_recipe]), {self.burger.pk: 6, self.no_recipe.pk: 0})

    def test_out_of_stock_is_zero(self):
        InventoryItem.objects.filter(pk=self.beef.pk).update(current_stock=Decimal("0.100"))
        self.assertEqual(orderable_units([self.burger])[self.burger.pk], 0)

    def test_product_list_exposes_available_quantity(self):
        cashier = Staff(branch=self.branch, name="C", role=StaffRole.CASHIER)
        cashier.set_pin("1")
        cashier.save()
        client = APIClient()
        client.force_authenticate(user=cashier)
        rows = {row["id"]: row for row in client.get("/api/products/").json()["results"]}
        self.assertEqual((rows[self.burger.pk]["is_orderable"], rows[self.burger.pk]["available_quantity"]), (True, 6))
        self.assertEqual((rows[self.no_recipe.pk]["is_orderable"], rows[self.no_recipe.pk]["available_quantity"]), (False, 0))
