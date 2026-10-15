from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product, ProductVariant
from customers.models import Customer
from inventory.models import InventoryItem, RecipeItem
from inventory.services import deduct_stock_for_order_items
from orders.models import Bill, Order, OrderItem, OrderItemStatus, OrderStatus, Payment, PaymentStatus
from purchasing.models import Purchase, PurchaseItem, PurchaseStatus, Supplier
from tables.models import Table


class ReportSetup(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="A", name_ar="A", timezone="Asia/Riyadh", currency="SAR")
        self.owner = Staff.objects.create(branch=self.branch, name="Owner", role=StaffRole.OWNER, pin_hash="x")
        self.waiter = Staff.objects.create(branch=self.branch, name="Ali", role=StaffRole.WAITER, pin_hash="x")
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        category = Category.objects.create(branch=self.branch, name_en="Food", name_ar="Food")
        self.karahi = Product.objects.create(branch=self.branch, category=category, name_en="Karahi", name_ar="K", price=45, cost_price=0)
        self.half = ProductVariant.objects.create(product=self.karahi, name_en="Half KG", name_ar="H", price=45, recipe_multiplier=1)
        self.full = ProductVariant.objects.create(product=self.karahi, name_en="1 KG", name_ar="F", price=85, recipe_multiplier=2)
        self.chicken = InventoryItem.objects.create(branch=self.branch, name="Chicken", unit="kg", current_stock=100)
        RecipeItem.objects.create(product=self.karahi, inventory_item=self.chicken, quantity=500, unit="g")
        supplier = Supplier.objects.create(name_en="S", name_ar="S")
        purchase = Purchase.objects.create(branch=self.branch, supplier=supplier, status=PurchaseStatus.RECEIVED, received_at=timezone.now())
        PurchaseItem.objects.create(purchase=purchase, inventory_item=self.chicken, quantity=10, unit_cost=20)
        self.table = Table.objects.create(branch=self.branch, label_en="T1", label_ar="ط1")
        self.customer = Customer.objects.create(branch=self.branch, phone="0500000000", name="Sara")

    def paid_order(self, lines, *, paid, tip=0, bill=None, customer=None, minutes=40):
        opened = timezone.now() - timedelta(minutes=minutes)
        order = Order.objects.create(
            branch=self.branch, status=OrderStatus.CLOSED, payment_status=PaymentStatus.PAID, table=self.table,
            bill=bill, customer=customer, assigned_waiter=self.waiter,
        )
        items = [
            OrderItem.objects.create(
                order=order, product=self.karahi, variant=variant, name_en_snapshot="Karahi", name_ar_snapshot="K",
                variant_name_en_snapshot=variant.name_en, price_snapshot=variant.price, quantity=quantity,
            )
            for variant, quantity in lines
        ]
        with transaction.atomic():
            deduct_stock_for_order_items(items)
        OrderItem.objects.filter(order=order).update(status=OrderItemStatus.SERVED)
        Order.objects.filter(pk=order.pk).update(opened_at=opened, closed_at=timezone.now())
        Payment.objects.create(order=order, amount=paid, method="cash", tip=tip)
        return order


class ProfitabilityBySizeTests(ReportSetup):
    def test_each_size_is_its_own_row_with_its_own_cost_and_todays_cost(self):
        self.paid_order([(self.half, 2), (self.full, 1)], paid=Decimal("175"))
        rows = {row["variant_name"]: row for row in self.client.get("/api/reports/product-profitability/?preset=today").json()["results"]}
        self.assertEqual(set(rows), {"Half KG", "1 KG"})
        # Half: 2 x 0.5 kg x 20 = 20; 1 KG: 1 x 1 kg x 20 = 20.
        self.assertEqual((Decimal(rows["Half KG"]["ingredient_cost"]), Decimal(rows["1 KG"]["ingredient_cost"])), (Decimal("20"), Decimal("20")))
        self.assertEqual(Decimal(rows["Half KG"]["current_recipe_cost_per_unit"]), Decimal("10"))
        self.assertEqual(Decimal(rows["1 KG"]["current_recipe_cost_per_unit"]), Decimal("20"))


