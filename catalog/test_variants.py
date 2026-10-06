"""
Menu variants and automatic recipe costing.

Two things these tests exist to pin down, because both fail silently rather
than loudly if they regress:

  1. UNIT CONVERSION. A recipe in grams against an ingredient priced per KG is
     where a costing engine goes wrong by a factor of 1000 and nobody notices
     until the margin report is questioned.

  2. THE TWO CLOCKS. Live costing must move when an ingredient price moves;
     order snapshots must not. Nothing errors if that boundary breaks — the
     numbers just quietly start lying.
"""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.costing import costing, recipe_cost, variant_cost
from catalog.models import Category, Product, ProductVariant
from catalog.services import current_price
from inventory.models import InventoryItem, RecipeItem
from orders.models import Order, OrderStatus
from tables.models import Table


class VariantTestBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.branch = Branch.objects.create(
            name_en="Main", name_ar="Main",
            latitude=Decimal("24.774265"), longitude=Decimal("46.738586"),
            geofence_radius_m=Decimal("1000"),
        )
        self.category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        # Chicken Karahi, base recipe = 1 KG of chicken, chicken at 600/KG.
        # Chosen to mirror the worked example in the requirements exactly.
        self.product = Product.objects.create(
            branch=self.branch, category=self.category,
            name_en="Chicken Karahi", name_ar="Chicken Karahi",
            price=Decimal("2200.00"), cost_price=Decimal("600.00"),
        )
        self.chicken = InventoryItem.objects.create(
            branch=self.branch, name="Chicken", unit="kg",
            current_stock=Decimal("1000"), cost_per_unit=Decimal("600.00"),
        )
        RecipeItem.objects.create(
            product=self.product, inventory_item=self.chicken, quantity=Decimal("1"), unit="kg",
        )
        self.owner = self._staff("Owner", StaffRole.OWNER)
        self.cashier = self._staff("Cashier", StaffRole.CASHIER)
        self.waiter = self._staff("Waiter", StaffRole.WAITER)

    def _staff(self, name, role):
        staff = Staff(branch=self.branch, name=name, role=role)
        staff.set_pin("1234")
        staff.save()
        return staff

    def _variant(self, name, price, multiplier, **kwargs):
        return ProductVariant.objects.create(
            product=self.product, name_en=name, name_ar=name,
            price=Decimal(price), recipe_multiplier=Decimal(multiplier), **kwargs,
        )

    def as_staff(self, staff):
        self.client.force_authenticate(user=staff)
        return self.client


class RecipeCostingTests(VariantTestBase):
    def test_base_recipe_cost_is_quantity_times_current_ingredient_cost(self):
        self.assertEqual(recipe_cost(self.product), Decimal("600.00"))

    def test_multiplier_scales_the_ingredient_cost(self):
        """The requirements' worked example, verbatim."""
        half = self._variant("Half KG", "1200.00", "0.5")
        one = self._variant("1 KG", "2200.00", "1.0")
        two = self._variant("2 KG", "4000.00", "2.0")
        self.assertEqual(variant_cost(self.product, half), Decimal("300.00"))
        self.assertEqual(variant_cost(self.product, one), Decimal("600.00"))
        self.assertEqual(variant_cost(self.product, two), Decimal("1200.00"))

    def test_costing_updates_when_the_ingredient_price_changes(self):
        """Nothing is stored, so a new purchase price moves costing immediately."""
        one = self._variant("1 KG", "2200.00", "1.0")
        self.assertEqual(variant_cost(self.product, one), Decimal("600.00"))

        self.chicken.cost_per_unit = Decimal("750.00")
        self.chicken.save(update_fields=["cost_per_unit"])
        self.product.refresh_from_db()

        self.assertEqual(variant_cost(self.product, one), Decimal("750.00"))

    def test_grams_against_a_kg_priced_ingredient_converts(self):
        """The 1000x trap: 500 g of chicken at 600/KG is 300, not 300,000."""
        salt = InventoryItem.objects.create(
            branch=self.branch, name="Salt", unit="kg",
            current_stock=Decimal("100"), cost_per_unit=Decimal("600.00"),
        )
        soup = Product.objects.create(
            branch=self.branch, category=self.category, name_en="Soup", name_ar="Soup",
            price=Decimal("100.00"), cost_price=Decimal("10.00"),
        )
        RecipeItem.objects.create(
            product=soup, inventory_item=salt, quantity=Decimal("500"), unit="g",
        )
        self.assertEqual(recipe_cost(soup), Decimal("300.00"))

    def test_gross_profit_and_food_cost_percent(self):
        one = self._variant("1 KG", "2400.00", "1.0")
        result = costing(self.product, one)
        self.assertEqual(result["ingredient_cost"], Decimal("600.00"))
        self.assertEqual(result["selling_price"], Decimal("2400.00"))
        self.assertEqual(result["gross_profit"], Decimal("1800.00"))
        self.assertEqual(result["food_cost_percent"], Decimal("25.00"))
        self.assertTrue(result["has_recipe"])

    def test_food_cost_percent_is_none_for_a_free_item(self):
        """None, not 0 — a zero percentage would read as a perfect margin."""
        free = Product.objects.create(
            branch=self.branch, category=self.category, name_en="Water", name_ar="Water",
            price=Decimal("0.00"), cost_price=Decimal("0.00"),
        )
        self.assertIsNone(costing(free)["food_cost_percent"])

    def test_a_product_without_variants_costs_its_base_recipe(self):
        self.assertEqual(costing(self.product)["ingredient_cost"], Decimal("600.00"))

    def test_has_recipe_distinguishes_free_to_make_from_not_configured(self):
        bare = Product.objects.create(
            branch=self.branch, category=self.category, name_en="Bare", name_ar="Bare",
            price=Decimal("50.00"), cost_price=Decimal("0.00"),
        )
        self.assertFalse(costing(bare)["has_recipe"])
        self.assertEqual(costing(bare)["ingredient_cost"], Decimal("0.00"))


