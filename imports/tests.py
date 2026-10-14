from decimal import Decimal
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from openpyxl import Workbook, load_workbook
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product, ProductVariant
from inventory.models import InventoryItem, RecipeItem, StockMovement, StockMovementReason


def make_staff(branch, name, role):
    staff = Staff(branch=branch, name=name, role=role, is_active=True)
    staff.set_pin("1234")
    staff.save()
    return staff


def xlsx(headers, *rows, title="Sheet"):
    book = Workbook()
    sheet = book.active
    sheet.title = title
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    output = BytesIO()
    book.save(output)
    return SimpleUploadedFile("import.xlsx", output.getvalue())


class ImportTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(name_en="Farzan", name_ar="فرزان", timezone="Asia/Karachi", currency="PKR")
        cls.owner = make_staff(cls.branch, "Owner", StaffRole.OWNER)
        cls.cashier = make_staff(cls.branch, "Cashier", StaffRole.CASHIER)
        cls.chicken = InventoryItem.objects.create(branch=cls.branch, name="Chicken", unit="kg", current_stock=10, par_level=2, cost_per_unit=600)
        cls.category = Category.objects.create(branch=cls.branch, name_en="Karahi", name_ar="")
        cls.karahi = Product.objects.create(branch=cls.branch, category=cls.category, name_en="Chicken Karahi", name_ar="", price=1400, cost_price=0)
        cls.half = ProductVariant.objects.create(product=cls.karahi, name_en="Half KG", name_ar="", price=1400, recipe_multiplier=1)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def upload(self, kind, file, commit=False):
        data = {"file": file}
        if commit:
            data["commit"] = "1"
        return self.client.post(f"/api/imports/{kind}/", data, format="multipart")


class TemplateTests(ImportTestCase):
    def test_template_is_prefilled_with_current_stock(self):
        response = self.client.get("/api/imports/stock/template/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        book = load_workbook(BytesIO(response.content))
        sheet = book["Stock"]
        self.assertEqual(sheet["A1"].value, "Name *")
        self.assertEqual(sheet["A2"].value, "Chicken")
        self.assertEqual(sheet["B2"].value, "kg")
        self.assertIn("Instructions", book.sheetnames)

    def test_only_owner_or_manager_can_import(self):
        self.client.force_authenticate(user=self.cashier)
        self.assertEqual(self.client.get("/api/imports/stock/template/").status_code, status.HTTP_403_FORBIDDEN)

    def test_unknown_kind_is_404(self):
        self.assertEqual(self.client.get("/api/imports/nothing/template/").status_code, status.HTTP_404_NOT_FOUND)


class StockImportTests(ImportTestCase):
    HEADERS = ("Name *", "Unit *", "Current stock", "Par level", "Cost per unit", "Supplier")

    def test_preview_shows_changes_without_writing(self):
        file = xlsx(self.HEADERS, ("chicken", "kg", 12, None, 650, None), ("Oil", "l", 5, 1, 400, "Al Noor"))
        body = self.upload("stock", file).json()
        self.assertEqual(body["summary"]["update"], 1)
        self.assertEqual(body["summary"]["create"], 1)
        update = body["rows"][0]
        self.assertEqual(update["action"], "update")
        self.assertIn({"field": "cost_per_unit", "old": "600", "new": "650"}, update["changes"])
        self.assertIn({"field": "current_stock", "old": "10", "new": "12"}, update["changes"])
        self.assertEqual(body["rows"][1]["notes"], [{"code": "newSupplier", "params": {"name": "Al Noor"}}])
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.cost_per_unit, Decimal("600.00"))
        self.assertFalse(InventoryItem.objects.filter(name="Oil").exists())

    def test_commit_applies_and_records_stock_adjustments(self):
        file = xlsx(self.HEADERS, ("Chicken", "kg", 12, None, 650, None), ("Oil", "l", 5, 1, 400, "Al Noor"))
        response = self.upload("stock", file, commit=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["committed"])
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("12"))
        self.assertEqual(self.chicken.cost_per_unit, Decimal("650"))
        movement = StockMovement.objects.get(inventory_item=self.chicken)
        self.assertEqual(movement.quantity_delta, Decimal("2"))
        self.assertEqual(movement.reason, StockMovementReason.ADJUSTMENT)
        oil = InventoryItem.objects.get(branch=self.branch, name="Oil")
        self.assertEqual((oil.current_stock, oil.supplier.name_en), (Decimal("5"), "Al Noor"))

    def test_any_error_blocks_the_whole_commit(self):
        file = xlsx(self.HEADERS, ("Oil", "l", 5, None, None, None), ("Rice", "kg", "lots", None, None, None))
        response = self.upload("stock", file, commit=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["rows"][1]["errors"][0]["code"], "numberInvalid")
        self.assertFalse(InventoryItem.objects.filter(name="Oil").exists())

    def test_duplicate_names_and_missing_columns_are_reported(self):
        body = self.upload("stock", xlsx(self.HEADERS, ("Oil", "l"), ("oil", "l"))).json()
        self.assertEqual(body["rows"][1]["errors"][0], {"code": "duplicateInFile", "params": {"firstRow": 2}})
        body = self.upload("stock", xlsx(("Name",), ("Oil",))).json()
        self.assertEqual(body["file_errors"][0]["code"], "missingColumns")

    def test_unit_change_that_breaks_a_recipe_is_rejected(self):
        RecipeItem.objects.create(product=self.karahi, inventory_item=self.chicken, quantity=500, unit="g")
        body = self.upload("stock", xlsx(self.HEADERS, ("Chicken", "pcs"))).json()
        self.assertEqual(body["rows"][0]["errors"][0]["code"], "unitBreaksRecipes")

    def test_not_an_excel_file(self):
        body = self.upload("stock", SimpleUploadedFile("x.xlsx", b"hello")).json()
        self.assertEqual(body["file_errors"], [{"code": "notExcel"}])


