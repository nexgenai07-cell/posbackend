"""Report date filters: branch-timezone days, paid-today counted, filters on the server."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from catalog.models import Category, Product
from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus, Payment, PaymentStatus
from tables.models import Table

RIYADH = ZoneInfo("Asia/Riyadh")


class ReportDateFilterTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="B", name_ar="B", timezone="Asia/Riyadh")
        self.owner = Staff(branch=self.branch, name="O", role=StaffRole.OWNER)
        self.owner.set_pin("1")
        self.owner.save()
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        category = Category.objects.create(branch=self.branch, name_en="C", name_ar="C")
        self.product = Product.objects.create(branch=self.branch, category=category, name_en="Karahi", name_ar="K",
                                              price=50, cost_price=20)
        self.table = Table.objects.create(branch=self.branch, label_en="Garden 1", label_ar="G1")
        self.other_table = Table.objects.create(branch=self.branch, label_en="Hall 2", label_ar="H2")

    def make_order(self, at, *, status=OrderStatus.PAID, paid=True, table=None, method="cash"):
        order = Order.objects.create(branch=self.branch, table=table or self.table, status=status,
                                     payment_status=PaymentStatus.PAID if paid else PaymentStatus.UNPAID)
        Order.objects.filter(pk=order.pk).update(opened_at=at)
        OrderItem.objects.create(order=order, product=self.product, name_en_snapshot="Karahi", name_ar_snapshot="K",
                                 price_snapshot=Decimal("50.00"), quantity=1, status=OrderItemStatus.SERVED)
        if paid:
            payment = Payment.objects.create(order=order, amount=Decimal("50.00"), method=method, idempotency_key=f"p-{order.pk}")
            Payment.objects.filter(pk=payment.pk).update(paid_at=at)
        return order

    def get(self, path, **params):
        return self.client.get(path, params).json()

    def test_paid_order_with_table_not_released_counts_today(self):
        now = timezone.now()
        self.make_order(now, status=OrderStatus.PAID)          # paid, table not released
        self.make_order(now, status=OrderStatus.SERVED, paid=False)  # not paid: not a sale yet
        summary = self.get("/api/reports/dashboard-summary/", preset="today")
        self.assertEqual((summary["order_count"], Decimal(str(summary["total_sales"]))), (1, Decimal("50.00")))
        sales = self.get("/api/reports/sales-overview/", preset="today")
        self.assertEqual(Decimal(str(sales["total_sales"])), Decimal("50.00"))
        financial = self.get("/api/reports/financial-summary/", preset="today")
        self.assertEqual(Decimal(str(financial["revenue"])), Decimal("50.00"))

    def test_today_preset_and_custom_range_of_today_agree(self):
        self.make_order(timezone.now())
        today = timezone.localdate(timezone=RIYADH).isoformat()
        for path in ("/api/reports/dashboard-summary/", "/api/reports/sales-overview/", "/api/reports/product-performance/"):
            with self.subTest(path):
                by_preset = self.get(path, preset="today")
                by_range = self.get(path, preset="custom", **{"from": today, "to": today})
                self.assertEqual(by_preset, by_range)

    def test_days_follow_the_restaurant_clock_not_utc(self):
        # 01:00 in Riyadh on the 11th is 22:00 UTC on the 10th.
        local = datetime(2026, 10, 11, 1, 0, tzinfo=RIYADH)
        self.make_order(local)
        on_11th = self.get("/api/reports/sales-overview/", **{"from": "2026-10-11", "to": "2026-10-11"})
        on_10th = self.get("/api/reports/sales-overview/", **{"from": "2026-10-10", "to": "2026-10-10"})
        self.assertEqual((Decimal(str(on_11th["total_sales"])), Decimal(str(on_10th["total_sales"]))), (Decimal("50.00"), Decimal("0")))
        self.assertEqual([row["day"] for row in on_11th["by_day"]], ["2026-10-11"])
        recent = self.get("/api/reports/recent-orders/", **{"from": "2026-10-11", "to": "2026-10-11"})
        self.assertEqual(len(recent), 1)
        peak = self.get("/api/reports/peak-hours/", **{"from": "2026-10-11", "to": "2026-10-11"})
        self.assertEqual(peak["results"], [{"hour": 1, "order_count": 1}])

    def test_recent_orders_filters_run_on_the_server_before_the_limit(self):
        now = timezone.now()
        target = self.make_order(now - timedelta(minutes=30), table=self.other_table, method="card")
        for _ in range(5):
            self.make_order(now)  # newer, would fill a small limit
        rows = self.get("/api/reports/recent-orders/", preset="today", limit=2, table=self.other_table.pk)
        self.assertEqual([row["id"] for row in rows], [target.pk])
        rows = self.get("/api/reports/recent-orders/", preset="today", limit=2, method="card")
        self.assertEqual([row["id"] for row in rows], [target.pk])
        rows = self.get("/api/reports/recent-orders/", preset="today", search="hall")
        self.assertEqual([row["id"] for row in rows], [target.pk])
        rows = self.get("/api/reports/recent-orders/", preset="today", search=f"ORD-{target.pk:08d}")
        self.assertEqual([row["id"] for row in rows], [target.pk])
        rows = self.get("/api/reports/recent-orders/", preset="today", status="served")
        self.assertEqual(rows, [])


    def test_staff_attendance_counts_shifts_by_restaurant_day(self):
        from accounts.models import Shift

        waiter = Staff(branch=self.branch, name="Waiter", role=StaffRole.WAITER)
        waiter.set_pin("2")
        waiter.save()
        # 01:00-05:00 Riyadh on the 11th (22:00 UTC on the 10th) and an older shift.
        Shift.objects.create(staff=waiter, clock_in=datetime(2026, 10, 11, 1, 0, tzinfo=RIYADH),
                             clock_out=datetime(2026, 10, 11, 5, 0, tzinfo=RIYADH))
        Shift.objects.create(staff=waiter, clock_in=datetime(2026, 10, 9, 10, 0, tzinfo=RIYADH),
                             clock_out=datetime(2026, 10, 9, 12, 0, tzinfo=RIYADH))
        data = self.get("/api/reports/staff-attendance/", **{"from": "2026-10-11", "to": "2026-10-11"})
        row = next(item for item in data["results"] if item["staff_id"] == waiter.pk)
        self.assertEqual((row["hours_in_window"], row["on_shift"]), (4.0, False))
        on_10th = self.get("/api/reports/staff-attendance/", **{"from": "2026-10-10", "to": "2026-10-10"})
        self.assertEqual(next(i for i in on_10th["results"] if i["staff_id"] == waiter.pk)["hours_in_window"], 0)