class VariantPricingTests(VariantTestBase):
    def test_current_price_uses_the_variant_price(self):
        two = self._variant("2 KG", "4000.00", "2.0")
        self.assertEqual(current_price(self.product, two), Decimal("4000.00"))

    def test_current_price_falls_back_to_the_product_without_a_variant(self):
        self.assertEqual(current_price(self.product), Decimal("2200.00"))


class VariantApiTests(VariantTestBase):
    def url(self):
        return f"/api/products/{self.product.pk}/variants/"

    def test_owner_can_create_a_variant(self):
        response = self.as_staff(self.owner).post(self.url(), {
            "name_en": "Half KG", "name_ar": "نصف كيلو",
            "price": "1200.00", "recipe_multiplier": "0.5",
        }, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["costing"]["ingredient_cost"], Decimal("300.00"))

    def test_multiplier_defaults_to_one(self):
        response = self.as_staff(self.owner).post(self.url(), {
            "name_en": "Regular", "name_ar": "عادي", "price": "500.00",
        }, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Decimal(response.data["recipe_multiplier"]), Decimal("1.000"))

    def test_duplicate_variant_name_is_rejected(self):
        self._variant("1 KG", "2200.00", "1.0")
        response = self.as_staff(self.owner).post(self.url(), {
            "name_en": "1 KG", "name_ar": "كيلو", "price": "2500.00",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_negative_price_is_rejected(self):
        response = self.as_staff(self.owner).post(self.url(), {
            "name_en": "Bad", "name_ar": "Bad", "price": "-1.00",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_zero_multiplier_is_rejected(self):
        """A zero multiplier would make a portion consume no stock at all."""
        response = self.as_staff(self.owner).post(self.url(), {
            "name_en": "Ghost", "name_ar": "Ghost", "price": "10.00", "recipe_multiplier": "0",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_cashier_can_read_but_not_write(self):
        self._variant("1 KG", "2200.00", "1.0")
        self.assertEqual(self.as_staff(self.cashier).get(self.url()).status_code, 200)
        response = self.as_staff(self.cashier).post(self.url(), {
            "name_en": "Nope", "name_ar": "Nope", "price": "1.00",
        }, format="json")
        self.assertEqual(response.status_code, 403)

    def test_another_branchs_product_is_not_reachable(self):
        other_branch = Branch.objects.create(name_en="Other", name_ar="Other")
        other_category = Category.objects.create(branch=other_branch, name_en="C", name_ar="C")
        other_product = Product.objects.create(
            branch=other_branch, category=other_category, name_en="X", name_ar="X",
            price=Decimal("10.00"), cost_price=Decimal("1.00"),
        )
        response = self.as_staff(self.owner).get(f"/api/products/{other_product.pk}/variants/")
        self.assertEqual(response.data["results"], [])


class PublicMenuVariantTests(VariantTestBase):
    def test_menu_lists_active_variants_only(self):
        self._variant("1 KG", "2200.00", "1.0")
        self._variant("Retired", "999.00", "1.0", is_active=False)
        response = self.client.get(f"/api/menu/?branch={self.branch.pk}")
        product = response.data["categories"][0]["products"][0]
        names = [variant["name_en"] for variant in product["variants"]]
        self.assertEqual(names, ["1 KG"])

    def test_menu_never_leaks_cost_or_margin(self):
        """This endpoint is AllowAny — ingredient cost is staff data."""
        self._variant("1 KG", "2200.00", "1.0")
        response = self.client.get(f"/api/menu/?branch={self.branch.pk}")
        body = str(response.data)
        for leaked in ("ingredient_cost", "recipe_multiplier", "food_cost_percent", "gross_profit", "cost_price"):
            self.assertNotIn(leaked, body, f"{leaked} must not appear on the public menu")


class VariantOrderSnapshotTests(VariantTestBase):
    """The freeze point: what the customer was charged must never move."""

    def setUp(self):
        super().setUp()
        self.table = Table.objects.create(branch=self.branch, label_en="T1", label_ar="T1")
        self.table.open()
        self.order = Order.objects.create(
            branch=self.branch, table=self.table, status=OrderStatus.OPEN,
        )
        self.one_kg = self._variant("1 KG", "2200.00", "1.0")

    def add_item(self, **overrides):
        payload = {"product": self.product.pk, "quantity": 1, **overrides}
        return self.as_staff(self.cashier).post(
            f"/api/orders/{self.order.pk}/items/", payload, format="json",
        )

    def test_ordering_a_variant_snapshots_its_name_and_price(self):
        response = self.add_item(variant=self.one_kg.pk)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["variant_name_en_snapshot"], "1 KG")
        self.assertEqual(Decimal(response.data["price_snapshot"]), Decimal("2200.00"))

    def test_renaming_a_variant_does_not_rewrite_past_orders(self):
        self.add_item(variant=self.one_kg.pk)
        self.one_kg.name_en = "Full KG"
        self.one_kg.price = Decimal("9999.00")
        self.one_kg.save(update_fields=["name_en", "price"])

        item = self.order.items.first()
        self.assertEqual(item.variant_name_en_snapshot, "1 KG")
        self.assertEqual(item.price_snapshot, Decimal("2200.00"))

    def test_ingredient_price_change_does_not_rewrite_past_orders(self):
        self.add_item(variant=self.one_kg.pk)
        self.chicken.cost_per_unit = Decimal("5000.00")
        self.chicken.save(update_fields=["cost_per_unit"])

        item = self.order.items.first()
        self.assertEqual(item.price_snapshot, Decimal("2200.00"))

    def test_a_variant_from_another_product_is_rejected(self):
        other = Product.objects.create(
            branch=self.branch, category=self.category, name_en="Biryani", name_ar="Biryani",
            price=Decimal("350.00"), cost_price=Decimal("100.00"),
        )
        foreign = ProductVariant.objects.create(
            product=other, name_en="Plate", name_ar="Plate",
            price=Decimal("1.00"), recipe_multiplier=Decimal("1.0"),
        )
        response = self.add_item(variant=foreign.pk)
        self.assertEqual(response.status_code, 400)

    def test_an_inactive_variant_cannot_be_ordered(self):
        retired = self._variant("Retired", "1.00", "1.0", is_active=False)
        response = self.add_item(variant=retired.pk)
        self.assertEqual(response.status_code, 400)

    def test_ordering_without_a_variant_still_works(self):
        """Variants are optional — unvarianted products behave as before."""
        response = self.add_item()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["variant_name_en_snapshot"], "")
        self.assertEqual(Decimal(response.data["price_snapshot"]), Decimal("2200.00"))


class VariantStockTests(VariantTestBase):
    """A 2 KG portion must consume twice the stock of a 1 KG one."""

    def test_stock_check_scales_with_the_multiplier(self):
        from inventory.services import check_product_stock

        self.chicken.current_stock = Decimal("1.5")
        self.chicken.save(update_fields=["current_stock"])

        half = self._variant("Half KG", "1200.00", "0.5")
        two = self._variant("2 KG", "4000.00", "2.0")

        self.assertTrue(check_product_stock(self.product, 1, variant=half)[0])
        # 1.5 kg in stock cannot make a 2 kg portion.
        self.assertFalse(check_product_stock(self.product, 1, variant=two)[0])

    def test_no_variant_checks_the_base_recipe(self):
        from inventory.services import check_product_stock

        self.chicken.current_stock = Decimal("1.5")
        self.chicken.save(update_fields=["current_stock"])
        self.assertTrue(check_product_stock(self.product, 1)[0])