class ProductImportTests(ImportTestCase):
    HEADERS = ("Category *", "Name (English)", "Name (Arabic)", "Description (English)", "Description (Arabic)",
               "Price", "Available", "Size (English)", "Size (Arabic)", "Size price", "Recipe multiplier")

    def test_new_size_on_existing_dish_and_new_dish_in_new_category(self):
        rows = (
            ("Karahi", "Chicken Karahi", None, None, None, 1400, "Yes", "Half KG", None, 1400, 1),
            ("Karahi", "Chicken Karahi", None, None, None, 1400, "Yes", "1 KG", None, 2600, 2),
            ("Drinks", "Lassi", None, None, None, 300, "No", None, None, None, None),
        )
        body = self.upload("products", xlsx(self.HEADERS, *rows)).json()
        actions = [row["action"] for row in body["rows"]]
        self.assertEqual(actions, ["unchanged", "create", "create"])
        self.assertEqual(body["rows"][2]["notes"], [{"code": "newCategory", "params": {"name": "Drinks"}}])

        response = self.upload("products", xlsx(self.HEADERS, *rows), commit=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        full = ProductVariant.objects.get(product=self.karahi, name_en="1 KG")
        self.assertEqual((full.price, full.recipe_multiplier), (Decimal("2600"), Decimal("2")))
        lassi = Product.objects.get(branch=self.branch, name_en="Lassi")
        self.assertEqual((lassi.category.name_en, lassi.is_available), ("Drinks", False))

    def test_price_change_is_previewed_old_to_new(self):
        body = self.upload("products", xlsx(self.HEADERS, ("Karahi", "Chicken Karahi", None, None, None, 1500))).json()
        self.assertIn({"field": "price", "old": "1400", "new": "1500"}, body["rows"][0]["changes"])

    def test_conflicting_dish_values_across_size_rows(self):
        file = xlsx(
            self.HEADERS,
            ("Karahi", "Chicken Karahi", None, None, None, 1400, None, "Half KG", None, 1400, 1),
            ("Karahi", "Chicken Karahi", None, None, None, 1500, None, "1 KG", None, 2600, 2),
        )
        body = self.upload("products", file).json()
        self.assertEqual(body["rows"][1]["errors"][0], {"code": "conflictingValue", "params": {"column": "Price", "firstRow": 2}})

    def test_new_dish_needs_a_price(self):
        body = self.upload("products", xlsx(self.HEADERS, ("Karahi", "Mutton Karahi"))).json()
        self.assertEqual(body["rows"][0]["errors"][0]["code"], "priceRequired")


class RecipeImportTests(ImportTestCase):
    HEADERS = ("Dish *", "Ingredient *", "Quantity *", "Unit *")

    def setUp(self):
        super().setUp()
        self.oil = InventoryItem.objects.create(branch=self.branch, name="Oil", unit="l", cost_per_unit=400)
        self.tomato = InventoryItem.objects.create(branch=self.branch, name="Tomato", unit="kg", cost_per_unit=200)
        RecipeItem.objects.create(product=self.karahi, inventory_item=self.chicken, quantity=500, unit="g")
        RecipeItem.objects.create(product=self.karahi, inventory_item=self.tomato, quantity=200, unit="g")

    def test_recipe_is_replaced_and_removed_lines_are_previewed(self):
        file = xlsx(self.HEADERS, ("Chicken Karahi", "Chicken", 600, "g"), ("Chicken Karahi", "Oil", 50, "ml"))
        body = self.upload("recipes", file).json()
        self.assertEqual([row["action"] for row in body["rows"]], ["update", "create", "delete"])
        self.assertEqual(body["rows"][2]["name"], "Chicken Karahi · Tomato")

        response = self.upload("recipes", xlsx(self.HEADERS, ("Chicken Karahi", "Chicken", 600, "g"), ("Chicken Karahi", "Oil", 50, "ml")), commit=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        lines = {line.inventory_item.name: (line.quantity, line.unit) for line in RecipeItem.objects.filter(product=self.karahi)}
        self.assertEqual(lines, {"Chicken": (Decimal("600"), "g"), "Oil": (Decimal("50"), "ml")})
        self.karahi.refresh_from_db()
        # 0.6 kg x 600 + 0.05 l x 400 = 360 + 20
        self.assertEqual(self.karahi.cost_price, Decimal("380.00"))

    def test_unknown_names_and_incompatible_units(self):
        file = xlsx(self.HEADERS, ("Biryani", "Chicken", 1, "kg"), ("Chicken Karahi", "Saffron", 1, "g"), ("Chicken Karahi", "Chicken", 2, "pcs"))
        rows = self.upload("recipes", file).json()["rows"]
        self.assertEqual([row["errors"][0]["code"] for row in rows[:3]], ["dishNotFound", "ingredientNotFound", "unitIncompatible"])
        # Tomato is not in the file, so it would leave the recipe — shown, not hidden.
        self.assertEqual((rows[3]["name"], rows[3]["action"]), ("Chicken Karahi · Tomato", "delete"))