class OrderDetailsTests(ReportSetup):
    def test_every_order_with_lines_discount_tips_cost_table_and_customer(self):
        bill = Bill.objects.create(branch=self.branch, table=self.table, customer=self.customer)
        Bill.objects.filter(pk=bill.pk).update(created_at=timezone.now() - timedelta(minutes=55), closed_at=timezone.now())
        self.paid_order([(self.half, 2)], paid=Decimal("80"), tip=Decimal("5"), bill=bill, customer=self.customer)
        data = self.client.get("/api/reports/order-details/?preset=today").json()
        order = data["orders"][0]
        self.assertEqual(Decimal(order["items_total"]), Decimal("90"))
        self.assertEqual(Decimal(order["paid"]), Decimal("80"))
        self.assertEqual(Decimal(order["discount"]), Decimal("10"))
        self.assertEqual(Decimal(order["tips"]), Decimal("5"))
        self.assertEqual(Decimal(order["cost"]), Decimal("20"))
        self.assertEqual(Decimal(order["profit"]), Decimal("60"))
        self.assertEqual((order["table"]["label_en"], order["customer"]["name"], order["waiter"]), ("T1", "Sara", "Ali"))
        # The sitting is the bill's open-to-release time.
        self.assertEqual(order["seated_minutes"], 55)
        self.assertEqual(order["items"][0]["variant_en"], "Half KG")
        customer = data["customers"][0]
        self.assertEqual((customer["orders"], Decimal(customer["spend"]), customer["average_minutes"]), (1, Decimal("80"), 55))
        self.assertEqual(data["tables"][0]["label_en"], "T1")

    def test_cancelled_orders_show_reason_stage_who_refund_and_waste(self):
        order = self.paid_order([(self.full, 1)], paid=Decimal("85"))
        self.client.post(f"/api/orders/{order.pk}/cancel/", {"reason": "Cold food"}, format="json")
        data = self.client.get("/api/reports/order-details/?preset=today").json()
        self.assertEqual(data["orders"], [])
        row = data["cancelled"][0]
        self.assertEqual(
            (row["reason"], row["from_status"], row["cancelled_by"], Decimal(row["total"]), Decimal(row["refunded"]), Decimal(row["waste_cost"])),
            ("Cold food", "closed", "Owner", Decimal("85"), Decimal("85"), Decimal("20")),
        )
        self.assertEqual(Decimal(data["totals"]["refunded"]), Decimal("85"))

    def test_owner_or_manager_only(self):
        self.client.force_authenticate(user=self.waiter)
        self.assertEqual(self.client.get("/api/reports/order-details/").status_code, 403)


class BusinessSummaryTests(ReportSetup):
    def test_verdicts_attention_and_waste_come_from_the_backend(self):
        from inventory.models import StockMovementReason
        from inventory.services import adjust_stock

        self.paid_order([(self.half, 1)], paid=Decimal("45"))
        # Chicken gets dearer after the sale: the dish is flagged for a price check.
        supplier = Supplier.objects.create(name_en="S2", name_ar="S2")
        purchase = Purchase.objects.create(branch=self.branch, supplier=supplier, status=PurchaseStatus.RECEIVED, received_at=timezone.now())
        PurchaseItem.objects.create(purchase=purchase, inventory_item=self.chicken, quantity=1, unit_cost=30)
        # The stock item's typed cost is 0; the waste is valued at its purchase price.
        with transaction.atomic():
            adjust_stock(self.chicken.pk, Decimal("-2"), StockMovementReason.WASTE)
        data = self.client.get("/api/reports/business-summary/?preset=today").json()

        dish = data["top_profit"][0]
        self.assertEqual((dish["verdict"], dish["cost_rose"]), ("good", True))
        self.assertEqual(Decimal(dish["cost_per_plate"]), Decimal("10"))
        self.assertEqual(Decimal(data["waste"]["total"]), Decimal("60"))
        codes = [item["code"] for item in data["attention"]]
        self.assertIn("cost_up", codes)
        self.assertIn("waste", codes)
        self.assertEqual(Decimal(data["money"]["known_gross_profit"]), Decimal("35"))

    def test_dashboard_sales_is_money_taken_not_menu_prices(self):
        self.paid_order([(self.half, 2)], paid=Decimal("80"))
        self.assertEqual(Decimal(self.client.get("/api/reports/dashboard-summary/?preset=today").json()["total_sales"]), Decimal("80"))
