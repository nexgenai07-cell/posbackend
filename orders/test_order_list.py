from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch
from tables.models import Table

from .models import Order, OrderStatus, Payment, PaymentStatus


class OrderListPagingTests(TestCase):
    """The Orders screen pages its list, so its counts come from the server
    and must describe the whole selection, not one page."""

    def setUp(self):
        self.branch = Branch.objects.create(name_en="Main", name_ar="Main")
        owner = Staff(branch=self.branch, name="Owner", role=StaffRole.OWNER)
        owner.set_pin("1234")
        owner.save()
        self.client = APIClient()
        self.client.force_authenticate(user=owner)
        self.t1 = Table.objects.create(branch=self.branch, label_en="T1", label_ar="T1")
        self.t2 = Table.objects.create(branch=self.branch, label_en="T2", label_ar="T2")

    def order(self, status, table=None, paid=None):
        order = Order.objects.create(
            branch=self.branch, table=table, status=status,
            payment_status=PaymentStatus.PAID if paid else PaymentStatus.UNPAID,
        )
        if paid:
            for amount in paid:
                Payment.objects.create(order=order, amount=Decimal(amount), method="cash")
        return order

    def test_summary_counts_the_whole_selection_not_a_page(self):
        self.order(OrderStatus.SENT)
        self.order(OrderStatus.CONFIRMED)
        self.order(OrderStatus.PREPARING)
        self.order(OrderStatus.READY)
        self.order(OrderStatus.PAID, paid=["10.50", "4.50"])  # two payments: counted once, summed
        self.order(OrderStatus.PAID, paid=["20"])
        self.order(OrderStatus.CANCELLED)

        page = self.client.get("/api/orders/?page_size=2").json()
        self.assertEqual((page["count"], len(page["results"])), (7, 2))
        summary = self.client.get("/api/orders/summary/").json()
        self.assertEqual(summary, {
            "total": 7, "pending": 2, "preparing": 1, "ready": 1, "served": 0,
            "paid": 2, "cancelled": 1, "sales": "35.00",
        })

    def test_summary_applies_the_list_filters(self):
        self.order(OrderStatus.PAID, paid=["20"])
        self.order(OrderStatus.READY)
        summary = self.client.get("/api/orders/summary/?status=paid").json()
        self.assertEqual((summary["total"], summary["ready"], summary["sales"]), (1, 0, "20.00"))

    def test_latest_per_table_is_one_order_per_table(self):
        self.order(OrderStatus.PAID, table=self.t1)
        latest_t1 = self.order(OrderStatus.SENT, table=self.t1)
        latest_t2 = self.order(OrderStatus.READY, table=self.t2)
        self.order(OrderStatus.SENT)  # takeaway: no table, not part of the map

        rows = self.client.get("/api/orders/?latest_per_table=1").json()["results"]
        self.assertEqual(sorted(row["id"] for row in rows), sorted([latest_t1.pk, latest_t2.pk]))
