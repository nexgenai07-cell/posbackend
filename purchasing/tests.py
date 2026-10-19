from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from inventory.models import InventoryItem, StockMovement, StockMovementReason
from purchasing.models import Purchase, PurchaseItem, PurchaseStatus, Supplier


class ReceivedPurchaseReconciliationTests(TestCase):
    """Received PO corrections keep inventory and the append-only ledger aligned."""

    def setUp(self):
        self.branch = Branch.objects.create(name_en="Main", name_ar="Main")
        self.owner = Staff(branch=self.branch, name="Owner", role=StaffRole.OWNER)
        self.owner.set_pin("1234")
        self.owner.save()
        self.cashier = Staff(branch=self.branch, name="Cashier", role=StaffRole.CASHIER)
        self.cashier.set_pin("5678")
        self.cashier.save()
        self.supplier = Supplier.objects.create(name_en="Supplier", name_ar="Supplier")
        self.ingredient = InventoryItem.objects.create(
            branch=self.branch, name="Chicken", unit="kg",
            current_stock=Decimal("20.000"), cost_per_unit=Decimal("8.00"),
        )
        self.purchase = Purchase.objects.create(branch=self.branch, supplier=self.supplier)
        self.line = PurchaseItem.objects.create(
            purchase=self.purchase, inventory_item=self.ingredient,
            quantity=Decimal("10.000"), unit_cost=Decimal("12.00"),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def receive(self):
        response = self.client.post(f"/api/purchases/{self.purchase.pk}/receive/")
        self.assertEqual(response.status_code, 200, response.data)
        self.purchase.refresh_from_db()
        self.ingredient.refresh_from_db()

    def test_received_edit_reverses_then_reapplies_without_deleting_ledger_rows(self):
        self.receive()
        self.assertEqual(self.ingredient.current_stock, Decimal("30.000"))
        self.assertEqual(self.ingredient.cost_per_unit, Decimal("12.00"))

        response = self.client.patch(f"/api/purchases/{self.purchase.pk}/", {
            "supplier": self.supplier.pk,
            "items": [{"inventory_item": self.ingredient.pk, "quantity": "6.000", "unit_cost": "15.00"}],
        }, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.ingredient.refresh_from_db()
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, PurchaseStatus.RECEIVED)
        self.assertEqual(self.ingredient.current_stock, Decimal("26.000"))
        self.assertEqual(self.ingredient.cost_per_unit, Decimal("15.00"))
        movements = StockMovement.objects.filter(purchase=self.purchase, reason=StockMovementReason.PURCHASE)
        self.assertEqual(movements.count(), 3)
        self.assertEqual(sum(row.quantity_delta for row in movements), Decimal("6.000"))
        self.assertEqual(list(movements.order_by("id").values_list("quantity_delta", flat=True)), [
            Decimal("10.000"), Decimal("-10.000"), Decimal("6.000"),
        ])

        response = self.client.patch(f"/api/purchases/{self.purchase.pk}/", {
            "supplier": self.supplier.pk,
            "items": [{"inventory_item": self.ingredient.pk, "quantity": "4.000", "unit_cost": "17.00"}],
        }, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.current_stock, Decimal("24.000"))
        self.assertEqual(self.ingredient.cost_per_unit, Decimal("17.00"))
        movements = StockMovement.objects.filter(purchase=self.purchase, reason=StockMovementReason.PURCHASE)
        self.assertEqual(movements.count(), 5)
        self.assertEqual(sum(row.quantity_delta for row in movements), Decimal("4.000"))

    def test_received_delete_reverses_stock_and_preserves_ledger(self):
        self.receive()
        response = self.client.delete(f"/api/purchases/{self.purchase.pk}/")

        self.assertEqual(response.status_code, 204)
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.current_stock, Decimal("20.000"))
        self.assertFalse(Purchase.objects.filter(pk=self.purchase.pk).exists())
        self.assertTrue(Purchase.all_objects.filter(pk=self.purchase.pk, is_deleted=True).exists())
        movements = StockMovement.objects.filter(purchase=self.purchase, reason=StockMovementReason.PURCHASE)
        self.assertEqual(movements.count(), 2)
        self.assertEqual(sum(row.quantity_delta for row in movements), Decimal("0.000"))

    def test_non_manager_cannot_edit_or_delete_received_purchase(self):
        self.receive()
        self.client.force_authenticate(self.cashier)
        url = f"/api/purchases/{self.purchase.pk}/"
        self.assertEqual(self.client.patch(url, {"supplier": self.supplier.pk}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.current_stock, Decimal("30.000"))


class PurchaseListPagingTests(TestCase):
    """The admin pages the purchase list on the server, sorted there too."""

    def setUp(self):
        self.branch = Branch.objects.create(name_en="Main", name_ar="Main")
        owner = Staff(branch=self.branch, name="Owner", role=StaffRole.OWNER)
        owner.set_pin("1234")
        owner.save()
        self.client = APIClient()
        self.client.force_authenticate(user=owner)
        item = InventoryItem.objects.create(branch=self.branch, name="Rice", unit="kg")
        self.ids = {}
        for name, cost in (("Bravo", "30"), ("Alpha", "50"), ("Charlie", "10")):
            purchase = Purchase.objects.create(branch=self.branch, supplier=Supplier.objects.create(name_en=name, name_ar=""))
            PurchaseItem.objects.create(purchase=purchase, inventory_item=item, quantity=2, unit_cost=Decimal(cost))
            self.ids[name] = purchase.pk

    def suppliers(self, query=""):
        return [row["supplier_name"] for row in self.client.get(f"/api/purchases/{query}").json()["results"]]

    def test_newest_first_by_default(self):
        self.assertEqual(self.suppliers(), ["Charlie", "Alpha", "Bravo"])

    def test_sorted_by_supplier_and_by_total(self):
        self.assertEqual(self.suppliers("?ordering=supplier"), ["Alpha", "Bravo", "Charlie"])
        self.assertEqual(self.suppliers("?ordering=-total"), ["Alpha", "Bravo", "Charlie"])
        self.assertEqual(self.suppliers("?ordering=total"), ["Charlie", "Bravo", "Alpha"])

    def test_sort_works_with_search_and_pages(self):
        self.assertEqual(self.suppliers("?search=a&ordering=supplier"), ["Alpha", "Bravo", "Charlie"])
        body = self.client.get("/api/purchases/?ordering=supplier&page_size=2&page=2").json()
        self.assertEqual((body["count"], [row["supplier_name"] for row in body["results"]]), (3, ["Charlie"]))
